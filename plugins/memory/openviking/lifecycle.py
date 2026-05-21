"""OpenViking MemoryProvider lifecycle implementation."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home

from .client import _VikingClient
from .utils import (
    _AUTO_RECALL_SOURCE_MARKER,
    _DEFAULT_COMMIT_KEEP_RECENT_COUNT,
    _OPENVIKING_CONTEXT_CLOSE,
    _OPENVIKING_CONTEXT_OPEN,
    _context_type_for_uri,
    _strip_relevant_memories,
    _url_path_part,
)

logger = logging.getLogger(__name__)
_SESSION_SYNC_QUEUE_DIRNAME = "openviking-session-sync-queue"
_SESSION_SYNC_DRAIN_LIMIT = 100


def _client_class():
    import plugins.memory.openviking as openviking_package

    return getattr(openviking_package, "_VikingClient", _VikingClient)


class OpenVikingLifecycleMixin:
    def system_prompt_block(self) -> str:
        if not self._client:
            return ""
        # Provide brief info about the knowledge base
        try:
            # Check what's in the knowledge base via a root listing
            resp = self._client.get("/api/v1/fs/ls", params={"uri": "viking://"})
            result = resp.get("result", [])
            children = len(result) if isinstance(result, list) else 0
            if children == 0:
                return ""
            return (
                "# OpenViking Knowledge Base\n"
                f"Active. Endpoint: {self._endpoint}\n"
                "Use viking_search to find information, viking_read for details "
                "(abstract/overview/full), viking_browse to explore.\n"
                "Use viking_remember to store facts, viking_add_resource to index URLs/docs."
            )
        except Exception as e:
            logger.warning("OpenViking system_prompt_block failed: %s", e)
            return (
                "# OpenViking Knowledge Base\n"
                f"Active. Endpoint: {self._endpoint}\n"
                "Use viking_search, viking_read, viking_browse, "
                "viking_remember, viking_add_resource."
            )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Return prefetched results from the background thread."""
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=5.0)
        with self._prefetch_lock:
            result = self._prefetch_result
            self._prefetch_result = ""
        if not result:
            return ""
        return result

    def _load_memory_tree(self, client: _VikingClient, scope_uri: str, label: str, max_files: int = 15, max_chars_per_file: int = 2000) -> str:
        """Load memory content from a viking:// URI tree using tiered loading.

        Uses tree() API for flat traversal instead of recursive ls(),
        reducing HTTP calls from O(n) to O(1).

        Strategy:
        1. For directories: read .overview.md (L1) first, skip if empty
        2. For files: read content/read (L2) but truncate
        3. Deduplicate by URI
        4. Respect max_files limit

        Returns formatted text for injection into the system prompt.
        """
        parts: List[str] = []
        files_loaded = 0
        seen_uris: set = set()

        # Single tree() call gets all entries with rel_path
        try:
            resp = client.get("/api/v1/fs/tree", params={"uri": scope_uri, "level_limit": 5})
            entries = self._unwrap_result(resp)
            if not isinstance(entries, list):
                return ""
        except Exception as e:
            logger.debug("OpenViking prefetch: fs/tree failed for %s: %s", scope_uri, e)
            return ""

        # Group entries by parent directory for overview-first loading
        dirs_by_parent: Dict[str, List[dict]] = {}
        files_by_parent: Dict[str, List[str]] = {}

        for e in entries:
            if not isinstance(e, dict):
                continue
            entry_uri = e.get("uri", "")
            is_dir = bool(e.get("isDir") or e.get("is_dir"))
            rel_path = e.get("rel_path", "")
            name = rel_path.rsplit("/", 1)[-1] if "/" in rel_path else rel_path

            # Skip pseudo summary files
            if name.startswith(".") and name.endswith(".md"):
                continue

            parent = scope_uri
            if "/" in rel_path:
                parent_rel = rel_path.rsplit("/", 1)[0]
                parent = scope_uri.rstrip("/") + "/" + parent_rel

            if is_dir:
                dirs_by_parent.setdefault(parent, []).append({"uri": entry_uri, "rel_path": rel_path})
            else:
                files_by_parent.setdefault(parent, []).append(entry_uri)

        # Process directories: try L1 overview first
        for parent, dirs in dirs_by_parent.items():
            for d in dirs:
                if files_loaded >= max_files:
                    break
                dir_uri = d["uri"]
                overview_uri = dir_uri.rstrip("/") + "/.overview.md"
                if overview_uri in seen_uris:
                    continue
                seen_uris.add(overview_uri)

                overview_content = ""
                try:
                    resp = client.get("/api/v1/content/overview", params={"uri": dir_uri})
                    result = self._unwrap_result(resp)
                    if isinstance(result, str):
                        overview_content = result
                    elif isinstance(result, dict):
                        overview_content = result.get("content", "") or result.get("text", "")
                except Exception:
                    pass

                if overview_content and overview_content.strip():
                    display_name = d["rel_path"]
                    parts.append(f"### {display_name}/ (overview)\n{overview_content.strip()}")
                    files_loaded += 1
                else:
                    # L1 not ready, try first non-hidden file as fallback
                    sibling_files = files_by_parent.get(dir_uri, [])
                    for file_uri in sibling_files:
                        if file_uri in seen_uris:
                            continue
                        fname = file_uri.rsplit("/", 1)[-1]
                        if fname.startswith("."):
                            continue
                        seen_uris.add(file_uri)
                        try:
                            read_resp = client.get("/api/v1/content/read", params={"uri": file_uri})
                            read_result = self._unwrap_result(read_resp)
                            if isinstance(read_result, str):
                                fb_content = read_result
                            elif isinstance(read_result, dict):
                                fb_content = read_result.get("content", "") or read_result.get("text", "")
                            else:
                                fb_content = ""
                            if fb_content and fb_content.strip():
                                if len(fb_content) > max_chars_per_file:
                                    fb_content = fb_content[:max_chars_per_file] + "\n\n[... truncated]"
                                display_name = d["rel_path"]
                                parts.append(f"### {display_name}/ (preview)\n{fb_content.strip()}")
                                files_loaded += 1
                                break
                        except Exception:
                            pass

        # Process files: read L2 but truncate
        for parent, files in files_by_parent.items():
            for file_uri in files:
                if files_loaded >= max_files:
                    break
                if file_uri in seen_uris:
                    continue
                seen_uris.add(file_uri)

                try:
                    resp = client.get("/api/v1/content/read", params={"uri": file_uri})
                    result = self._unwrap_result(resp)
                    if isinstance(result, str):
                        content = result
                    elif isinstance(result, dict):
                        content = result.get("content", "") or result.get("text", "")
                    else:
                        content = ""
                    if content and content.strip():
                        if len(content) > max_chars_per_file:
                            content = content[:max_chars_per_file] + "\n\n[... truncated]"
                        # Extract rel_path from URI for display
                        rel_path = file_uri.replace(scope_uri.rstrip("/") + "/", "")
                        parts.append(f"### {rel_path}\n{content.strip()}")
                        files_loaded += 1
                except Exception as e:
                    logger.debug("OpenViking prefetch: read failed for %s: %s", file_uri, e)

        if not parts:
            return ""

        return f"## {label}\n" + "\n\n".join(parts)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Fire OpenClaw-style background memory recall for the next turn."""
        query = self._prepare_recall_query(query)
        if not self._client or not self._auto_recall or len(query) < 5:
            return

        def _run():
            try:
                client = _client_class()(
                    self._endpoint, self._api_key,
                    account=self._account, user=self._user, agent=self._agent,
                )
                recall_block = self._build_auto_recall_context(
                    client,
                    query,
                    session_id=session_id or self._session_id,
                )
                if recall_block:
                    with self._prefetch_lock:
                        self._prefetch_result = recall_block

            except Exception as e:
                logger.debug("OpenViking prefetch failed: %s", e)

        self._prefetch_thread = threading.Thread(
            target=_run, daemon=True, name="openviking-prefetch"
        )
        self._prefetch_thread.start()

    @staticmethod
    def _prepare_recall_query(query: str) -> str:
        return re.sub(r"\s+", " ", (query or "").replace("\x00", " ")).strip()[:4000]

    def _build_auto_recall_context(
        self,
        client: _VikingClient,
        query: str,
        *,
        session_id: str,
    ) -> str:
        sections: List[str] = []
        session_context = self._fetch_session_context(client, session_id, token_budget=32000)
        session_block = self._format_session_context_block(
            session_context,
            include_active_messages=False,
        )
        if session_block:
            sections.append(session_block)

        candidate_limit = max(self._recall_limit * 4, 20)
        try:
            resp = client.post("/api/v1/search/search", {
                "query": query,
                "session_id": session_id,
                "limit": candidate_limit,
                "include_provenance": True,
            })
        except Exception as e:
            logger.debug("OpenViking prefetch search failed: %s", e)
            return self._wrap_injected_context(sections)

        result = self._unwrap_result(resp)
        if not isinstance(result, dict):
            result = {}

        candidates: List[Dict[str, Any]] = []
        buckets = ["memories", "skills"]
        if self._recall_resources:
            buckets.append("resources")
        for ctx_type in buckets:
            for item in result.get(ctx_type, []) or []:
                if not isinstance(item, dict):
                    continue
                uri = item.get("uri", "")
                if not uri:
                    continue
                raw_score = item.get("score")
                score = raw_score if isinstance(raw_score, (int, float)) else 0.0
                if score < self._recall_score_threshold:
                    continue
                level = item.get("level")
                is_leaf = item.get("is_leaf")
                if is_leaf is False:
                    continue
                if level not in (None, 2, "2"):
                    continue
                entry = dict(item)
                entry["score"] = score
                entry["context_type"] = item.get("context_type") or {
                    "memories": "memory",
                    "resources": "resource",
                    "skills": "skill",
                }.get(ctx_type, ctx_type.rstrip("s"))
                candidates.append(entry)

        deduped: Dict[str, Dict[str, Any]] = {}
        for item in candidates:
            uri = item.get("uri", "")
            prev = deduped.get(uri)
            if prev is None or item.get("score", 0.0) > prev.get("score", 0.0):
                deduped[uri] = item
        ranked = sorted(
            deduped.values(),
            key=lambda item: (item.get("score", 0.0), bool(item.get("abstract"))),
            reverse=True,
        )[: self._recall_limit]

        if not ranked or self._recall_max_injected_chars <= 0:
            if not sections:
                return ""
            return self._wrap_injected_context(sections)

        used = 0
        lines = ["Relevant recalled context:"]
        selected = 0
        context_parts: List[Dict[str, Any]] = []
        for item in ranked:
            uri = item.get("uri", "")
            abstract = str(item.get("abstract") or item.get("content") or "").strip()
            text = abstract
            if not text:
                try:
                    read_resp = client.get("/api/v1/content/read", params={"uri": uri})
                    read_result = self._unwrap_result(read_resp)
                    if isinstance(read_result, str):
                        text = read_result.strip()
                    elif isinstance(read_result, dict):
                        text = (read_result.get("content", "") or read_result.get("text", "")).strip()
                except Exception:
                    text = ""
            if not text:
                continue
            entry = f"- [{item.get('score', 0.0):.2f}] [{item.get('context_type', 'memory')}] {text} ({uri})"
            if used + len(entry) > self._recall_max_injected_chars:
                continue
            lines.append(entry)
            used += len(entry)
            selected += 1
            context_parts.append({
                "type": "context",
                "uri": uri,
                "context_type": item.get("context_type", "memory"),
                "abstract": text[:500],
            })
        if selected == 0:
            if not sections:
                return ""
            return self._wrap_injected_context(sections)
        sections.append("\n".join(lines))
        with self._capture_lock:
            self._pending_context_parts = context_parts
        return self._wrap_injected_context(sections)

    def _fetch_session_context(
        self,
        client: _VikingClient,
        session_id: str,
        *,
        token_budget: int,
    ) -> Dict[str, Any]:
        if not session_id:
            return {}
        try:
            resp = client.get(
                f"/api/v1/sessions/{_url_path_part(session_id)}/context",
                params={"token_budget": token_budget},
            )
            result = self._unwrap_result(resp)
            return result if isinstance(result, dict) else {}
        except Exception as exc:
            logger.debug("OpenViking session context fetch failed: %s", exc)
            return {}

    def _format_session_context_block(
        self,
        session_context: Dict[str, Any],
        *,
        include_active_messages: bool,
    ) -> str:
        sections: List[str] = []
        latest_archive = str(session_context.get("latest_archive_overview") or "").strip()
        if latest_archive:
            sections.append("Session archive overview:\n" + latest_archive)

        abstracts: List[str] = []
        for archive in session_context.get("pre_archive_abstracts") or []:
            if not isinstance(archive, dict):
                continue
            archive_id = archive.get("archive_id") or "archive"
            abstract = str(archive.get("abstract") or "").strip()
            if abstract:
                abstracts.append(f"[{archive_id}] {abstract}")
        if abstracts:
            sections.append("Older archive abstracts:\n" + "\n".join(abstracts))

        if include_active_messages:
            active_messages = [
                self._format_context_message(message)
                for message in session_context.get("messages") or []
                if isinstance(message, dict)
            ]
            active_messages = [message for message in active_messages if message]
            if active_messages:
                sections.append("Active session messages:\n" + "\n".join(active_messages))
        return "\n\n".join(sections)

    @staticmethod
    def _format_context_message(message: Dict[str, Any]) -> str:
        role = message.get("role", "")
        chunks: List[str] = []
        for part in message.get("parts") or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and part.get("text"):
                chunks.append(str(part["text"]))
            elif part.get("type") == "context" and part.get("abstract"):
                chunks.append(f"[context:{part.get('uri', '')}] {part['abstract']}")
            elif part.get("type") == "tool" and part.get("tool_output"):
                chunks.append(f"[tool:{part.get('tool_name', '')}] {part['tool_output']}")
        text = _strip_relevant_memories("\n".join(chunks))
        return f"{role}: {text}" if text else ""

    @staticmethod
    def _wrap_injected_context(sections: List[str]) -> str:
        body = "\n\n".join(section for section in sections if section.strip()).strip()
        if not body:
            return ""
        return "\n".join([
            _OPENVIKING_CONTEXT_OPEN,
            _AUTO_RECALL_SOURCE_MARKER,
            body,
            _OPENVIKING_CONTEXT_CLOSE,
        ])

    def _capture_payloads_from_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        payloads: List[Dict[str, Any]] = []
        signatures: List[str] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role", "")
            if role == "system" or message.get("_thinking_prefill") or message.get("_empty_recovery_synthetic"):
                continue
            converted = self._message_to_openviking_payload(message)
            if not converted:
                continue
            payloads.append(converted)
            signatures.append(self._capture_signature(self._signature_payload(converted)))

        with self._capture_lock:
            previous = list(self._capture_signatures)
            first_changed = 0
            while (
                first_changed < len(previous)
                and first_changed < len(signatures)
                and previous[first_changed] == signatures[first_changed]
            ):
                first_changed += 1
            if first_changed == len(previous) == len(signatures):
                self._pending_context_parts = []
                return []
            self._capture_signatures = signatures
        return payloads[first_changed:]

    def _message_to_openviking_payload(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        role = message.get("role", "")
        if role == "tool":
            return {
                "role": "assistant",
                "parts": [self._tool_result_part(message)],
            }
        if role not in {"user", "assistant"}:
            return None

        parts: List[Dict[str, Any]] = []
        text = _strip_relevant_memories(self._message_text(message))[:4000]
        if text:
            parts.append({"type": "text", "text": text})
        elif role == "user":
            parts.append({"type": "text", "text": ""})

        if role == "assistant":
            for tool_call in message.get("tool_calls") or []:
                part = self._tool_call_part(tool_call)
                if part:
                    parts.append(part)
            with self._capture_lock:
                if self._pending_context_parts:
                    parts.extend(self._pending_context_parts)
                    self._pending_context_parts = []

        if not parts:
            return None
        return {"role": role, "parts": parts}

    @staticmethod
    def _message_text(message: Dict[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks: List[str] = []
            for block in content:
                if isinstance(block, str):
                    chunks.append(block)
                elif isinstance(block, dict):
                    if block.get("type") == "text" and isinstance(block.get("text"), str):
                        chunks.append(block["text"])
                    elif isinstance(block.get("content"), str):
                        chunks.append(block["content"])
            return "\n".join(chunks)
        return "" if content is None else str(content)

    @staticmethod
    def _tool_call_part(tool_call: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(tool_call, dict):
            return None
        function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
        name = tool_call.get("name") or function.get("name") or ""
        raw_args = tool_call.get("args")
        if raw_args is None:
            raw_args = tool_call.get("arguments")
        if raw_args is None:
            raw_args = function.get("arguments")
        tool_input: Any
        if isinstance(raw_args, str):
            try:
                tool_input = json.loads(raw_args) if raw_args else {}
            except Exception:
                tool_input = {"value": raw_args}
        elif isinstance(raw_args, dict):
            tool_input = raw_args
        elif raw_args is None:
            tool_input = {}
        else:
            tool_input = {"value": raw_args}
        return {
            "type": "tool",
            "tool_id": str(tool_call.get("id") or tool_call.get("call_id") or ""),
            "tool_name": str(name),
            "tool_input": tool_input,
            "tool_status": "pending",
        }

    @classmethod
    def _tool_result_part(cls, message: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "type": "tool",
            "tool_id": str(message.get("tool_call_id") or message.get("id") or ""),
            "tool_name": str(message.get("name") or message.get("tool_name") or ""),
            "tool_output": cls._message_text(message)[:4000],
            "tool_status": "error" if message.get("status") == "error" else "completed",
        }

    @staticmethod
    def _capture_signature(payload: Dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)

    @staticmethod
    def _signature_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Signature only the Hermes message state, not injected recall refs."""
        clone = {
            "role": payload.get("role"),
            "parts": [
                part for part in payload.get("parts", [])
                if not (isinstance(part, dict) and part.get("type") == "context")
            ],
        }
        return clone

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        contexts: Optional[List[Dict[str, str]]] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Record the conversation turn in OpenViking's session (non-blocking).

        Aligned with OpenViking session.add_message() API:
        - Uses "parts" array instead of flat "content" string
        - Supports TextPart, ContextPart, ToolPart structures
        - Includes ContextPart for retrieved resources/memories (improves retrieval ranking)
        - Trims very long messages to 4000 chars
        """
        if not self._client or not self._auto_capture:
            return

        self._turn_count += 1
        sid = session_id or self._session_id

        if messages:
            payloads = self._capture_payloads_from_messages(messages)
            if not payloads:
                with self._capture_lock:
                    self._pending_context_parts = []
                return
        else:
            clean_user_content = _strip_relevant_memories(user_content)[:4000]
            clean_assistant_content = _strip_relevant_memories(assistant_content)[:4000]
            user_parts = [{"type": "text", "text": clean_user_content}]
            asst_parts = [{"type": "text", "text": clean_assistant_content}]
            if contexts:
                for ctx in contexts:
                    if isinstance(ctx, dict) and ctx.get("uri"):
                        asst_parts.append({
                            "type": "context",
                            "uri": ctx["uri"],
                            "context_type": ctx.get("context_type", "resource"),
                            "abstract": ctx.get("abstract", "")[:500],
                        })
            payloads = [
                {"role": "user", "parts": user_parts},
                {"role": "assistant", "parts": asst_parts},
            ]
        queue_path = self._enqueue_session_sync(sid, payloads)

        def _sync():
            try:
                client = _client_class()(
                    self._endpoint, self._api_key,
                    account=self._account, user=self._user, agent=self._agent,
                )
                posted = self._drain_session_sync_queue(client, limit=_SESSION_SYNC_DRAIN_LIMIT)
                logger.debug(
                    "OpenViking sync_turn: session=%s drained_messages=%s queued=%s",
                    sid,
                    posted,
                    queue_path.name,
                )
            except Exception as e:
                with self._capture_lock:
                    self._pending_context_parts = []
                logger.warning("OpenViking sync_turn failed: %s", e)

        # Wait for any previous sync to finish before starting a new one
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)

        self._sync_thread = threading.Thread(
            target=_sync, daemon=True, name="openviking-sync"
        )
        self._sync_thread.start()

    def _session_sync_queue_dir(self) -> Path:
        return get_hermes_home() / _SESSION_SYNC_QUEUE_DIRNAME

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _enqueue_session_sync(self, session_id: str, payloads: List[Dict[str, Any]]) -> Path:
        queue_dir = self._session_sync_queue_dir()
        queue_dir.mkdir(parents=True, exist_ok=True)
        now = self._utc_now_iso()
        name = f"{int(time.time() * 1000)}-{uuid.uuid4().hex}.json"
        path = queue_dir / name
        record = {
            "version": "openviking_session_sync_queue.v1",
            "session_id": session_id,
            "payloads": payloads,
            "attempts": 0,
            "created_at": now,
            "updated_at": now,
            "last_error": "",
        }
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        tmp_path.replace(path)
        return path

    def _load_session_sync_record(self, path: Path) -> Dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    def _mark_session_sync_failed(self, path: Path, record: Dict[str, Any], error: Exception) -> None:
        record["attempts"] = int(record.get("attempts") or 0) + 1
        record["updated_at"] = self._utc_now_iso()
        record["last_error"] = str(error)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        tmp_path.replace(path)

    def _send_session_sync_record(self, client: _VikingClient, record: Dict[str, Any]) -> int:
        session_id = str(record.get("session_id") or self._session_id)
        sid_part = _url_path_part(session_id)
        payloads = [payload for payload in record.get("payloads") or [] if isinstance(payload, dict)]
        posted = 0
        for payload in payloads:
            client.post(f"/api/v1/sessions/{sid_part}/messages", payload)
            posted += 1
        context_uris = [
            part["uri"]
            for payload in payloads
            for part in payload.get("parts", [])
            if isinstance(part, dict) and part.get("type") == "context" and part.get("uri")
        ]
        if context_uris:
            client.post(
                f"/api/v1/sessions/{sid_part}/used",
                {"contexts": list(dict.fromkeys(context_uris))},
            )
        self._commit_if_threshold_reached(client, session_id)
        return posted

    def _drain_session_sync_queue(self, client: _VikingClient, *, limit: int = _SESSION_SYNC_DRAIN_LIMIT) -> int:
        queue_dir = self._session_sync_queue_dir()
        if not queue_dir.exists():
            return 0
        posted = 0
        processed = 0
        for path in sorted(queue_dir.glob("*.json")):
            if processed >= limit:
                break
            processed += 1
            record: Dict[str, Any] = {"version": "openviking_session_sync_queue.v1"}
            try:
                record = self._load_session_sync_record(path)
                posted += self._send_session_sync_record(client, record)
                path.unlink(missing_ok=True)
            except Exception as exc:
                try:
                    self._mark_session_sync_failed(path, record, exc)
                except Exception:
                    logger.warning("OpenViking session sync queue update failed for %s", path, exc_info=True)
                logger.warning("OpenViking session sync queue item failed: %s", exc)
        return posted

    def _commit_if_threshold_reached(self, client: _VikingClient, session_id: str) -> None:
        self._maybe_commit_session(
            client,
            session_id,
            mode="pending_tokens",
            pending_token_threshold=self._commit_token_threshold,
            keep_recent_count=self._commit_keep_recent_count,
            reason="threshold",
        )

    def _maybe_commit_session(
        self,
        client: _VikingClient,
        session_id: str,
        *,
        mode: str,
        pending_token_threshold: int,
        keep_recent_count: int,
        reason: str,
    ) -> Optional[Dict[str, Any]]:
        if mode == "never":
            return None
        if mode == "always":
            return self._commit_session(
                wait=False,
                keep_recent_count=keep_recent_count,
                reason=reason,
                client=client,
                session_id=session_id,
            )
        if mode != "pending_tokens":
            raise ValueError(f"Unsupported OpenViking commit policy: {mode}")
        if pending_token_threshold <= 0:
            return None
        try:
            sid_part = _url_path_part(session_id)
            resp = client.get(f"/api/v1/sessions/{sid_part}")
            result = self._unwrap_result(resp)
            pending_tokens = result.get("pending_tokens", 0) if isinstance(result, dict) else 0
            if not isinstance(pending_tokens, (int, float)):
                pending_tokens = 0
            if pending_tokens < pending_token_threshold:
                return None
            return self._commit_session(
                wait=False,
                keep_recent_count=keep_recent_count,
                reason=reason,
                client=client,
                session_id=session_id,
            )
        except Exception as e:
            logger.debug("OpenViking pending-token commit skipped: %s", e)
            return None

    def _commit_session(
        self,
        *,
        wait: bool,
        keep_recent_count: int,
        reason: str,
        client: Optional[_VikingClient] = None,
        session_id: str = "",
    ) -> Dict[str, Any]:
        commit_client = client or self._client
        if not commit_client:
            return {}
        sid = session_id or self._session_id
        sid_part = _url_path_part(sid)
        payload: Dict[str, Any] = {}
        if keep_recent_count > 0:
            payload["keep_recent_count"] = keep_recent_count
        resp = commit_client.post(f"/api/v1/sessions/{sid_part}/commit", payload)
        result = self._unwrap_result(resp)
        if not isinstance(result, dict):
            result = {}
        task_id = result.get("task_id")
        logger.info(
            "OpenViking session %s committed reason=%s archived=%s task=%s keep_recent=%s",
            sid,
            reason,
            result.get("archived", False),
            task_id,
            keep_recent_count,
        )
        if wait and task_id:
            task_result = self._poll_commit_task(task_id, max_wait=30, client=commit_client)
            if task_result:
                result.update(task_result)
        return result

    def record_usage(self, contexts: Optional[List[str]] = None, skill: Optional[Dict[str, Any]] = None) -> None:
        """Record context/skill usage to OpenViking session.

        Aligned with OpenViking session.used() API:
        - contexts: List of URIs that were used (e.g., ["viking://user/memories/profile.md"])
        - skill: Dict with uri, input, output, success fields

        This helps OpenViking track active_count and skill effectiveness.
        """
        if not self._client:
            return
        if not contexts and not skill:
            return

        def _record():
            try:
                client = _client_class()(
                    self._endpoint, self._api_key,
                    account=self._account, user=self._user, agent=self._agent,
                )
                payload = {}
                if contexts:
                    payload["contexts"] = contexts
                if skill:
                    payload["skill"] = skill

                client.post(f"/api/v1/sessions/{self._session_id}/used", payload)
                logger.debug(
                    "OpenViking record_usage: session=%s contexts=%d skill=%s",
                    self._session_id,
                    len(contexts) if contexts else 0,
                    skill.get("uri") if skill else "none",
                )
            except Exception as e:
                logger.debug("OpenViking record_usage failed: %s", e)

        t = threading.Thread(target=_record, daemon=True, name="openviking-usage")
        t.start()

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Commit the session to trigger OpenViking archive + memory extraction."""
        if not self._client:
            return

        # Wait for any pending sync to finish first
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=10.0)

        if self._turn_count == 0:
            return

        try:
            self._log_session_context(self._session_id, token_budget=32000)
            self._commit_session(
                wait=False,
                keep_recent_count=0,
                reason="session_end",
            )
        except Exception as e:
            logger.warning("OpenViking session commit failed: %s", e)

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Commit with keep_recent_count=0 before Hermes discards old context."""
        if not self._client:
            return ""
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=10.0)
        try:
            result = self._commit_session(
                wait=True,
                keep_recent_count=0,
                reason="pre_compress",
            )
            archive_uri = result.get("archive_uri", "") if isinstance(result, dict) else ""
            if archive_uri:
                return f"OpenViking archived pre-compression history at {archive_uri}."
        except Exception as e:
            logger.warning("OpenViking pre-compress commit failed: %s", e)
        return ""

    def _log_session_context(self, session_id: str, *, token_budget: int) -> None:
        try:
            sid_part = _url_path_part(session_id)
            ctx_resp = self._client.get(
                f"/api/v1/sessions/{sid_part}/context",
                params={"token_budget": token_budget},
            )
            ctx_result = self._unwrap_result(ctx_resp)
            if not isinstance(ctx_result, dict):
                return
            estimated_tokens = ctx_result.get("estimatedTokens", 0)
            msg_count = len(ctx_result.get("messages", []))
            logger.debug(
                "OpenViking session context before commit: tokens=%s messages=%d",
                estimated_tokens,
                msg_count,
            )
        except Exception:
            pass

    def _poll_commit_task(
        self,
        task_id: str,
        max_wait: int = 6,
        *,
        client: Optional[_VikingClient] = None,
    ) -> Dict[str, Any]:
        """Poll commit task status until completion or timeout.

        Logs extraction results and memory diff for observability.
        """
        poll_client = client or self._client
        if not poll_client:
            return {}
        start = time.time()
        while time.time() - start < max_wait:
            try:
                resp = poll_client.get(f"/api/v1/tasks/{task_id}")
                result = resp.get("result", {})
                status = result.get("status", "unknown")

                if status == "completed":
                    task_result = result.get("result") if isinstance(result.get("result"), dict) else {}
                    mem_extracted = (
                        task_result.get("memories_extracted")
                        or result.get("memories_extracted")
                        or {}
                    )
                    total = sum(mem_extracted.values()) if isinstance(mem_extracted, dict) else 0
                    archive_uri = task_result.get("archive_uri") or result.get("archive_uri", "")

                    # Try to read memory_diff.json for audit trail
                    memory_diff = None
                    if archive_uri:
                        try:
                            diff_resp = poll_client.get(
                                "/api/v1/content/read",
                                params={"uri": f"{archive_uri}/memory_diff.json"}
                            )
                            diff_result = self._unwrap_result(diff_resp)
                            if isinstance(diff_result, str):
                                import json
                                memory_diff = json.loads(diff_result)
                            elif isinstance(diff_result, dict):
                                memory_diff = diff_result
                        except Exception:
                            pass

                    if memory_diff:
                        ops = memory_diff.get("operations", {})
                        adds = len(ops.get("adds", []))
                        updates = len(ops.get("updates", []))
                        deletes = len(ops.get("deletes", []))
                        logger.info(
                            "OpenViking extraction completed: task=%s memories=%s total=%d "
                            "diff=(+%d ~%d -%d)",
                            task_id, mem_extracted, total, adds, updates, deletes
                        )
                    else:
                        logger.info(
                        "OpenViking extraction completed: task=%s memories=%s total=%d",
                        task_id, mem_extracted, total
                    )
                    return task_result or result
                elif status == "failed":
                    logger.warning(
                        "OpenViking extraction failed: task=%s error=%s",
                        task_id, result.get("error", "unknown")
                    )
                    return result if isinstance(result, dict) else {}
                elif status in ("pending", "running", "processing"):
                    time.sleep(1)
                    continue
                else:
                    # Unknown status, stop polling
                    return result if isinstance(result, dict) else {}
            except Exception as e:
                logger.debug("OpenViking task poll failed: %s", e)
                return {}
        return {"status": "timeout", "task_id": task_id}

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mirror built-in memory writes to OpenViking as explicit memories."""
        if not self._client or action not in {"add", "replace"} or not content:
            return
        metadata = metadata or {}
        origin = str(metadata.get("write_origin") or "")
        context = str(metadata.get("execution_context") or "")
        if not origin:
            origin = self._agent_context if self._agent_context != "primary" else "assistant_tool"
        if not context:
            context = self._agent_context if self._agent_context != "primary" else "foreground"

        signature = json.dumps(
            {
                "session_id": self._session_id,
                "origin": origin,
                "context": context,
                "action": action,
                "target": target,
                "content": content,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        with self._capture_lock:
            if signature in self._memory_write_signatures:
                return
            self._memory_write_signatures.add(signature)

        def _write():
            try:
                client = _client_class()(
                    self._endpoint, self._api_key,
                    account=self._account, user=self._user, agent=self._agent,
                )
                if origin == "background_review" or context == "background_review":
                    label = f"[Self-evolution memory - {target}]"
                else:
                    label = f"[Memory note - {target}]"
                text = (
                    f"{label} {content}\n"
                    f"Source: {origin}; context: {context}; action: {action}"
                )
                # Add as a user message with memory context so the commit
                # picks it up as an explicit memory during extraction
                client.post(f"/api/v1/sessions/{self._session_id}/messages", {
                    "role": "user",
                    "parts": [
                        {"type": "text", "text": text},
                    ],
                })
            except Exception as e:
                logger.debug("OpenViking memory mirror failed: %s", e)

        t = threading.Thread(target=_write, daemon=True, name="openviking-memwrite")
        t.start()

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        **kwargs,
    ) -> None:
        """Keep OpenViking writes aligned when Hermes rotates session IDs."""
        if not new_session_id:
            return
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=10.0)
        self._session_id = new_session_id
        if reset:
            self._turn_count = 0
        if not self._client:
            return
        try:
            self._client.post("/api/v1/sessions", {"session_id": new_session_id})
        except Exception as e:
            err_str = str(e).lower()
            if not ("exist" in err_str or "duplicate" in err_str or "already" in err_str):
                logger.debug("OpenViking session switch create failed: %s", e)

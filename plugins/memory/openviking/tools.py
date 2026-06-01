"""OpenViking tool schema routing and handlers."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from tools.registry import tool_error

from .schemas import get_openviking_tool_schemas
from .utils import (
    _agent_scope_uri,
    _context_type_for_uri,
    _is_local_path_reference,
    _is_remote_resource_source,
    _is_resource_uri,
    _is_windows_absolute_path,
    _path_from_file_uri,
    _skill_summary_from_result,
    _url_path_part,
    _zip_directory,
)

logger = logging.getLogger(__name__)

_LOCAL_CONTRACT_FIELDS = (
    "when_to_use",
    "when_not_to_use",
    "required_before_call",
    "next_action",
    "dangerous_misroutes",
    "write_policy",
    "recovery_hint",
)


def _input_error(
    message: str,
    *,
    required_before_call: list[str],
    next_action: str,
    recovery_hint: str,
    error_code: str = "invalid_input",
) -> str:
    return tool_error(
        message,
        error_code=error_code,
        required_before_call=required_before_call,
        next_action=next_action,
        recovery_hint=recovery_hint,
    )


class OpenVikingToolMixin:
    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return get_openviking_tool_schemas()

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if not self._client:
            return self._attach_tool_contract(tool_name, tool_error("OpenViking server not connected"))

        try:
            if tool_name == "viking_search":
                return self._attach_tool_contract(tool_name, self._tool_search(args))
            elif tool_name == "viking_read":
                return self._attach_tool_contract(tool_name, self._tool_read(args))
            elif tool_name == "viking_browse":
                return self._attach_tool_contract(tool_name, self._tool_browse(args))
            elif tool_name == "viking_fs":
                return self._attach_tool_contract(tool_name, self._tool_fs(args))
            elif tool_name == "viking_remember":
                return self._attach_tool_contract(tool_name, self._tool_remember(args))
            elif tool_name == "viking_write":
                return self._attach_tool_contract(tool_name, self._tool_write(args))
            elif tool_name == "viking_link":
                return self._attach_tool_contract(tool_name, self._tool_link(args))
            elif tool_name == "viking_relations":
                return self._attach_tool_contract(tool_name, self._tool_relations(args))
            elif tool_name == "viking_find":
                return self._attach_tool_contract(tool_name, self._tool_find(args))
            elif tool_name == "viking_grep":
                return self._attach_tool_contract(tool_name, self._tool_grep(args))
            elif tool_name == "viking_glob":
                return self._attach_tool_contract(tool_name, self._tool_glob(args))
            elif tool_name == "viking_add_resource":
                return self._attach_tool_contract(tool_name, self._tool_add_resource(args))
            elif tool_name == "viking_archive":
                return self._attach_tool_contract(tool_name, self._tool_archive(args))
            elif tool_name == "viking_add_skill":
                return self._attach_tool_contract(tool_name, self._tool_add_skill(args))
            elif tool_name == "viking_sync_skills":
                return self._attach_tool_contract(tool_name, self._tool_sync_skills(args))
            elif tool_name == "viking_system":
                return self._attach_tool_contract(tool_name, self._tool_system(args))
            elif tool_name == "viking_admin":
                return self._attach_tool_contract(tool_name, self._tool_admin(args))
            elif tool_name == "viking_consistency":
                return self._attach_tool_contract(tool_name, self._tool_consistency(args))
            elif tool_name == "viking_reindex":
                return self._attach_tool_contract(tool_name, self._tool_reindex(args))
            elif tool_name == "viking_watch":
                return self._attach_tool_contract(tool_name, self._tool_watch(args))
            return tool_error(f"Unknown tool: {tool_name}")
        except Exception as e:
            return self._attach_tool_contract(tool_name, tool_error(str(e)))

    def shutdown(self) -> None:
        # Wait for background threads to finish
        for t in (self._sync_thread, self._prefetch_thread):
            if t and t.is_alive():
                t.join(timeout=5.0)
        # Clear atexit reference so it doesn't double-commit
        global _last_active_provider
        if _last_active_provider is self:
            _last_active_provider = None

    # -- Tool implementations ------------------------------------------------

    @staticmethod
    def _unwrap_result(resp: Any) -> Any:
        """Return OpenViking payload body regardless of wrapped/unwrapped shape."""
        if isinstance(resp, dict) and "result" in resp:
            return resp.get("result")
        return resp

    def _attach_tool_contract(self, tool_name: str, raw: str) -> str:
        schemas = {schema.get("name"): schema for schema in self.get_tool_schemas()}
        schema = schemas.get(tool_name)
        if not schema:
            return raw
        try:
            payload = json.loads(raw)
        except Exception:
            return raw
        if not isinstance(payload, dict):
            return raw
        contract = {field: schema.get(field) for field in _LOCAL_CONTRACT_FIELDS if schema.get(field)}
        if contract:
            payload.setdefault("tool_contract", contract)
            payload.setdefault("write_policy", contract.get("write_policy"))
            payload.setdefault("recovery_hint", contract.get("recovery_hint"))
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _normalize_summary_uri(uri: str) -> str:
        """Map pseudo summary files to their parent directory URI for L0/L1 reads."""
        if not uri:
            return uri
        for suffix in ("/.abstract.md", "/.overview.md", "/.read.md", "/.full.md"):
            if uri.endswith(suffix):
                return uri[: -len(suffix)] or "viking://"
        return uri

    def _is_directory_uri(self, uri: str) -> bool | None:
        """Probe fs/stat to decide if a URI is a directory.

        Returns True/False when the server answers cleanly, and None when the
        probe itself fails (network error, unexpected shape). Callers should
        treat None as "unknown" and fall back to the exception-based path.
        """
        try:
            resp = self._client.get("/api/v1/fs/stat", params={"uri": uri})
        except Exception:
            return None
        result = self._unwrap_result(resp)
        if isinstance(result, dict):
            if "isDir" in result:
                return bool(result.get("isDir"))
            if "is_dir" in result:
                return bool(result.get("is_dir"))
            if result.get("type") == "dir":
                return True
            if result.get("type") == "file":
                return False
        return None

    def _tool_search(self, args: dict) -> str:
        query = args.get("query", "")
        if not query:
            return _input_error(
                "query is required",
                required_before_call=["query"],
                next_action="Call viking_search with a semantic query, or use viking_glob for path matching.",
                recovery_hint="OpenViking search is recall-only; confirm structured facts through factmemory before writes.",
                error_code="missing_query",
            )

        payload: Dict[str, Any] = {"query": query}
        strategy = args.get("strategy", "find")
        if strategy not in ("find", "search"):
            return _input_error(
                "strategy must be 'find' or 'search'",
                required_before_call=["strategy omitted, 'find', or 'search'"],
                next_action="Use strategy='find' for simple recall or strategy='search' for session-aware rerank.",
                recovery_hint="Do not invent other strategy values; use mode/level/scope to tune retrieval.",
                error_code="invalid_strategy",
            )
        mode = args.get("mode", "auto")
        if strategy == "find" and mode != "auto":
            payload["mode"] = mode
        if args.get("scope"):
            payload["target_uri"] = args["scope"]
        if args.get("limit"):
            payload["limit"] = args["limit"]
        if args.get("level"):
            payload["level"] = args["level"]
        for key in (
            "score_threshold",
            "node_limit",
            "since",
            "until",
            "time_field",
            "include_provenance",
            "telemetry",
        ):
            if key in args and args[key] is not None:
                payload[key] = args[key]
        if strategy == "search":
            payload["session_id"] = args.get("session_id") or self._session_id

        endpoint = "/api/v1/search/search" if strategy == "search" else "/api/v1/search/find"
        resp = self._client.post(endpoint, payload)
        result = self._unwrap_result(resp)
        if not isinstance(result, dict):
            result = {}

        # Format results for the model — keep it concise
        scored_entries = []
        for ctx_type in ("memories", "resources", "skills"):
            items = result.get(ctx_type, [])
            for item in items:
                if not isinstance(item, dict):
                    continue
                raw_score = item.get("score")
                score = raw_score if isinstance(raw_score, (int, float)) else 0.0
                entry = {
                    "uri": item.get("uri", ""),
                    "type": item.get("type") or item.get("context_type") or ctx_type.rstrip("s"),
                    "context_type": item.get("context_type") or _context_type_for_uri(item.get("uri", ""), ctx_type.rstrip("s")),
                    "score": round(score, 3),
                    "abstract": item.get("abstract", ""),
                }
                for key in ("is_leaf", "category", "match_reason", "level"):
                    if key in item:
                        entry[key] = item.get(key)
                if item.get("relations") is not None:
                    entry["relations"] = item.get("relations") or []
                    entry["related"] = [
                        r.get("uri") for r in entry["relations"][:3]
                        if isinstance(r, dict) and r.get("uri")
                    ]
                for key in ("provenance", "metadata"):
                    if key in item:
                        entry[key] = item.get(key)
                scored_entries.append((score, entry))

        scored_entries.sort(key=lambda x: x[0], reverse=True)
        formatted = [entry for _, entry in scored_entries]

        output = {
            "strategy": strategy,
            "results": formatted,
            "total": result.get("total", len(formatted)),
        }
        if args.get("packet", True) is not False:
            output["packet"] = self._build_search_packet(
                query=query,
                strategy=strategy,
                results=formatted,
                total=output["total"],
            )
        for key in ("query_plan", "query_results"):
            if key in result:
                output[key] = result.get(key)

        return json.dumps(output, ensure_ascii=False)

    @classmethod
    def _build_search_packet(
        cls,
        *,
        query: str,
        strategy: str,
        results: List[Dict[str, Any]],
        total: int,
    ) -> Dict[str, Any]:
        sections: Dict[str, List[Dict[str, Any]]] = {
            "authoritative_candidates": [],
            "recall_only": [],
            "factmemory_mirrors": [],
            "governance": [],
        }
        for item in results:
            section = cls._search_packet_section(item)
            sections[section].append(cls._search_packet_item(item, section))

        sections["governance"].extend([
            cls._governance_packet_item(
                code="factmemory_authority_boundary",
                summary=(
                    "OpenViking search can suggest related memories and mirrored entity URIs, "
                    "but Project/Task/Fact/Problem truth stays in /home/moyu/workspace/.memory/entities/ "
                    "and memory/YYYY-MM-DD.md."
                ),
                confidence="high",
            ),
            cls._governance_packet_item(
                code="write_boundary",
                summary=(
                    "Do not write back from OpenViking results into fact-memory. Confirm with "
                    "factmemory_context/search and then use log -> stage -> commit for durable changes."
                ),
                confidence="high",
            ),
        ])

        return {
            "version": "openviking_retrieval_packet.v1",
            "query": query,
            "strategy": strategy,
            "total": total,
            "section_order": [
                "authoritative_candidates",
                "recall_only",
                "factmemory_mirrors",
                "governance",
            ],
            "sections": sections,
            "source_policy": (
                "OpenViking is retrieval and mirror infrastructure. Its results may improve recall "
                "and association judgment, but fact-memory entities and daily logs remain authoritative "
                "for structured project/task/fact/problem records."
            ),
            "write_policy": "retrieval_only_no_factmemory_writeback",
        }

    @classmethod
    def _search_packet_section(cls, item: Dict[str, Any]) -> str:
        uri = str(item.get("uri") or "")
        context_type = str(item.get("context_type") or item.get("type") or "").lower()
        if cls._is_factmemory_mirror_uri(uri, item):
            return "factmemory_mirrors"
        if context_type in {"resource", "skill"} or uri.startswith("viking://resources/") or "/skills/" in uri:
            return "authoritative_candidates"
        return "recall_only"

    @staticmethod
    def _is_factmemory_mirror_uri(uri: str, item: Dict[str, Any]) -> bool:
        lowered = uri.lower()
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        entity_id = str(metadata.get("entity_id") or item.get("entity_id") or "")
        return (
            "/memories/entities/" in lowered
            or "/factmemory/" in lowered
            or bool(re.match(r"^(project|task|fact|problem)/", entity_id))
        )

    @classmethod
    def _search_packet_item(cls, item: Dict[str, Any], section: str) -> Dict[str, Any]:
        score = item.get("score")
        if not isinstance(score, (int, float)):
            score = 0.0
        confidence = cls._confidence_from_score(float(score))
        policies = {
            "authoritative_candidates": {
                "authority": "openviking_authoritative_candidate",
                "source_policy": "OpenViking-owned resource or skill candidate; read before relying on details.",
                "write_policy": "openviking_read_or_domain_tooling_only",
            },
            "recall_only": {
                "authority": "openviking_recall_signal",
                "source_policy": "Semantic recall signal; useful for association discovery but not a confirmed fact.",
                "write_policy": "recall_only_no_direct_write",
            },
            "factmemory_mirrors": {
                "authority": "factmemory_mirror_not_source_of_truth",
                "source_policy": "Mirrored fact-memory entity; confirm against local .memory/entities before edits.",
                "write_policy": "confirm_with_factmemory_then_log_stage_commit",
            },
        }
        policy = policies[section]
        return {
            "source_section": section,
            "source_policy": policy["source_policy"],
            "authority": policy["authority"],
            "confidence": confidence,
            "write_policy": policy["write_policy"],
            "why_retrieved": item.get("match_reason") or f"Semantic score {float(score):.3f} for query.",
            "uri": item.get("uri", ""),
            "score": round(float(score), 3),
            "context_type": item.get("context_type") or item.get("type") or "",
            "title": item.get("title") or item.get("name") or "",
            "abstract": item.get("abstract", ""),
        }

    @staticmethod
    def _confidence_from_score(score: float) -> str:
        if score >= 0.75:
            return "high"
        if score >= 0.45:
            return "medium"
        return "low"

    @staticmethod
    def _governance_packet_item(*, code: str, summary: str, confidence: str) -> Dict[str, Any]:
        return {
            "source_section": "governance",
            "source_policy": "Tool-generated retrieval governance, not a retrieved fact.",
            "authority": "policy",
            "confidence": confidence,
            "write_policy": "read_only_policy",
            "why_retrieved": "Always attached to viking_search(packet=true).",
            "uri": "",
            "score": 0.0,
            "context_type": "governance",
            "code": code,
            "summary": summary,
        }

    def _tool_read(self, args: dict) -> str:
        uri = args.get("uri", "")
        if not uri:
            return _input_error(
                "uri is required",
                required_before_call=["viking:// uri from viking_search/browse/glob"],
                next_action="Find a URI with viking_search, viking_browse, or viking_glob, then call viking_read.",
                recovery_hint="Use document_read for registered document doc_id reads; use viking_read for raw viking:// URIs.",
                error_code="missing_uri",
            )

        level = args.get("level", "overview")

        summary_level = level in ("abstract", "overview")
        # OpenViking expects directory URIs for pseudo summary files
        # (e.g. viking://user/hermes/.overview.md).
        resolved_uri = self._normalize_summary_uri(uri) if summary_level else uri
        used_fallback = False

        # abstract/overview endpoints are directory-only on OpenViking
        # (v0.3.x returns 500/412 for file URIs). When the caller asks for a
        # summary level on a non-pseudo URI, probe fs/stat first and route
        # file URIs straight to /content/read instead of eating a failing
        # round-trip. The pseudo-URI path already points at a directory, so
        # skip the probe there.
        if summary_level and resolved_uri == uri:
            is_dir = self._is_directory_uri(uri)
            if is_dir is False:
                resolved_uri = uri
                used_fallback = True

        # Map our level names to OpenViking GET endpoints.
        endpoint = "/api/v1/content/read"
        if not used_fallback:
            if level == "abstract":
                endpoint = "/api/v1/content/abstract"
            elif level == "overview":
                endpoint = "/api/v1/content/overview"

        try:
            params: Dict[str, Any] = {"uri": resolved_uri}
            if endpoint == "/api/v1/content/read" and level == "full":
                for key in ("offset", "limit"):
                    if key in args and args[key] is not None:
                        params[key] = args[key]
            if args.get("raw") is not None:
                params["raw"] = bool(args["raw"])
            resp = self._client.get(endpoint, params=params)
        except Exception:
            # OpenViking may return HTTP 500 for abstract/overview reads on normal
            # file URIs (mem_*.md). For those, gracefully fallback to full read.
            if not summary_level or resolved_uri != uri or used_fallback:
                raise
            params = {"uri": uri}
            resp = self._client.get("/api/v1/content/read", params=params)
            used_fallback = True

        result = self._unwrap_result(resp)
        # Content endpoints may return either plain strings or objects.
        if isinstance(result, str):
            content = result
        elif isinstance(result, dict):
            content = result.get("content", "") or result.get("text", "")
        else:
            content = ""

        # Truncate long content to avoid flooding context.
        max_len = 8000
        if level == "overview":
            max_len = 4000
        elif level == "abstract":
            max_len = 1200

        if len(content) > max_len:
            content = content[:max_len] + "\n\n[... truncated, use a more specific URI or full level]"

        payload = {
            "uri": uri,
            "resolved_uri": resolved_uri,
            "level": level,
            "content": content,
        }
        if used_fallback:
            payload["fallback"] = "content/read"
            payload["requested_level"] = level

        return json.dumps(payload, ensure_ascii=False)

    def _tool_browse(self, args: dict) -> str:
        action = args.get("action", "list")
        path = args.get("path", "viking://")

        # Map action to the correct fs endpoint (all GET with uri= param)
        endpoint_map = {"tree": "/api/v1/fs/tree", "list": "/api/v1/fs/ls", "stat": "/api/v1/fs/stat"}
        endpoint = endpoint_map.get(action, "/api/v1/fs/ls")
        params: Dict[str, Any] = {"uri": path}
        if action == "list":
            for key in ("simple", "recursive"):
                if key in args and args[key] is not None:
                    params[key] = args[key]
        elif action == "tree":
            if args.get("level_limit") is not None:
                params["level_limit"] = args["level_limit"]
        resp = self._client.get(endpoint, params=params)
        result = self._unwrap_result(resp)

        # Format list/tree results for readability
        if action in ("list", "tree"):
            raw_entries = result
            if isinstance(result, dict):
                raw_entries = result.get("entries") or result.get("items") or result.get("children") or []

            if isinstance(raw_entries, list):
                entries = []
                for e in raw_entries[:50]:  # cap at 50 entries
                    uri = e.get("uri", "")
                    name = e.get("rel_path") or e.get("name") or (uri.rsplit("/", 1)[-1] if uri else "")
                    is_dir = bool(e.get("isDir") or e.get("is_dir") or e.get("type") == "dir")
                    entries.append({
                        "name": name,
                        "uri": uri,
                        "type": "dir" if is_dir else "file",
                        "abstract": e.get("abstract", ""),
                    })
                return json.dumps({"path": path, "entries": entries}, ensure_ascii=False)

        return json.dumps(result, ensure_ascii=False)

    def _tool_fs(self, args: dict) -> str:
        action = args.get("action", "")
        if action == "mkdir":
            uri = args.get("uri", "")
            if not uri:
                return _input_error(
                    "uri is required for mkdir",
                    required_before_call=["uri under viking://"],
                    next_action="Provide target viking:// URI for mkdir.",
                    recovery_hint="Browse/stat first if unsure; viking_fs mutates OpenViking AGFS only.",
                    error_code="missing_uri",
                )
            payload: Dict[str, Any] = {"uri": uri}
            if args.get("description"):
                payload["description"] = args["description"]
            result = self._unwrap_result(self._client.post("/api/v1/fs/mkdir", payload))
            return json.dumps({"status": "created", "action": action, "uri": uri, "result": result}, ensure_ascii=False)

        if action == "mv":
            from_uri = args.get("from_uri", "")
            to_uri = args.get("to_uri", "")
            if not from_uri:
                return _input_error(
                    "from_uri is required for mv",
                    required_before_call=["from_uri", "to_uri"],
                    next_action="Provide both source and destination viking:// URIs.",
                    recovery_hint="Use viking_browse/stat to verify the source before moving.",
                    error_code="missing_from_uri",
                )
            if not to_uri:
                return _input_error(
                    "to_uri is required for mv",
                    required_before_call=["from_uri", "to_uri"],
                    next_action="Provide destination viking:// URI.",
                    recovery_hint="Use viking_browse/stat to verify the destination parent before moving.",
                    error_code="missing_to_uri",
                )
            payload = {"from_uri": from_uri, "to_uri": to_uri}
            result = self._unwrap_result(self._client.post("/api/v1/fs/mv", payload))
            return json.dumps({
                "status": "moved",
                "action": action,
                "from_uri": from_uri,
                "to_uri": to_uri,
                "result": result,
            }, ensure_ascii=False)

        if action == "rm":
            uri = args.get("uri", "")
            if not uri:
                return _input_error(
                    "uri is required for rm",
                    required_before_call=["uri under viking:// and explicit owner intent"],
                    next_action="Provide target viking:// URI only after confirming removal is intended.",
                    recovery_hint="Browse/stat first; do not use viking_fs rm to clean factmemory source state.",
                    error_code="missing_uri",
                )
            payload = {"uri": uri, "recursive": bool(args.get("recursive", False))}
            result = self._unwrap_result(self._client.delete("/api/v1/fs/rm", payload))
            return json.dumps({
                "status": "removed",
                "action": action,
                "uri": uri,
                "recursive": payload["recursive"],
                "result": result,
            }, ensure_ascii=False)

        return _input_error(
            "action must be one of: mkdir, mv, rm",
            required_before_call=["action in mkdir|mv|rm"],
            next_action="Choose an explicit OpenViking AGFS maintenance action.",
            recovery_hint="Use viking_browse for read-only inspection before mutation.",
            error_code="invalid_action",
        )

    def _tool_remember(self, args: dict) -> str:
        content = args.get("content", "")
        content_path = args.get("content_path", "")
        if content and content_path:
            return tool_error("Provide exactly one of content or content_path")
        if content_path:
            try:
                path = Path(str(content_path)).expanduser()
                if not path.exists():
                    return tool_error(f"content_path not found: {content_path}")
                if not path.is_file():
                    return tool_error(f"content_path is not a file: {content_path}")
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return tool_error(f"content_path must be a UTF-8 text file: {content_path}")
            except Exception as e:
                return tool_error(f"Failed to read content_path: {e}")
        if not content:
            return tool_error("content or content_path is required")

        # If explicit URI is provided, write directly to it
        explicit_uri = args.get("uri", "")
        if explicit_uri:
            return self._tool_write({"uri": explicit_uri, "content": content, "append": True})

        # Otherwise, determine target URI based on category and topic
        category = args.get("category", "")
        topic = args.get("topic", "")

        agent_id = getattr(self, "_agent", os.environ.get("OPENVIKING_AGENT", "hermes"))
        # User memories keep direct user scope. Agent memories include the configured agent id.
        category_paths = {
            "profile": "viking://user/memories/profile.md",
            "preference": f"viking://user/memories/preferences/{topic or 'general'}.md",
            "entity": f"viking://user/memories/entities/{topic or 'unknown'}.md",
            "event": "viking://user/memories/events/{calendar:today}/event.md",
            "case": _agent_scope_uri(agent_id, "memories", "cases", "{calendar:today}", "case.md"),
            "pattern": _agent_scope_uri(agent_id, "memories", "patterns", f"{topic or 'general'}.md"),
            "tool": _agent_scope_uri(agent_id, "memories", "tools", f"{topic or 'general'}.md"),
            "skill": _agent_scope_uri(agent_id, "memories", "skills", f"{topic or 'general'}.md"),
        }

        target_uri = category_paths.get(category, "")
        if not target_uri:
            # Auto-detect: store as session message for extraction
            text = f"[Remember] {content}"
            if category:
                text = f"[Remember — {category}] {content}"
            sid_part = _url_path_part(self._session_id)
            self._client.post(f"/api/v1/sessions/{sid_part}/messages", {
                "role": "user",
                "parts": [{"type": "text", "text": text}],
            })
            commit_result = self._commit_session(
                wait=True,
                keep_recent_count=0,
                reason="remember",
            )
            return json.dumps({
                "status": "stored",
                "message": "Memory recorded for extraction on session commit.",
                "commit": commit_result,
            })

        # Write directly to the categorized path
        return self._tool_write({"uri": target_uri, "content": content, "append": True})

    def _tool_write(self, args: dict) -> str:
        uri = args.get("uri", "")
        content = args.get("content", "")
        content_path = args.get("content_path", "")
        delete_content_path_after_write = bool(args.get("delete_content_path_after_write", False))
        append = args.get("append", False)
        source_path: Optional[Path] = None

        if not uri:
            return _input_error(
                "uri is required",
                required_before_call=["target viking:// uri", "content or content_path"],
                next_action="Provide the OpenViking AGFS URI to write.",
                recovery_hint="Do not use viking_write for factmemory entities or daily logs.",
                error_code="missing_uri",
            )
        if content and content_path:
            return _input_error(
                "Provide exactly one of content or content_path",
                required_before_call=["exactly one of content or content_path"],
                next_action="Remove one content source and retry viking_write.",
                recovery_hint="Use content_path for large text to avoid huge tool-call arguments.",
                error_code="ambiguous_content_source",
            )
        if content_path:
            try:
                path = Path(str(content_path)).expanduser()
                if not path.exists():
                    return tool_error(f"content_path not found: {content_path}")
                if not path.is_file():
                    return tool_error(f"content_path is not a file: {content_path}")
                content = path.read_text(encoding="utf-8")
                source_path = path
            except UnicodeDecodeError:
                return tool_error(f"content_path must be a UTF-8 text file: {content_path}")
            except Exception as e:
                return tool_error(f"Failed to read content_path: {e}")
        if not content:
            return _input_error(
                "content or content_path is required",
                required_before_call=["content or content_path"],
                next_action="Provide exact content or a local UTF-8 file path.",
                recovery_hint="Use content_path for large content; this writes OpenViking AGFS only.",
                error_code="missing_content",
            )

        # If appending, read existing content first
        existing = ""
        file_exists = False
        if append:
            try:
                resp = self._client.get("/api/v1/content/read", params={"uri": uri})
                result = self._unwrap_result(resp)
                if isinstance(result, str):
                    existing = result
                    file_exists = True
                elif isinstance(result, dict):
                    existing = result.get("content", "") or result.get("text", "")
                    file_exists = True
            except Exception:
                pass  # File may not exist yet

        if existing and existing.strip():
            content = existing.rstrip() + "\n\n" + content

        # Determine mode: create for new files, replace for existing
        mode = "replace" if file_exists else "create"

        try:
            resp = self._client.post("/api/v1/content/write", {
                "uri": uri,
                "content": content,
                "mode": mode,
            })
            result = resp.get("result", {})
            written_bytes = result.get("written_bytes", len(content))
            deleted_content_path = self._delete_content_path_after_write(
                source_path,
                delete_content_path_after_write,
            )
            return json.dumps({
                "status": "written",
                "uri": uri,
                "mode": mode,
                "written_bytes": written_bytes,
                "deleted_content_path": deleted_content_path,
                "message": f"Content written to {uri} ({mode} mode)",
            }, ensure_ascii=False)
        except Exception as e:
            err_str = str(e).lower()
            # If create failed because file already exists, try replace
            if mode == "create" and ("conflict" in err_str or "already exists" in err_str):
                try:
                    resp = self._client.post("/api/v1/content/write", {
                        "uri": uri,
                        "content": content,
                        "mode": "replace",
                    })
                    result = resp.get("result", {})
                    written_bytes = result.get("written_bytes", len(content))
                    deleted_content_path = self._delete_content_path_after_write(
                        source_path,
                        delete_content_path_after_write,
                    )
                    return json.dumps({
                        "status": "written",
                        "uri": uri,
                        "mode": "replace",
                        "written_bytes": written_bytes,
                        "deleted_content_path": deleted_content_path,
                        "message": f"Content written to {uri} (replace mode, file existed)",
                    }, ensure_ascii=False)
                except Exception as e2:
                    return tool_error(f"Write failed (tried create then replace): {e2}")
            return tool_error(f"Write failed: {e}")

    @staticmethod
    def _delete_content_path_after_write(
        source_path: Optional[Path],
        enabled: bool,
    ) -> bool:
        if not enabled or source_path is None:
            return False
        try:
            source_path.unlink()
            return True
        except Exception as e:
            logger.debug("OpenViking content_path cleanup failed: %s", e)
            return False

    def _tool_find(self, args: dict) -> str:
        pattern = args.get("pattern", "")
        scope = args.get("scope", "viking://")

        if not pattern:
            return _input_error(
                "pattern is required",
                required_before_call=["pattern"],
                next_action="Provide a filename pattern, or use viking_search for semantic recall.",
                recovery_hint="Prefer viking_glob for new path matching.",
                error_code="missing_pattern",
            )

        # Use the search/glob endpoint for filename matching
        try:
            resp = self._client.post("/api/v1/search/glob", {
                "pattern": pattern,
                "uri": scope,
            })
            result = resp.get("result", [])
            if not isinstance(result, list):
                result = []
        except Exception as e:
            # Fallback: use fs/tree and filter client-side
            try:
                resp = self._client.get("/api/v1/fs/tree", params={"uri": scope})
                result = self._unwrap_result(resp)
                if not isinstance(result, list):
                    result = []
                # Filter by pattern
                import fnmatch
                result = [e for e in result if fnmatch.fnmatch(e.get("uri", "").rsplit("/", 1)[-1], pattern)]
            except Exception:
                return tool_error(f"Find failed: {e}")

        entries = []
        for e in result[:50]:
            if not isinstance(e, dict):
                continue
            uri = e.get("uri", "")
            name = uri.rsplit("/", 1)[-1] if uri else ""
            is_dir = bool(e.get("isDir") or e.get("is_dir") or e.get("type") == "dir")
            entries.append({
                "name": name,
                "uri": uri,
                "type": "dir" if is_dir else "file",
            })

        return json.dumps({
            "pattern": pattern,
            "scope": scope,
            "matches": entries,
            "count": len(entries),
        }, ensure_ascii=False)

    def _tool_grep(self, args: dict) -> str:
        pattern = args.get("pattern", "")
        scope = args.get("scope", "viking://")
        case_insensitive = args.get("case_insensitive", False)
        level_limit = args.get("level_limit", 5)

        if not pattern:
            return tool_error("pattern is required")

        try:
            resp = self._client.post("/api/v1/search/grep", {
                "uri": scope,
                "pattern": pattern,
                "case_insensitive": case_insensitive,
                "level_limit": level_limit,
            })
            result = resp.get("result", {})
            matches = result.get("matches", []) if isinstance(result, dict) else []
            count = result.get("count", len(matches)) if isinstance(result, dict) else len(matches)
        except Exception as e:
            return tool_error(f"Grep failed: {e}")

        entries = []
        for m in matches[:50]:
            if not isinstance(m, dict):
                continue
            entries.append({
                "uri": m.get("uri", ""),
                "line": m.get("line", 0),
                "content": m.get("content", ""),
            })

        return json.dumps({
            "pattern": pattern,
            "scope": scope,
            "case_insensitive": case_insensitive,
            "matches": entries,
            "count": count,
        }, ensure_ascii=False)

    def _tool_glob(self, args: dict) -> str:
        pattern = args.get("pattern", "")
        scope = args.get("scope", "viking://")
        limit = args.get("limit")

        if not pattern:
            return tool_error("pattern is required")

        try:
            payload = {"pattern": pattern, "uri": scope}
            if limit is not None:
                payload["node_limit"] = limit
            resp = self._client.post("/api/v1/search/glob", payload)
            result = resp.get("result", {})
            matches = result.get("matches", []) if isinstance(result, dict) else (result if isinstance(result, list) else [])
            count = result.get("count", len(matches)) if isinstance(result, dict) else len(matches)
        except Exception as e:
            return tool_error(f"Glob failed: {e}")

        entries = []
        for uri in matches[:50]:
            if isinstance(uri, str):
                name = uri.rsplit("/", 1)[-1] if uri else ""
                entries.append({"name": name, "uri": uri})
            elif isinstance(uri, dict):
                entries.append({
                    "name": uri.get("uri", "").rsplit("/", 1)[-1],
                    "uri": uri.get("uri", ""),
                })

        return json.dumps({
            "pattern": pattern,
            "scope": scope,
            "matches": entries,
            "count": count,
        }, ensure_ascii=False)

    def _tool_archive(self, args: dict) -> str:
        action = args.get("action", "")
        session_id = args.get("session_id") or self._session_id
        if not session_id:
            return tool_error("session_id is required")
        sid_part = _url_path_part(session_id)

        if action == "search":
            query = args.get("query", "")
            if not query:
                return tool_error("query is required for archive search")
            base_uri = f"viking://session/{session_id}/history"
            archive_id = args.get("archive_id", "")
            uri = f"{base_uri}/{archive_id}" if archive_id else base_uri
            payload: Dict[str, Any] = {
                "uri": uri,
                "pattern": query,
                "case_insensitive": args.get("case_insensitive", True),
            }
            for key in ("node_limit", "level_limit"):
                if args.get(key) is not None:
                    payload[key] = args[key]
            resp = self._client.post("/api/v1/search/grep", payload)
            result = self._unwrap_result(resp)
            if not isinstance(result, dict):
                result = {"matches": result if isinstance(result, list) else []}
            if self._archive_match_count(result) == 0:
                result = self._archive_context_fallback(
                    session_id=session_id,
                    query=query,
                    max_matches=int(args.get("node_limit") or 10),
                )
            return json.dumps({
                "action": action,
                "session_id": session_id,
                "archive_id": archive_id,
                "result": result,
            }, ensure_ascii=False)

        if action == "expand":
            archive_id = args.get("archive_id", "")
            if not archive_id:
                return tool_error("archive_id is required for archive expand")
            resp = self._client.get(
                f"/api/v1/sessions/{sid_part}/archives/{_url_path_part(archive_id)}"
            )
            return json.dumps({
                "action": action,
                "session_id": session_id,
                "archive_id": archive_id,
                "result": self._unwrap_result(resp),
                "raw": resp,
            }, ensure_ascii=False)

        return tool_error("action must be one of: search, expand")

    @staticmethod
    def _archive_match_count(result: Dict[str, Any]) -> int:
        for key in ("count", "match_count"):
            if result.get(key) is not None:
                try:
                    return int(result.get(key) or 0)
                except (TypeError, ValueError):
                    return 0
        matches = result.get("matches")
        return len(matches) if isinstance(matches, list) else 0

    def _archive_context_fallback(
        self,
        *,
        session_id: str,
        query: str,
        max_matches: int,
    ) -> Dict[str, Any]:
        context = self._fetch_session_context(self._client, session_id, token_budget=64000)
        tokens = [
            token for token in re.findall(r"[a-z0-9_]+", query.lower())
            if len(token) > 1
        ]
        matches: List[Dict[str, Any]] = []
        for section, text in self._archive_context_sections(context):
            haystack = text.lower()
            if tokens and not all(token in haystack for token in tokens):
                continue
            if not tokens and query.lower() not in haystack:
                continue
            matches.append({
                "section": section,
                "snippet": self._archive_snippet(text, tokens or [query.lower()]),
            })
            if len(matches) >= max_matches:
                break
        return {
            "source": f"/api/v1/sessions/{session_id}/context",
            "fallback": "session_context",
            "matches": matches,
            "count": len(matches),
            "match_count": len(matches),
        }

    @staticmethod
    def _archive_context_sections(payload: Dict[str, Any]) -> List[tuple[str, str]]:
        sections: List[tuple[str, str]] = []
        if payload.get("latest_archive_overview"):
            sections.append(("latest_archive_overview", str(payload["latest_archive_overview"])))
        for archive in payload.get("pre_archive_abstracts") or []:
            if isinstance(archive, dict):
                sections.append((
                    f"archive:{archive.get('archive_id', 'archive')}:abstract",
                    str(archive.get("abstract") or ""),
                ))
        for index, message in enumerate(payload.get("messages") or [], start=1):
            if not isinstance(message, dict):
                continue
            chunks: List[str] = []
            for part in message.get("parts") or []:
                if not isinstance(part, dict):
                    continue
                for key in ("text", "abstract", "tool_output"):
                    if part.get(key):
                        chunks.append(str(part[key]))
            if chunks:
                sections.append((f"message:{index}:{message.get('role', '')}", "\n".join(chunks)))
        return [(section, text) for section, text in sections if text.strip()]

    @staticmethod
    def _archive_snippet(text: str, tokens: List[str], *, radius: int = 240) -> str:
        lower = text.lower()
        positions = [lower.find(token) for token in tokens if token and lower.find(token) >= 0]
        start = max(0, min(positions) - radius) if positions else 0
        end = min(len(text), start + radius * 2)
        prefix = "..." if start else ""
        suffix = "..." if end < len(text) else ""
        return prefix + text[start:end] + suffix

    def _tool_add_resource(self, args: dict) -> str:
        url = args.get("url", "")
        if not url:
            return tool_error("url is required")

        if args.get("to") and args.get("parent"):
            return tool_error("Cannot specify both 'to' and 'parent'")
        for key in ("to", "parent"):
            if args.get(key) and not _is_resource_uri(args[key]):
                return tool_error(f"{key} must be under viking://resources/")

        payload: Dict[str, Any] = {}
        for key in ("reason", "to", "parent", "instruction", "wait", "timeout", "watch_interval"):
            if key in args and args[key] not in (None, ""):
                payload[key] = args[key]

        parsed_url = urlparse(url)
        if _is_remote_resource_source(url):
            source_path = None
        elif parsed_url.scheme == "file":
            source_path = _path_from_file_uri(url)
            if isinstance(source_path, str):
                return tool_error(source_path)
        elif parsed_url.scheme and not _is_windows_absolute_path(url):
            source_path = None
        else:
            source_path = Path(url).expanduser()

        source_path_text = str(source_path) if isinstance(source_path, Path) else None
        cleanup_path: Optional[Path] = None
        try:
            if source_path is not None:
                if source_path.exists():
                    if source_path.is_dir():
                        payload["source_name"] = source_path.name
                        cleanup_path = _zip_directory(source_path)
                        upload_path = cleanup_path
                    elif source_path.is_file():
                        payload["source_name"] = source_path.name
                        upload_path = source_path
                    else:
                        return tool_error(f"Unsupported local resource path: {url}")
                    payload["temp_file_id"] = self._client.upload_temp_file(upload_path)
                elif _is_local_path_reference(url):
                    return tool_error(f"Local resource path does not exist: {url}")
                else:
                    payload["path"] = url
            else:
                payload["path"] = url

            resp = self._client.post("/api/v1/resources", payload)
            result = self._unwrap_result(resp)
            if not isinstance(result, dict):
                result = {}

            # If wait is requested, poll for processing completion
            if args.get("wait"):
                timeout = args.get("timeout", 60)
                try:
                    wait_resp = self._client.post("/api/v1/system/wait", {"timeout": timeout})
                    wait_result = self._unwrap_result(wait_resp)
                    if not isinstance(wait_result, dict):
                        wait_result = {}
                    total_processed = sum(
                        q.get("processed", 0) for q in wait_result.values() if isinstance(q, dict)
                    )
                    total_errors = sum(
                        q.get("error_count", 0) for q in wait_result.values() if isinstance(q, dict)
                    )
                    return json.dumps({
                        "status": "added_and_processed",
                        "root_uri": result.get("root_uri", ""),
                        "source_path": result.get("source_path") or source_path_text,
                        "errors": total_errors,
                        "resource_errors": result.get("errors", []),
                        "queue_status": wait_result,
                        "processed": total_processed,
                        "error_count": total_errors,
                        "result": result,
                        "message": f"Resource added and processed ({total_processed} items, {total_errors} errors).",
                    }, ensure_ascii=False)
                except Exception as e:
                    logger.warning("OpenViking wait_processed failed: %s", e)
                    return json.dumps({
                        "status": "added_wait_failed",
                        "root_uri": result.get("root_uri", ""),
                        "source_path": result.get("source_path") or source_path_text,
                        "errors": result.get("errors", []),
                        "wait_error": str(e),
                        "result": result,
                        "message": "Resource was added, but waiting for processing failed.",
                    }, ensure_ascii=False)

        finally:
            if cleanup_path:
                cleanup_path.unlink(missing_ok=True)

        return json.dumps({
            "status": "added",
            "root_uri": result.get("root_uri", ""),
            "source_path": result.get("source_path") or source_path_text,
            "errors": result.get("errors", []),
            "result": result,
            "message": "Resource queued for processing. Use viking_search after a moment to find it.",
        }, ensure_ascii=False)

    def _tool_link(self, args: dict) -> str:
        from_uri = args.get("from_uri", "")
        to_uri = args.get("to_uri", "")
        reason = args.get("reason", "")

        if not from_uri:
            return tool_error("from_uri is required")
        if not to_uri:
            return tool_error("to_uri is required")

        # OpenViking link() writes .relations.json to from_uri's directory.
        # If from_uri is a file, we need to use its parent directory.
        # Check if from_uri is a file or directory.
        try:
            stat_resp = self._client.get("/api/v1/fs/stat", params={"uri": from_uri})
            stat_result = self._unwrap_result(stat_resp)
            is_dir = bool(stat_result.get("isDir") or stat_result.get("is_dir"))
            if not is_dir:
                # Use parent directory for the link
                from_uri = from_uri.rsplit("/", 1)[0] + "/"
        except Exception:
            pass  # Assume it's a directory if stat fails

        try:
            resp = self._client.post("/api/v1/relations/link", {
                "from_uri": from_uri,
                "to_uris": to_uri,
                "reason": reason,
            })
            return json.dumps({
                "status": "linked",
                "from": from_uri,
                "to": to_uri,
                "reason": reason,
                "message": f"Linked {from_uri} -> {to_uri}",
            }, ensure_ascii=False)
        except Exception as e:
            return tool_error(f"Link failed: {e}")

    def _tool_relations(self, args: dict) -> str:
        uri = args.get("uri", "")
        if not uri:
            return tool_error("uri is required")

        try:
            resp = self._client.get("/api/v1/relations", params={"uri": uri})
            result = self._unwrap_result(resp)
            relations = result if isinstance(result, list) else []
            return json.dumps({
                "status": "ok",
                "uri": uri,
                "relations": relations,
                "count": len(relations),
            }, ensure_ascii=False)
        except Exception as e:
            return tool_error(f"Relations query failed: {e}")

    def _add_skill_payload(self, *, data: Any = None, path: str = "",
                           wait: Any = None, timeout: Any = None) -> Dict[str, Any] | str:
        has_data = data is not None
        has_path = bool(path)
        if has_data == has_path:
            return "Exactly one of data or path is required"

        payload: Dict[str, Any] = {}
        if wait is not None:
            payload["wait"] = bool(wait)
        if timeout is not None:
            payload["timeout"] = timeout

        if has_data:
            if not isinstance(data, (dict, str)):
                return "data must be a structured skill dict, MCP tool dict, or raw SKILL.md string"
            payload["data"] = data
            return payload

        skill_path = Path(path).expanduser()
        if not skill_path.exists():
            return f"Skill path does not exist: {path}"

        cleanup_path: Optional[Path] = None
        try:
            if skill_path.is_dir():
                if not (skill_path / "SKILL.md").is_file():
                    return f"Skill directory must contain SKILL.md: {path}"
                cleanup_path = _zip_directory(skill_path)
                upload_path = cleanup_path
                payload["source_name"] = skill_path.name
            elif skill_path.is_file():
                if skill_path.name != "SKILL.md":
                    return f"Skill file must be named SKILL.md: {path}"
                upload_path = skill_path
                payload["source_name"] = skill_path.parent.name
            else:
                return f"Unsupported skill path: {path}"
            payload["temp_file_id"] = self._client.upload_temp_file(upload_path)
        finally:
            if cleanup_path:
                cleanup_path.unlink(missing_ok=True)
        return payload

    def _post_skill_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        resp = self._client.post("/api/v1/skills", payload)
        result = self._unwrap_result(resp)
        summary = _skill_summary_from_result(result)
        if payload.get("wait") and not summary["queue_status"]:
            timeout = payload.get("timeout", 60)
            try:
                wait_resp = self._client.post("/api/v1/system/wait", {"timeout": timeout})
                wait_result = self._unwrap_result(wait_resp)
                summary["queue_status"] = wait_result if isinstance(wait_result, dict) else {}
                summary["status"] = "added_and_processed"
            except Exception as exc:
                summary["status"] = "added_wait_failed"
                summary["wait_error"] = str(exc)
        return summary

    def _tool_add_skill(self, args: dict) -> str:
        payload_or_error = self._add_skill_payload(
            data=args.get("data") if "data" in args else None,
            path=args.get("path", ""),
            wait=args.get("wait") if "wait" in args else None,
            timeout=args.get("timeout") if "timeout" in args else None,
        )
        if isinstance(payload_or_error, str):
            return tool_error(payload_or_error)

        summary = self._post_skill_payload(payload_or_error)
        return json.dumps(summary, ensure_ascii=False)

    def _tool_sync_skills(self, args: dict) -> str:
        """Sync Hermes skills to OpenViking."""
        dry_run = args.get("dry_run", False)
        skills_dir = Path.home() / ".hermes" / "skills"

        if not skills_dir.exists():
            return tool_error(f"Hermes skills directory not found: {skills_dir}")

        skills = []
        for skill_md in skills_dir.rglob("SKILL.md"):
            try:
                content = skill_md.read_text(encoding="utf-8")
                # Parse YAML frontmatter
                name = skill_md.parent.name
                description = ""
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        frontmatter = parts[1].strip()
                        for line in frontmatter.splitlines():
                            line = line.strip()
                            if line.startswith("name:"):
                                name = line.split(":", 1)[1].strip().strip('"').strip("'")
                            elif line.startswith("description:"):
                                description = line.split(":", 1)[1].strip().strip('"').strip("'")
                skills.append({
                    "name": name,
                    "description": description,
                    "content": content,
                    "source": str(skill_md.relative_to(skills_dir)),
                })
            except Exception as e:
                logger.debug("Failed to parse %s: %s", skill_md, e)

        if not skills:
            return json.dumps({"status": "ok", "message": "No skills found to sync."}, ensure_ascii=False)

        if dry_run:
            return json.dumps({
                "status": "dry_run",
                "skills_found": len(skills),
                "skills": [{"name": s["name"], "description": s["description"], "source": s["source"]} for s in skills],
                "message": f"Found {len(skills)} skills. Use dry_run=false to sync.",
            }, ensure_ascii=False)

        success = 0
        failed = 0
        results = []

        for skill in skills:
            try:
                payload_or_error = self._add_skill_payload(data={
                    "name": skill["name"],
                    "description": skill["description"],
                    "content": skill["content"],
                })
                if isinstance(payload_or_error, str):
                    raise RuntimeError(payload_or_error)
                result = self._post_skill_payload(payload_or_error)
                uri = result.get("uri", "")
                results.append({"name": skill["name"], "status": "success", "uri": uri, "result": result})
                success += 1
            except Exception as e:
                results.append({"name": skill["name"], "status": "failed", "error": str(e)})
                failed += 1

        return json.dumps({
            "status": "synced",
            "total": len(skills),
            "success": success,
            "failed": failed,
            "results": results,
            "message": f"Synced {success}/{len(skills)} skills to OpenViking.",
        }, ensure_ascii=False)

    def _tool_system(self, args: dict) -> str:
        action = args.get("action", "")
        if action == "health":
            result = self._client.get("/health")
            return json.dumps({"action": action, "result": result}, ensure_ascii=False)
        if action == "ready":
            result = self._client.get("/ready")
            return json.dumps({"action": action, "result": result}, ensure_ascii=False)
        if action == "status":
            result = self._client.get("/api/v1/system/status")
            return json.dumps({"action": action, "result": self._unwrap_result(result), "raw": result}, ensure_ascii=False)
        if action == "wait":
            payload: Dict[str, Any] = {}
            if args.get("timeout") is not None:
                payload["timeout"] = args["timeout"]
            result = self._client.post("/api/v1/system/wait", payload)
            return json.dumps({"action": action, "result": self._unwrap_result(result), "raw": result}, ensure_ascii=False)
        if action == "observer":
            component = args.get("component", "")
            if component not in {"queue", "vikingdb", "models"}:
                return tool_error("component must be one of: queue, vikingdb, models")
            result = self._client.get(f"/api/v1/observer/{component}")
            return json.dumps({"action": action, "component": component, "result": self._unwrap_result(result), "raw": result}, ensure_ascii=False)
        if action == "metrics":
            text = self._client.get_text("/metrics")
            filter_prefix = args.get("filter_prefix", "")
            if filter_prefix:
                kept = []
                for line in text.splitlines():
                    stripped = line.strip()
                    if not stripped:
                        continue
                    metric_name = stripped[2:].split(None, 1)[0] if stripped.startswith("# ") else stripped.split("{", 1)[0].split(None, 1)[0]
                    if metric_name.startswith(filter_prefix):
                        kept.append(line)
                text = "\n".join(kept)
            max_chars = args.get("max_chars", 12000)
            try:
                max_chars = max(0, int(max_chars))
            except (TypeError, ValueError):
                max_chars = 12000
            truncated = len(text) > max_chars
            if truncated:
                text = text[:max_chars]
            return json.dumps({
                "action": action,
                "format": "prometheus",
                "metrics": text,
                "truncated": truncated,
                "max_chars": max_chars,
            }, ensure_ascii=False)
        return tool_error("action must be one of: health, ready, status, wait, observer, metrics")

    def _require_admin_approval(self, action: str, details: Dict[str, Any]) -> str | None:
        try:
            from tools.approval import check_tool_action_approval
        except Exception as exc:
            return f"Approval system unavailable: {exc}"

        summary = f"OpenViking admin write '{action}' with {json.dumps(details, ensure_ascii=False, sort_keys=True)}"
        decision = check_tool_action_approval(
            f"openviking_admin:{action}",
            summary,
            pattern_key=f"openviking-admin:{action}",
            allow_permanent=False,
            surface="openviking_admin",
        )
        if decision.get("approved"):
            return None
        return decision.get("message") or "OpenViking admin action denied"

    def _tool_admin(self, args: dict) -> str:
        action = args.get("action", "")
        if action == "list_accounts":
            resp = self._client.get("/api/v1/admin/accounts")
            return json.dumps({"action": action, "result": self._unwrap_result(resp), "raw": resp}, ensure_ascii=False)

        if action in {"list_users", "list_agents"}:
            account_id = args.get("account_id", "")
            if not account_id:
                return tool_error("account_id is required")
            suffix = "users" if action == "list_users" else "agents"
            resp = self._client.get(f"/api/v1/admin/accounts/{_url_path_part(account_id)}/{suffix}")
            return json.dumps({"action": action, "account_id": account_id, "result": self._unwrap_result(resp), "raw": resp}, ensure_ascii=False)

        write_actions = {
            "create_account", "delete_account", "register_user",
            "remove_user", "set_role", "regenerate_key",
        }
        if action not in write_actions:
            return tool_error("unknown admin action")

        account_id = args.get("account_id", "")
        user_id = args.get("user_id", "")
        role = args.get("role", "")
        admin_user_id = args.get("admin_user_id", "")

        if action == "create_account":
            if not account_id:
                return tool_error("account_id is required for create_account")
            if not admin_user_id:
                return tool_error("admin_user_id is required for create_account")
            payload = {"account_id": account_id, "admin_user_id": admin_user_id}
            denied = self._require_admin_approval(action, payload)
            if denied:
                return tool_error(denied)
            resp = self._client.post("/api/v1/admin/accounts", payload)
            return json.dumps({"action": action, "result": self._unwrap_result(resp), "raw": resp}, ensure_ascii=False)

        if not account_id:
            return tool_error(f"account_id is required for {action}")

        account_part = _url_path_part(account_id)
        if action == "delete_account":
            denied = self._require_admin_approval(action, {"account_id": account_id})
            if denied:
                return tool_error(denied)
            resp = self._client.delete(f"/api/v1/admin/accounts/{account_part}")
            return json.dumps({"action": action, "result": self._unwrap_result(resp), "raw": resp}, ensure_ascii=False)

        if not user_id:
            return tool_error(f"user_id is required for {action}")
        user_part = _url_path_part(user_id)

        if action == "register_user":
            payload = {"user_id": user_id}
            if role:
                normalized_role = str(role).lower()
                if normalized_role not in {"admin", "user"}:
                    return tool_error("role must be admin or user")
                payload["role"] = normalized_role
            denied = self._require_admin_approval(action, {"account_id": account_id, **payload})
            if denied:
                return tool_error(denied)
            resp = self._client.post(f"/api/v1/admin/accounts/{account_part}/users", payload)
            return json.dumps({"action": action, "result": self._unwrap_result(resp), "raw": resp}, ensure_ascii=False)

        if action == "remove_user":
            denied = self._require_admin_approval(action, {"account_id": account_id, "user_id": user_id})
            if denied:
                return tool_error(denied)
            resp = self._client.delete(f"/api/v1/admin/accounts/{account_part}/users/{user_part}")
            return json.dumps({"action": action, "result": self._unwrap_result(resp), "raw": resp}, ensure_ascii=False)

        if action == "set_role":
            normalized_role = str(role).lower()
            if normalized_role not in {"admin", "user"}:
                return tool_error("role is required and must be admin or user for set_role")
            payload = {"role": normalized_role}
            denied = self._require_admin_approval(action, {"account_id": account_id, "user_id": user_id, **payload})
            if denied:
                return tool_error(denied)
            resp = self._client.put(f"/api/v1/admin/accounts/{account_part}/users/{user_part}/role", payload)
            return json.dumps({"action": action, "result": self._unwrap_result(resp), "raw": resp}, ensure_ascii=False)

        if action == "regenerate_key":
            denied = self._require_admin_approval(action, {"account_id": account_id, "user_id": user_id})
            if denied:
                return tool_error(denied)
            resp = self._client.post(f"/api/v1/admin/accounts/{account_part}/users/{user_part}/key")
            return json.dumps({"action": action, "result": self._unwrap_result(resp), "raw": resp}, ensure_ascii=False)

        return tool_error("unknown admin action")

    def _tool_consistency(self, args: dict) -> str:
        scope = args.get("scope", "viking://")

        try:
            resp = self._client.post("/api/v1/system/consistency", {"uri": scope})
            result = resp.get("result", {})

            ok = result.get("ok", False)
            expected = result.get("expected_count", 0)
            missing_count = result.get("missing_record_count", 0)
            truncated = result.get("missing_records_truncated", False)
            missing = result.get("missing_records", [])

            return json.dumps({
                "ok": ok,
                "scope": scope,
                "expected_count": expected,
                "missing_count": missing_count,
                "truncated": truncated,
                "missing_records": [
                    {"uri": m.get("uri", ""), "path": m.get("path", ""), "level": m.get("level", 0)}
                    for m in missing[:20]
                ],
                "message": f"Consistency check: {'OK' if ok else f'{missing_count} missing'} (expected={expected}).",
            }, ensure_ascii=False)
        except Exception as e:
            return tool_error(f"Consistency check failed: {e}")

    def _tool_reindex(self, args: dict) -> str:
        scope = args.get("scope", "viking://")
        mode = args.get("mode", "vectors_only")
        wait = args.get("wait", True)

        try:
            resp = self._client.post("/api/v1/content/reindex", {
                "uri": scope,
                "mode": mode,
                "wait": wait,
            })
            result = resp.get("result", {})

            status = result.get("status", "unknown")
            task_id = result.get("task_id", "")
            scanned = result.get("scanned_records", 0)
            rebuilt = result.get("rebuilt_records", 0)
            unsupported = result.get("unsupported_records", 0)
            failed = result.get("failed_records", 0)
            duration = result.get("duration_ms", 0)
            warnings = result.get("warnings", [])

            return json.dumps({
                "status": status,
                "scope": scope,
                "mode": mode,
                "task_id": task_id,
                "scanned": scanned,
                "rebuilt": rebuilt,
                "unsupported": unsupported,
                "failed": failed,
                "duration_ms": duration,
                "warnings": warnings,
                "message": f"Reindex {status}: {rebuilt}/{scanned} rebuilt ({failed} failed, {unsupported} unsupported).",
            }, ensure_ascii=False)
        except Exception as e:
            return tool_error(f"Reindex failed: {e}")

    def _tool_watch(self, args: dict) -> str:
        action = args.get("action", "")
        if action == "list":
            try:
                resp = self._client.get("/api/v1/watches")
                result = self._unwrap_result(resp)
                if isinstance(result, dict) and "tasks" in result:
                    tasks = result["tasks"]
                elif isinstance(result, list):
                    tasks = result
                else:
                    tasks = []
                return json.dumps({
                    "status": "ok",
                    "action": action,
                    "tasks": [
                        {
                            "task_id": t.get("task_id", ""),
                            "to_uri": t.get("to_uri", ""),
                            "watch_interval": t.get("watch_interval", 0),
                            "is_active": t.get("is_active", False),
                            "last_execution_time": t.get("last_execution_time", ""),
                        }
                        for t in tasks if isinstance(t, dict)
                    ],
                    "count": len(tasks),
                }, ensure_ascii=False)
            except Exception as e:
                return tool_error(f"Watch list failed: {e}")

        if action == "get":
            task_id = args.get("task_id", "")
            to_uri = args.get("to_uri", "")
            if not task_id and not to_uri:
                return _input_error(
                    "task_id or to_uri is required for get",
                    required_before_call=["task_id or to_uri"],
                    next_action="Provide the watch task_id or the resource to_uri.",
                    recovery_hint="Use list first if you are unsure of the task_id.",
                )
            try:
                if task_id:
                    resp = self._client.get(f"/api/v1/watches/{_url_path_part(task_id)}")
                else:
                    resp = self._client.get("/api/v1/watches", params={"to_uri": to_uri})
                result = self._unwrap_result(resp)
                return json.dumps({
                    "status": "ok",
                    "action": action,
                    "task": result if isinstance(result, dict) else {},
                }, ensure_ascii=False)
            except Exception as e:
                return tool_error(f"Watch get failed: {e}")

        if action == "cancel":
            to_uri = args.get("to_uri", "")
            task_id = args.get("task_id", "")
            if not to_uri and not task_id:
                return _input_error(
                    "task_id or to_uri is required for cancel",
                    required_before_call=["task_id or to_uri"],
                    next_action="Provide the resource to_uri or task_id to cancel.",
                    recovery_hint="Use list to find the to_uri or task_id of the watch to cancel.",
                )
            try:
                if task_id:
                    resp = self._client.delete(f"/api/v1/watches/{_url_path_part(task_id)}")
                else:
                    resp = self._client.delete("/api/v1/watches", params={"to_uri": to_uri})
                result = self._unwrap_result(resp)
                return json.dumps({
                    "status": "cancelled",
                    "action": action,
                    "task_id": result.get("task_id") if isinstance(result, dict) else task_id,
                    "to_uri": result.get("to_uri") if isinstance(result, dict) else to_uri,
                    "message": "Watch task cancelled.",
                }, ensure_ascii=False)
            except Exception as e:
                return tool_error(f"Watch cancel failed: {e}")

        if action == "trigger":
            to_uri = args.get("to_uri", "")
            task_id = args.get("task_id", "")
            if not to_uri and not task_id:
                return _input_error(
                    "task_id or to_uri is required for trigger",
                    required_before_call=["task_id or to_uri"],
                    next_action="Provide the resource to_uri or task_id to trigger.",
                    recovery_hint="Use list to find the to_uri or task_id of the watch to trigger.",
                )
            try:
                if task_id:
                    resp = self._client.post(f"/api/v1/watches/{_url_path_part(task_id)}/trigger")
                else:
                    resp = self._client.post("/api/v1/watches/trigger", params={"to_uri": to_uri})
                result = self._unwrap_result(resp)
                return json.dumps({
                    "status": "triggered",
                    "action": action,
                    "task_id": result.get("task_id") if isinstance(result, dict) else task_id,
                    "to_uri": result.get("to_uri") if isinstance(result, dict) else to_uri,
                    "scheduled": result.get("scheduled", True) if isinstance(result, dict) else True,
                    "message": "Watch task triggered for immediate execution.",
                }, ensure_ascii=False)
            except Exception as e:
                return tool_error(f"Watch trigger failed: {e}")

        return _input_error(
            "action must be one of: list, get, cancel, trigger",
            required_before_call=["action in list|get|cancel|trigger"],
            next_action="Choose an explicit watch management action.",
            recovery_hint="Use list first to see active watches, then cancel or trigger by to_uri.",
        )


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

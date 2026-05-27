"""Local registry and OpenViking bridge for document-memory tools."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from tools.registry import tool_error

from plugins.memory.openviking.client import _VikingClient
from plugins.memory.openviking.utils import _DEFAULT_ENDPOINT

REGISTRY_VERSION = "document_memory_registry.v1"
RESOURCE_ROOT = "viking://resources/document-memory"
_REMOTE_PREFIXES = ("http://", "https://")
_CLIENT_FACTORY = _VikingClient


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _workspace_root() -> Path:
    configured = os.environ.get("DOCUMENT_MEMORY_WORKSPACE")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "workspace"


def _registry_root() -> Path:
    configured = os.environ.get("DOCUMENT_MEMORY_ROOT")
    if configured:
        return Path(configured).expanduser()
    return _workspace_root() / ".memory" / "documents"


def _events_path() -> Path:
    return _registry_root() / "registry.jsonl"


def _index_path() -> Path:
    return _registry_root() / "index.json"


def _ensure_root() -> None:
    _registry_root().mkdir(parents=True, exist_ok=True)


def _slug(value: str, fallback: str = "document") -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = value.strip("-._")
    return value or fallback


def _collection(value: str | None) -> str:
    return _slug(value or "general", "general")


def _as_tags(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        raw = re.split(r"[,，]\s*", value)
    elif isinstance(value, list):
        raw = value
    else:
        raw = []
    seen: set[str] = set()
    tags: list[str] = []
    for item in raw:
        tag = str(item).strip()
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags


def _is_remote(path: str) -> bool:
    return path.startswith(_REMOTE_PREFIXES)


def _source_title(path: str) -> str:
    parsed = urlparse(path)
    name = Path(unquote(parsed.path)).name if parsed.scheme else Path(path).name
    return name or parsed.netloc or "Untitled document"


def _local_path(path: str) -> Path:
    parsed = urlparse(path)
    if parsed.scheme == "file":
        return Path(unquote(parsed.path)).expanduser()
    return Path(path).expanduser()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_index() -> dict[str, Any]:
    path = _index_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("documents"), dict):
                return data
        except Exception:
            pass

    documents: dict[str, dict[str, Any]] = {}
    events = _events_path()
    if events.exists():
        for line in events.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            doc_id = event.get("doc_id")
            if not doc_id:
                continue
            if event.get("event") == "import":
                documents[doc_id] = dict(event.get("document") or {})
            elif event.get("event") == "link_entity" and doc_id in documents:
                links = documents[doc_id].setdefault("related_entities", [])
                link = event.get("link") or {}
                if link and link not in links:
                    links.append(link)
    return {"version": REGISTRY_VERSION, "documents": documents}


def _write_index(data: dict[str, Any]) -> None:
    _ensure_root()
    _index_path().write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _append_event(event: dict[str, Any]) -> None:
    _ensure_root()
    event = {"version": REGISTRY_VERSION, "created_at": _now_iso(), **event}
    with _events_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def _client() -> _VikingClient:
    return _CLIENT_FACTORY(
        os.environ.get("OPENVIKING_ENDPOINT", _DEFAULT_ENDPOINT),
        os.environ.get("OPENVIKING_API_KEY", ""),
        account=os.environ.get("OPENVIKING_ACCOUNT", "default"),
        user=os.environ.get("OPENVIKING_USER", "default"),
        agent=os.environ.get("OPENVIKING_AGENT", "hermes"),
    )


def _unwrap(resp: Any) -> Any:
    if isinstance(resp, dict) and "result" in resp:
        return resp["result"]
    return resp


def _doc_resource_parent(collection: str) -> str:
    return f"{RESOURCE_ROOT}/{collection}/"


def _doc_id(collection: str, title: str, digest: str) -> str:
    return f"doc/{collection}/{_slug(title, 'document')}-{digest[:12]}"


def _upload_or_add_resource(client: _VikingClient, source: str, collection: str, wait: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "parent": _doc_resource_parent(collection),
        "reason": "document_memory_import",
    }
    if _is_remote(source):
        payload["path"] = source
    else:
        path = _local_path(source)
        if not path.exists():
            raise FileNotFoundError(f"Local document path does not exist: {source}")
        if not path.is_file():
            raise ValueError(f"document_import currently supports files and URLs, not directories: {source}")
        payload["source_name"] = path.name
        payload["temp_file_id"] = client.upload_temp_file(path)
    resp = client.post("/api/v1/resources", payload)
    result = _unwrap(resp)
    if not isinstance(result, dict):
        result = {}
    if wait:
        try:
            result["wait_result"] = _unwrap(client.post("/api/v1/system/wait", {"timeout": 60}))
        except Exception as e:
            result["wait_error"] = str(e)
    return result


def _match_filters(doc: dict[str, Any], collection: str = "", tags: list[str] | None = None, status: str = "") -> bool:
    if collection and doc.get("collection") != _collection(collection):
        return False
    if status and doc.get("status") != status:
        return False
    required = set(tags or [])
    if required and not required.issubset(set(doc.get("tags") or [])):
        return False
    return True


def _find_doc_by_uri(documents: dict[str, dict[str, Any]], uri: str) -> dict[str, Any] | None:
    for doc in documents.values():
        root = str(doc.get("openviking_uri") or "")
        if uri and root and (uri == root or uri.startswith(root)):
            return doc
    return None


def handle_document_import(args: dict, **kwargs) -> str:
    source = str(args.get("path") or "").strip()
    if not source:
        return tool_error("path is required")

    collection = _collection(args.get("collection"))
    title = str(args.get("title") or _source_title(source)).strip()
    tags = _as_tags(args.get("tags"))
    doc_type = str(args.get("doc_type") or Path(_source_title(source)).suffix.lstrip(".") or "document")
    force = bool(args.get("force", False))

    try:
        if _is_remote(source):
            digest = _sha256_text(source)
            size_bytes = None
            source_path = source
        else:
            path = _local_path(source)
            if not path.exists():
                return tool_error(f"Local document path does not exist: {source}")
            if not path.is_file():
                return tool_error(f"document_import currently supports files and URLs, not directories: {source}")
            digest = _sha256_file(path)
            size_bytes = path.stat().st_size
            source_path = str(path)
    except Exception as e:
        return tool_error(str(e))

    doc_id = _doc_id(collection, title, digest)
    index = _load_index()
    documents = index.setdefault("documents", {})
    if doc_id in documents and not force:
        return json.dumps({
            "status": "duplicate",
            "doc_id": doc_id,
            "document": documents[doc_id],
            "message": "Document already registered; pass force=true to re-index.",
        }, ensure_ascii=False)

    openviking_result: dict[str, Any] = {}
    status = "registered"
    error = ""
    try:
        openviking_result = _upload_or_add_resource(_client(), source, collection, bool(args.get("wait", False)))
        status = "indexed" if openviking_result.get("root_uri") else "queued"
    except Exception as e:
        status = "registered_unindexed"
        error = str(e)

    document = {
        "doc_id": doc_id,
        "title": title,
        "source_path": source_path,
        "source_type": "url" if _is_remote(source) else "file",
        "doc_type": doc_type,
        "sha256": digest,
        "size_bytes": size_bytes,
        "collection": collection,
        "tags": tags,
        "openviking_uri": openviking_result.get("root_uri", ""),
        "status": status,
        "imported_at": _now_iso(),
        "related_entities": [],
        "openviking_error": error,
    }
    documents[doc_id] = document
    _write_index(index)
    _append_event({"event": "import", "doc_id": doc_id, "document": document})

    return json.dumps({
        "status": status,
        "doc_id": doc_id,
        "document": document,
        "openviking_result": openviking_result,
        "boundary": "RAG recall only; confirmed claims must go through factmemory_context -> factmemory_log -> factmemory_entity(stage|commit).",
    }, ensure_ascii=False)


def handle_document_list(args: dict, **kwargs) -> str:
    index = _load_index()
    tags = _as_tags(args.get("tags"))
    limit = int(args.get("limit") or 50)
    docs = [
        doc for doc in index.get("documents", {}).values()
        if _match_filters(doc, args.get("collection") or "", tags, args.get("status") or "")
    ]
    docs.sort(key=lambda doc: str(doc.get("imported_at", "")), reverse=True)
    return json.dumps({
        "status": "ok",
        "count": len(docs),
        "documents": docs[:limit],
        "registry_root": str(_registry_root()),
    }, ensure_ascii=False)


def handle_document_search(args: dict, **kwargs) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return tool_error("query is required")
    collection = _collection(args.get("collection")) if args.get("collection") else ""
    tags = _as_tags(args.get("tags"))
    limit = int(args.get("limit") or 10)
    scope = _doc_resource_parent(collection) if collection else f"{RESOURCE_ROOT}/"

    payload: dict[str, Any] = {"query": query, "target_uri": scope, "limit": limit}
    if args.get("level"):
        payload["level"] = args["level"]

    try:
        result = _unwrap(_client().post("/api/v1/search/find", payload))
    except Exception as e:
        return tool_error(f"OpenViking document search failed: {e}")
    if not isinstance(result, dict):
        result = {}

    index = _load_index()
    documents = index.get("documents", {})
    enriched: list[dict[str, Any]] = []
    for group in ("resources", "memories", "skills"):
        for item in result.get(group, []) or []:
            if not isinstance(item, dict):
                continue
            doc = _find_doc_by_uri(documents, str(item.get("uri") or ""))
            if tags and (not doc or not set(tags).issubset(set(doc.get("tags") or []))):
                continue
            enriched.append({
                "uri": item.get("uri", ""),
                "score": item.get("score"),
                "summary": item.get("abstract") or item.get("summary") or item.get("text") or "",
                "context_type": group[:-1],
                "document": doc,
            })

    return json.dumps({
        "status": "ok",
        "query": query,
        "scope": scope,
        "results": enriched[:limit],
        "raw_total": result.get("total"),
        "boundary": "Search results are document evidence only, not confirmed factmemory state.",
    }, ensure_ascii=False)


def handle_document_read(args: dict, **kwargs) -> str:
    doc_id = str(args.get("doc_id") or "").strip()
    uri = str(args.get("uri") or "").strip()
    if not doc_id and not uri:
        return tool_error("Provide doc_id or uri")

    index = _load_index()
    document = None
    if doc_id:
        document = index.get("documents", {}).get(doc_id)
        if not document:
            return tool_error(f"Unknown doc_id: {doc_id}")
        uri = str(document.get("openviking_uri") or "")
        if not uri:
            return tool_error(f"Document is registered but has no OpenViking URI: {doc_id}")

    level = args.get("level") or "overview"
    endpoint = "/api/v1/content/read"
    if level == "abstract":
        endpoint = "/api/v1/content/abstract"
    elif level == "overview":
        endpoint = "/api/v1/content/overview"

    params: dict[str, Any] = {"uri": uri}
    if endpoint == "/api/v1/content/read":
        for key in ("offset", "limit"):
            if args.get(key) is not None:
                params[key] = args[key]

    try:
        try:
            result = _unwrap(_client().get(endpoint, params=params))
        except Exception:
            if endpoint == "/api/v1/content/read":
                raise
            result = _unwrap(_client().get("/api/v1/content/read", params={"uri": uri}))
    except Exception as e:
        return tool_error(f"OpenViking document read failed: {e}")

    content = result if isinstance(result, str) else ""
    if isinstance(result, dict):
        content = result.get("content") or result.get("text") or ""
    return json.dumps({
        "status": "ok",
        "doc_id": doc_id or (document or {}).get("doc_id", ""),
        "uri": uri,
        "level": level,
        "document": document,
        "content": content,
    }, ensure_ascii=False)


def handle_document_link_entity(args: dict, **kwargs) -> str:
    doc_id = str(args.get("doc_id") or "").strip()
    entity_id = str(args.get("entity_id") or "").strip()
    if not doc_id or not entity_id:
        return tool_error("doc_id and entity_id are required")

    index = _load_index()
    doc = index.get("documents", {}).get(doc_id)
    if not doc:
        return tool_error(f"Unknown doc_id: {doc_id}")
    link = {"entity_id": entity_id, "reason": str(args.get("reason") or ""), "linked_at": _now_iso()}
    links = doc.setdefault("related_entities", [])
    if not any(existing.get("entity_id") == entity_id for existing in links if isinstance(existing, dict)):
        links.append(link)
    _write_index(index)
    _append_event({"event": "link_entity", "doc_id": doc_id, "link": link})
    return json.dumps({"status": "linked", "doc_id": doc_id, "entity_id": entity_id, "document": doc}, ensure_ascii=False)


def handle_document_promote_to_fact(args: dict, **kwargs) -> str:
    doc_id = str(args.get("doc_id") or "").strip()
    conclusion = str(args.get("conclusion") or "").strip()
    if not doc_id or not conclusion:
        return tool_error("doc_id and conclusion are required")
    index = _load_index()
    doc = index.get("documents", {}).get(doc_id)
    if not doc:
        return tool_error(f"Unknown doc_id: {doc_id}")

    evidence_packet = {
        "source": "document_memory",
        "doc_id": doc_id,
        "title": doc.get("title"),
        "source_path": doc.get("source_path"),
        "openviking_uri": doc.get("openviking_uri"),
        "collection": doc.get("collection"),
        "tags": doc.get("tags"),
        "evidence": str(args.get("evidence") or ""),
        "candidate_conclusion": conclusion,
        "related_entity_id": str(args.get("entity_id") or ""),
    }
    return json.dumps({
        "status": "needs_factmemory_confirmation",
        "message": "This is a candidate claim from a document. Confirm it before writing factmemory.",
        "evidence_packet": evidence_packet,
        "recommended_workflow": [
            "factmemory_context(query=<candidate conclusion and entity/domain keywords>)",
            "factmemory_log(action='write', ... include document_memory evidence packet ...)",
            "factmemory_entity(action='stage', workflow_log_key=<returned key>, ...)",
            "factmemory_entity(action='commit', entity_stage_key=<returned key>)",
        ],
        "boundary": "document_memory never turns RAG recall into confirmed factmemory state by itself.",
    }, ensure_ascii=False)

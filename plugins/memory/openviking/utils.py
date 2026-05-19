"""Shared OpenViking provider utilities."""

from __future__ import annotations

import os
import re
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict
from urllib.parse import quote, urlparse
from urllib.request import url2pathname

_REMOTE_RESOURCE_PREFIXES = ("http://", "https://", "git@", "ssh://", "git://")
_DEFAULT_ENDPOINT = "http://127.0.0.1:1933"
_AUTO_RECALL_SOURCE_MARKER = "Source: openviking-auto-recall"
_OPENVIKING_CONTEXT_OPEN = "<openviking_context>"
_OPENVIKING_CONTEXT_CLOSE = "</openviking_context>"
_RELEVANT_MEMORIES_RE = re.compile(r"<relevant-memories>[\s\S]*?</relevant-memories>", re.IGNORECASE)
_OPENVIKING_CONTEXT_RE = re.compile(r"<openviking_context>[\s\S]*?</openviking_context>", re.IGNORECASE)
_DEFAULT_RECALL_LIMIT = 6
_DEFAULT_RECALL_SCORE_THRESHOLD = 0.15
_DEFAULT_RECALL_MAX_INJECTED_CHARS = 4000
_DEFAULT_COMMIT_TOKEN_THRESHOLD = 20000
_DEFAULT_COMMIT_KEEP_RECENT_COUNT = 10


def _zip_directory(dir_path: Path) -> Path:
    """Create a temporary zip file containing a directory tree."""
    root = dir_path.resolve()
    zip_path = Path(tempfile.gettempdir()) / f"openviking_upload_{uuid.uuid4().hex}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for file_path in dir_path.rglob("*"):
            if file_path.is_symlink():
                continue
            if file_path.is_file():
                try:
                    file_path.resolve().relative_to(root)
                except ValueError:
                    continue
                arcname = str(file_path.relative_to(dir_path)).replace("\\", "/")
                zipf.write(file_path, arcname=arcname)
    return zip_path


def _is_windows_absolute_path(value: str) -> bool:
    return (
        len(value) >= 3
        and value[0].isalpha()
        and value[1] == ":"
        and value[2] in ("/", "\\")
    )


def _is_remote_resource_source(value: str) -> bool:
    return value.startswith(_REMOTE_RESOURCE_PREFIXES)


def _is_local_path_reference(value: str) -> bool:
    if not value or "\n" in value or "\r" in value:
        return False
    if _is_remote_resource_source(value):
        return False
    if _is_windows_absolute_path(value):
        return True
    return (
        value.startswith(("/", "./", "../", "~/", ".\\", "..\\", "~\\"))
        or "/" in value
        or "\\" in value
    )


def _path_from_file_uri(uri: str) -> Path | str:
    parsed = urlparse(uri)
    if parsed.netloc not in ("", "localhost"):
        return f"Unsupported non-local file URI: {uri}"
    return Path(url2pathname(parsed.path)).expanduser()


def _is_resource_uri(uri: str) -> bool:
    normalized = uri.rstrip("/")
    return normalized == "viking://resources" or normalized.startswith("viking://resources/")


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return max(minimum, float(value))
    except (TypeError, ValueError):
        return default


def _strip_relevant_memories(text: str) -> str:
    if not text:
        return ""
    text = _OPENVIKING_CONTEXT_RE.sub(" ", text)
    text = _RELEVANT_MEMORIES_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _context_type_for_uri(uri: str, fallback: str = "") -> str:
    normalized = uri.rstrip("/")
    if normalized == "viking://resources" or normalized.startswith("viking://resources/"):
        return "resource"
    if normalized.startswith("viking://user/") or "/memories/" in normalized or normalized.endswith("/memories"):
        return "memory"
    if normalized.startswith("viking://agent/") and ("/skills/" in normalized or normalized.endswith("/skills")):
        return "skill"
    return fallback


def _agent_scope_uri(agent_id: str, *parts: str) -> str:
    base = f"viking://agent/{agent_id or 'hermes'}"
    suffix = "/".join(part.strip("/") for part in parts if part)
    return f"{base}/{suffix}" if suffix else base


def _url_path_part(value: str) -> str:
    return quote(str(value), safe="")


def _skill_summary_from_result(result: Any) -> Dict[str, Any]:
    if not isinstance(result, dict):
        result = {}
    return {
        "status": result.get("status") or "added",
        "root_uri": result.get("root_uri", ""),
        "uri": result.get("uri", ""),
        "name": result.get("name", ""),
        "auxiliary_files": result.get("auxiliary_files", []),
        "queue_status": result.get("queue_status") or result.get("queue") or {},
        "errors": result.get("errors", []),
        "result": result,
    }


# ---------------------------------------------------------------------------
# MemoryProvider implementation

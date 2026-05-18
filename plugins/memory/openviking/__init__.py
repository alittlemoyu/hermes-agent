"""OpenViking memory plugin — full bidirectional MemoryProvider interface.

Context database by Volcengine (ByteDance) that organizes agent knowledge
into a filesystem hierarchy (viking:// URIs) with tiered context loading,
automatic memory extraction, and session management.

Original PR #3369 by Mibayy, rewritten to use the full OpenViking session
lifecycle instead of read-only search endpoints.

Config via environment variables (profile-scoped via each profile's .env):
  OPENVIKING_ENDPOINT  — Server URL (default: http://127.0.0.1:1933)
  OPENVIKING_API_KEY   — API key (required for authenticated servers)
  OPENVIKING_ACCOUNT   — Tenant account (default: default)
  OPENVIKING_USER      — Tenant user (default: default)
  OPENVIKING_AGENT   — Tenant agent (default: hermes)

Capabilities:
  - Automatic memory extraction on session commit (6 categories)
  - Tiered context: L0 (~100 tokens), L1 (~2k), L2 (full)
  - Semantic search with hierarchical directory retrieval
  - Filesystem-style browsing via viking:// URIs
  - Resource ingestion (URLs, docs, code)
"""

from __future__ import annotations

import atexit
import json
import logging
import mimetypes
import os
import re
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlparse
from urllib.request import url2pathname

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

logger = logging.getLogger(__name__)

_DEFAULT_ENDPOINT = "http://127.0.0.1:1933"
_TIMEOUT = 30.0
_REMOTE_RESOURCE_PREFIXES = ("http://", "https://", "git@", "ssh://", "git://")
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

# Maps the viking_remember `category` enum to a viking:// subdirectory.
# Keep in sync with REMEMBER_SCHEMA.parameters.properties.category.enum.
_CATEGORY_SUBDIR_MAP = {
    "preference": "preferences",
    "entity": "entities",
    "event": "events",
    "case": "cases",
    "pattern": "patterns",
}
_DEFAULT_MEMORY_SUBDIR = "preferences"

# Maps the built-in memory tool's `target` ("user" vs "memory") to a subdir
# for on_memory_write mirroring. User profile facts → preferences; agent
# notes / observations → patterns. Anything unknown falls back to the default.
_MEMORY_WRITE_TARGET_SUBDIR_MAP = {
    "user": "preferences",
    "memory": "patterns",
}


# ---------------------------------------------------------------------------
# Process-level atexit safety net — ensures pending sessions are committed
# even if shutdown_memory_provider is never called (e.g. gateway crash,
# SIGKILL, or exception in the session expiry watcher preventing shutdown).
# ---------------------------------------------------------------------------
_last_active_provider: Optional["OpenVikingMemoryProvider"] = None


def _atexit_commit_sessions():
    """Fire on_session_end for the last active provider on process exit."""
    global _last_active_provider
    provider = _last_active_provider
    if provider is None:
        return
    _last_active_provider = None
    try:
        provider.on_session_end([])
    except Exception:
        pass  # best-effort at shutdown time


atexit.register(_atexit_commit_sessions)


# ---------------------------------------------------------------------------
# HTTP helper — uses httpx to avoid requiring the openviking SDK
# ---------------------------------------------------------------------------

def _get_httpx():
    """Lazy import httpx."""
    try:
        import httpx
        return httpx
    except ImportError:
        return None


class _VikingClient:
    """Thin HTTP client for the OpenViking REST API."""

    def __init__(self, endpoint: str, api_key: str = "",
                 account: str = "", user: str = "", agent: str = ""):
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._account = account or os.environ.get("OPENVIKING_ACCOUNT", "default")
        self._user = user or os.environ.get("OPENVIKING_USER", "default")
        self._agent = agent or os.environ.get("OPENVIKING_AGENT", "hermes")
        self._httpx = _get_httpx()
        if self._httpx is None:
            raise ImportError("httpx is required for OpenViking: pip install httpx")

    def _headers(self) -> dict:
        # Always send tenant headers when account/user are configured.
        # OpenViking 0.3.x requires X-OpenViking-Account and X-OpenViking-User
        # for ROOT API key requests to tenant-scoped APIs — omitting them
        # causes INVALID_ARGUMENT errors even when account="default".
        # User-level keys can omit them (server derives tenancy from the key),
        # but ROOT keys must always include them explicitly.
        h = {
            "Content-Type": "application/json",
            "X-OpenViking-Agent": self._agent,
        }
        if self._account:
            h["X-OpenViking-Account"] = self._account
        if self._user:
            h["X-OpenViking-User"] = self._user
        if self._api_key:
            h["X-API-Key"] = self._api_key
            h["Authorization"] = "Bearer " + self._api_key
        return h

    def _url(self, path: str) -> str:
        return f"{self._endpoint}{path}"

    def _multipart_headers(self) -> dict:
        headers = self._headers()
        headers.pop("Content-Type", None)
        return headers

    def _parse_response(self, resp) -> dict:
        try:
            data = resp.json()
        except Exception:
            data = None

        if resp.status_code >= 400:
            if isinstance(data, dict):
                error = data.get("error")
                if isinstance(error, dict):
                    code = error.get("code", "HTTP_ERROR")
                    message = error.get("message", resp.text)
                    raise RuntimeError(f"{code}: {message}")
                if data.get("status") == "error":
                    raise RuntimeError(str(data))
            resp.raise_for_status()

        if isinstance(data, dict) and data.get("status") == "error":
            error = data.get("error")
            if isinstance(error, dict):
                code = error.get("code", "OPENVIKING_ERROR")
                message = error.get("message", "")
                raise RuntimeError(f"{code}: {message}")
            raise RuntimeError(str(data))

        if data is None:
            return {}
        return data

    def get(self, path: str, **kwargs) -> dict:
        resp = self._httpx.get(
            self._url(path), headers=self._headers(), timeout=_TIMEOUT, **kwargs
        )
        return self._parse_response(resp)

    def post(self, path: str, payload: dict = None, **kwargs) -> dict:
        resp = self._httpx.post(
            self._url(path), json=payload or {}, headers=self._headers(),
            timeout=_TIMEOUT, **kwargs
        )
        return self._parse_response(resp)

    def put(self, path: str, payload: dict = None, **kwargs) -> dict:
        resp = self._httpx.put(
            self._url(path), json=payload or {}, headers=self._headers(),
            timeout=_TIMEOUT, **kwargs
        )
        return self._parse_response(resp)

    def delete(self, path: str, payload: dict = None, **kwargs) -> dict:
        resp = self._httpx.request(
            "DELETE", self._url(path), json=payload or {}, headers=self._headers(),
            timeout=_TIMEOUT, **kwargs
        )
        return self._parse_response(resp)

    def get_text(self, path: str, **kwargs) -> str:
        resp = self._httpx.get(
            self._url(path), headers=self._headers(), timeout=_TIMEOUT, **kwargs
        )
        if resp.status_code >= 400:
            self._parse_response(resp)
        return resp.text

    def upload_temp_file(self, file_path: Path) -> str:
        mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        with file_path.open("rb") as f:
            resp = self._httpx.post(
                self._url("/api/v1/resources/temp_upload"),
                files={"file": (file_path.name, f, mime_type)},
                headers=self._multipart_headers(),
                timeout=_TIMEOUT,
            )
        data = self._parse_response(resp)
        result = data.get("result", {})
        temp_file_id = result.get("temp_file_id", "")
        if not temp_file_id:
            raise RuntimeError("OpenViking temp upload did not return temp_file_id")
        return temp_file_id

    def health(self) -> bool:
        try:
            resp = self._httpx.get(
                self._url("/health"), headers=self._headers(), timeout=3.0
            )
            return resp.status_code == 200
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

SEARCH_SCHEMA = {
    "name": "viking_search",
    "description": (
        "Search the OpenViking knowledge base. "
        "Returns ranked results with viking:// URIs for deeper reading. "
        "For Moyu's fact-memory workflow, use this first as fuzzy recall and "
        "keyword discovery, then confirm official project/task/fact/problem "
        "entities with the fact-memory query.py/ingest.py workflow. "
        "Defaults to OpenViking find for compatibility; use strategy='search' "
        "for session-aware retrieval managed by OpenViking. "
        "Use level to limit results to specific content layers (0=abstract, 1=overview, 2=full)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "strategy": {
                "type": "string", "enum": ["find", "search"],
                "description": "Retrieval API to use: find (default) or session-aware search.",
            },
            "mode": {
                "type": "string", "enum": ["auto", "fast", "deep"],
                "description": "Find-mode search depth (default: auto).",
            },
            "scope": {
                "type": "string",
                "description": "Viking URI prefix to scope search (e.g. 'viking://resources/docs/').",
            },
            "limit": {"type": "integer", "description": "Max results (default: 10)."},
            "score_threshold": {
                "type": "number",
                "description": "Minimum score threshold for OpenViking retrieval.",
            },
            "node_limit": {
                "type": "integer",
                "description": "Maximum number of nodes OpenViking retrieval may consider.",
            },
            "since": {
                "type": "string",
                "description": "Lower time bound for retrieval filtering.",
            },
            "until": {
                "type": "string",
                "description": "Upper time bound for retrieval filtering.",
            },
            "time_field": {
                "type": "string",
                "description": "Metadata time field used with since/until filters.",
            },
            "include_provenance": {
                "type": "boolean",
                "description": "Include provenance data when supported by OpenViking.",
            },
            "telemetry": {
                "type": "boolean",
                "description": "Request OpenViking retrieval telemetry when supported.",
            },
            "level": {
                "type": "string",
                "description": "Limit results to specific layers: '0' (abstract), '1' (overview), '2' (full), or '0,1,2' (default: all).",
            },
        },
        "required": ["query"],
    },
}

READ_SCHEMA = {
    "name": "viking_read",
    "description": (
        "Read content at a viking:// URI. Three detail levels:\n"
        "  abstract — ~100 token summary (L0)\n"
        "  overview — ~2k token key points (L1)\n"
        "  full — complete content (L2)\n"
        "Start with abstract/overview, only use full when you need details."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "uri": {"type": "string", "description": "viking:// URI to read."},
            "level": {
                "type": "string", "enum": ["abstract", "overview", "full"],
                "description": "Detail level (default: overview).",
            },
            "offset": {
                "type": "integer",
                "description": "Byte/character offset for full content reads.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum bytes/characters for full content reads.",
            },
        },
        "required": ["uri"],
    },
}

BROWSE_SCHEMA = {
    "name": "viking_browse",
    "description": (
        "Browse the OpenViking knowledge store like a filesystem.\n"
        "  list — show directory contents\n"
        "  tree — show hierarchy\n"
        "  stat — show metadata for a URI"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string", "enum": ["tree", "list", "stat"],
                "description": "Browse action.",
            },
            "path": {
                "type": "string",
                "description": "Viking URI path (default: viking://). Examples: 'viking://resources/', 'viking://user/memories/'.",
            },
            "simple": {
                "type": "boolean",
                "description": "Request a simplified listing when supported by OpenViking.",
            },
            "recursive": {
                "type": "boolean",
                "description": "Request recursive listing when supported by OpenViking.",
            },
            "level_limit": {
                "type": "integer",
                "description": "Maximum tree depth for action=tree.",
            },
        },
        "required": ["action"],
    },
}

FS_SCHEMA = {
    "name": "viking_fs",
    "description": (
        "Maintain OpenViking filesystem paths through official APIs. "
        "Use explicit actions only: mkdir, mv, rm."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["mkdir", "mv", "rm"],
                "description": "Filesystem maintenance action.",
            },
            "uri": {
                "type": "string",
                "description": "Target viking:// URI for mkdir or rm.",
            },
            "from_uri": {
                "type": "string",
                "description": "Source viking:// URI for mv.",
            },
            "to_uri": {
                "type": "string",
                "description": "Destination viking:// URI for mv.",
            },
            "description": {
                "type": "string",
                "description": "Optional description for mkdir.",
            },
            "recursive": {
                "type": "boolean",
                "description": "Allow recursive remove for rm.",
            },
        },
        "required": ["action"],
    },
}

REMEMBER_SCHEMA = {
    "name": "viking_remember",
    "description": (
        "Explicitly store a fact or memory in the OpenViking knowledge base. "
        "Use content only for concise facts. For larger notes, first put the text "
        "in a local UTF-8 file and pass content_path. "
        "Uses the 8-category memory system:\n"
        "  user/profile.md — user basic info (merged single file)\n"
        "  user/preferences/{topic}/ — user preferences (appendable)\n"
        "  user/entities/{name}/ — entities (people, projects, appendable)\n"
        "  user/events/{calendar:today}/ — event records (immutable)\n"
        "  agent/cases/ — learned cases (immutable)\n"
        "  agent/patterns/ — reusable patterns (mergeable)\n"
        "  agent/tools/ — tool usage experience (mergeable)\n"
        "  agent/skills/ — skill execution experience (mergeable)\n"
        "Supports calendar path variables like {calendar:today}, {calendar:yesterday}."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "maxLength": 8000,
                "description": "Concise information to remember. Keep this short; use content_path for larger notes.",
            },
            "content_path": {
                "type": "string",
                "description": "Local UTF-8 text file containing the memory note. Prefer this for larger content.",
            },
            "category": {
                "type": "string",
                "enum": ["profile", "preference", "entity", "event", "case", "pattern", "tool", "skill"],
                "description": "Memory category. Defaults to auto-detect.",
            },
            "topic": {
                "type": "string",
                "description": "For preferences/entities: the topic or entity name (e.g., 'coding_style', 'ProjectX').",
            },
            "uri": {
                "type": "string",
                "description": "Optional explicit viking:// URI to store at. Overrides category/topic.",
            },
        },
    },
}

WRITE_SCHEMA = {
    "name": "viking_write",
    "description": (
        "Write content directly to a viking:// URI. Creates parent directories if needed. "
        "Use content only for short text. For large text, first put it in a local file "
        "and pass content_path so the model does not have to emit a huge tool-call argument. "
        "Supports calendar path variables like {calendar:today} in the URI."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "uri": {
                "type": "string",
                "description": "Target viking:// URI. Examples: viking://user/memories/notes/todo.md, viking://agent/{agent_id}/memories/patterns/git-workflow.md",
            },
            "content": {
                "type": "string",
                "maxLength": 8000,
                "description": "Short content to write. Keep this small; use content_path for large content to avoid model output limits.",
            },
            "content_path": {
                "type": "string",
                "description": "Local UTF-8 text file whose contents should be written. Prefer this for large content.",
            },
            "delete_content_path_after_write": {
                "type": "boolean",
                "description": "If true with content_path, delete the local file only after OpenViking write succeeds. Default false.",
            },
            "append": {
                "type": "boolean",
                "description": "If true, append to existing content instead of overwriting.",
            },
        },
        "required": ["uri"],
    },
}

LINK_SCHEMA = {
    "name": "viking_link",
    "description": (
        "Create a relationship link between two viking:// URIs. "
        "Use this to connect related memories, resources, or notes. "
        "For example, link a project entity to its related events, "
        "or link a preference to a profile."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "from_uri": {
                "type": "string",
                "description": "Source viking:// URI to link from.",
            },
            "to_uri": {
                "type": "string",
                "description": "Target viking:// URI to link to.",
            },
            "reason": {
                "type": "string",
                "description": "Why these two items are related (improves retrieval context).",
            },
        },
        "required": ["from_uri", "to_uri"],
    },
}

RELATIONS_SCHEMA = {
    "name": "viking_relations",
    "description": (
        "Get all relationships for a given viking:// URI. "
        "Shows what other memories, resources, or notes are linked to this item."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "uri": {
                "type": "string",
                "description": "viking:// URI to query relationships for.",
            },
        },
        "required": ["uri"],
    },
}

SYNC_SKILLS_SCHEMA = {
    "name": "viking_sync_skills",
    "description": (
        "Sync Hermes skills to OpenViking knowledge base. "
        "Scans ~/.hermes/skills/ for SKILL.md files and uploads them to the configured OpenViking agent scope. "
        "This makes Hermes skills searchable via OpenViking semantic search."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "dry_run": {
                "type": "boolean",
                "description": "If true, only list skills that would be synced without uploading.",
            },
        },
    },
}

ADD_SKILL_SCHEMA = {
    "name": "viking_add_skill",
    "description": (
        "Add a skill to OpenViking through the official Skills API. "
        "Provide exactly one of data or path. Data is only for small structured "
        "skill/MCP dicts or short raw SKILL.md text; use path for large SKILL.md "
        "files or skill directories so tool-call arguments stay small. Local paths are uploaded first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "data": {
                "oneOf": [{"type": "object"}, {"type": "string"}],
                "description": "Small structured skill dict, MCP tool dict, or short raw SKILL.md string. Use path for large content.",
            },
            "path": {
                "type": "string",
                "description": "Local SKILL.md file or skill directory containing SKILL.md.",
            },
            "wait": {
                "type": "boolean",
                "description": "Ask OpenViking to wait for skill processing.",
            },
            "timeout": {
                "type": "number",
                "description": "Timeout in seconds when wait is true.",
            },
        },
    },
}

SYSTEM_SCHEMA = {
    "name": "viking_system",
    "description": (
        "Inspect OpenViking health, readiness, status, observers, wait state, or Prometheus metrics."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["health", "ready", "status", "wait", "observer", "metrics"],
                "description": "System action to run.",
            },
            "component": {
                "type": "string",
                "enum": ["queue", "vikingdb", "models"],
                "description": "Observer component for action=observer.",
            },
            "timeout": {
                "type": "number",
                "description": "Timeout in seconds for action=wait.",
            },
            "filter_prefix": {
                "type": "string",
                "description": "Only include Prometheus metric lines with this metric prefix.",
            },
            "max_chars": {
                "type": "integer",
                "description": "Maximum metrics characters to return. Default 12000.",
            },
        },
        "required": ["action"],
    },
}

ADMIN_SCHEMA = {
    "name": "viking_admin",
    "description": (
        "OpenViking multi-tenant Admin API. Read actions run directly; write actions "
        "require Hermes approval before any HTTP request is sent."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "list_accounts", "list_users", "list_agents",
                    "create_account", "delete_account", "register_user",
                    "remove_user", "set_role", "regenerate_key",
                ],
                "description": "Admin action.",
            },
            "account_id": {"type": "string", "description": "OpenViking account/workspace ID."},
            "admin_user_id": {"type": "string", "description": "Initial admin user for create_account."},
            "user_id": {"type": "string", "description": "OpenViking user ID."},
            "role": {
                "type": "string",
                "enum": ["admin", "user", "ADMIN", "USER"],
                "description": "User role for register_user or set_role.",
            },
        },
        "required": ["action"],
    },
}

CONSISTENCY_SCHEMA = {
    "name": "viking_consistency",
    "description": (
        "Check consistency between filesystem content and vector index for a URI subtree. "
        "Useful for debugging missing search results or index issues. "
        "Returns missing records and counts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "description": "viking:// URI to check. Default: viking://",
            },
        },
    },
}

REINDEX_SCHEMA = {
    "name": "viking_reindex",
    "description": (
        "Rebuild semantic products and/or vector index for existing content. "
        "Useful after embedding/VLM model change, vector store refresh, or version upgrade. "
        "Mode 'vectors_only' rebuilds vectors only; 'semantic_and_vectors' regenerates summaries then rebuilds vectors."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "description": "viking:// URI to reindex. Default: viking://",
            },
            "mode": {
                "type": "string",
                "enum": ["vectors_only", "semantic_and_vectors"],
                "description": "Reindex mode. Default: vectors_only.",
            },
            "wait": {
                "type": "boolean",
                "description": "Wait for completion. Default: true.",
            },
        },
    },
}

FIND_SCHEMA = {
    "name": "viking_find",
    "description": (
        "Find files by filename or glob-like pattern within a viking:// scope. "
        "This is path matching via OpenViking glob/tree APIs, not semantic retrieval."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Filename pattern to match (e.g., '*.md', 'profile*').",
            },
            "scope": {
                "type": "string",
                "description": "viking:// URI to search under. Default: viking://",
            },
        },
        "required": ["pattern"],
    },
}

GREP_SCHEMA = {
    "name": "viking_grep",
    "description": (
        "Search file contents with a regular expression pattern. "
        "Like Unix grep — returns matching lines with file URIs and line numbers."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regular expression pattern to search for.",
            },
            "scope": {
                "type": "string",
                "description": "viking:// URI to search under. Default: viking://",
            },
            "case_insensitive": {
                "type": "boolean",
                "description": "Ignore case when matching. Default: false.",
            },
            "level_limit": {
                "type": "integer",
                "description": "Maximum directory traversal depth. Default: 5.",
            },
        },
        "required": ["pattern"],
    },
}

GLOB_SCHEMA = {
    "name": "viking_glob",
    "description": (
        "Find files by glob pattern (e.g., '**/*.md', '*.py'). "
        "Like Unix shell glob — supports * and ** wildcards."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern to match (e.g., '**/*.md', '*.py').",
            },
            "scope": {
                "type": "string",
                "description": "viking:// URI to start from. Default: viking://",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of matches to return.",
            },
        },
        "required": ["pattern"],
    },
}

ADD_RESOURCE_SCHEMA = {
    "name": "viking_add_resource",
    "description": (
        "Add a remote URL or local file/directory to the OpenViking knowledge base. "
        "Remote resources must be public http(s), git, or ssh URLs. "
        "Local files are uploaded first using OpenViking temp_upload. "
        "The system automatically parses, indexes, and generates summaries."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Remote URL or local file/directory path to add."},
            "reason": {
                "type": "string",
                "description": "Why this resource is relevant (improves search).",
            },
            "to": {
                "type": "string",
                "description": "Optional target viking:// URI for the resource.",
            },
            "parent": {
                "type": "string",
                "description": "Optional parent viking:// URI. Cannot be used with to.",
            },
            "instruction": {
                "type": "string",
                "description": "Optional processing instruction for semantic extraction.",
            },
            "wait": {
                "type": "boolean",
                "description": "Whether to wait for processing to complete.",
            },
            "timeout": {
                "type": "number",
                "description": "Timeout in seconds when wait is true.",
            },
            "watch_interval": {
                "type": "number",
                "description": "Polling interval in seconds for watched resource processing.",
            },
        },
        "required": ["url"],
    },
}

ARCHIVE_SCHEMA = {
    "name": "viking_archive",
    "description": (
        "Search or expand OpenViking archived session history. "
        "Use action=search to grep archived turns; use action=expand with an archive_id "
        "to retrieve the original archived messages."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "expand"],
                "description": "Archive action to run.",
            },
            "query": {
                "type": "string",
                "description": "Search query/pattern for action=search.",
            },
            "archive_id": {
                "type": "string",
                "description": "Archive ID for action=expand, or optional scope for action=search.",
            },
            "session_id": {
                "type": "string",
                "description": "OpenViking/Hermes session ID. Defaults to the active session.",
            },
            "case_insensitive": {
                "type": "boolean",
                "description": "Case-insensitive archive search. Default true.",
            },
            "node_limit": {
                "type": "integer",
                "description": "Optional grep node limit.",
            },
            "level_limit": {
                "type": "integer",
                "description": "Optional grep level limit.",
            },
        },
        "required": ["action"],
    },
}


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
# ---------------------------------------------------------------------------

class OpenVikingMemoryProvider(MemoryProvider):
    """Full bidirectional memory via OpenViking context database."""

    def __init__(self):
        self._client: Optional[_VikingClient] = None
        self._endpoint = ""
        self._api_key = ""
        self._account = "default"
        self._user = "default"
        self._agent = os.environ.get("OPENVIKING_AGENT", "hermes")
        self._session_id = ""
        self._turn_count = 0
        self._sync_thread: Optional[threading.Thread] = None
        self._prefetch_result = ""
        self._prefetch_lock = threading.Lock()
        self._prefetch_thread: Optional[threading.Thread] = None
        self._auto_capture = True
        self._auto_recall = True
        self._recall_resources = False
        self._recall_limit = _DEFAULT_RECALL_LIMIT
        self._recall_score_threshold = _DEFAULT_RECALL_SCORE_THRESHOLD
        self._recall_max_injected_chars = _DEFAULT_RECALL_MAX_INJECTED_CHARS
        self._commit_token_threshold = _DEFAULT_COMMIT_TOKEN_THRESHOLD
        self._commit_keep_recent_count = _DEFAULT_COMMIT_KEEP_RECENT_COUNT
        self._capture_signatures: List[str] = []
        self._pending_context_parts: List[Dict[str, Any]] = []
        self._capture_lock = threading.Lock()
        self._agent_context = "primary"
        self._capture_mode = ""
        self._parent_session_id = ""
        self._memory_write_signatures: set[str] = set()

    @property
    def name(self) -> str:
        return "openviking"

    def is_available(self) -> bool:
        """Check if OpenViking endpoint is configured. No network calls."""
        return bool(os.environ.get("OPENVIKING_ENDPOINT"))

    def get_config_schema(self):
        return [
            {
                "key": "endpoint",
                "description": "OpenViking server URL",
                "required": True,
                "default": _DEFAULT_ENDPOINT,
                "env_var": "OPENVIKING_ENDPOINT",
            },
            {
                "key": "api_key",
                "description": "OpenViking API key (leave blank for local dev mode)",
                "secret": True,
                "env_var": "OPENVIKING_API_KEY",
            },
            {
                "key": "account",
                "description": "OpenViking tenant account ID ([default], used when local mode, OPENVIKING_API_KEY is empty)",
                "default": "default",
                "env_var": "OPENVIKING_ACCOUNT",
            },
            {
                "key": "user",
                "description": "OpenViking user ID within the account ([default], used when local mode, OPENVIKING_API_KEY is empty)",
                "default": "default",
                "env_var": "OPENVIKING_USER",
            },
            {
                "key": "agent",
                "description": "OpenViking agent ID within the account ([hermes], useful in multi-agent mode)",
                "default": "hermes",
                "env_var": "OPENVIKING_AGENT",
            },
            {
                "key": "auto_recall",
                "description": "Enable OpenClaw-style automatic memory recall before turns",
                "default": "true",
                "env_var": "OPENVIKING_AUTO_RECALL",
            },
            {
                "key": "auto_capture",
                "description": "Enable OpenClaw-style automatic turn capture and threshold commits",
                "default": "true",
                "env_var": "OPENVIKING_AUTO_CAPTURE",
            },
            {
                "key": "recall_resources",
                "description": "Include viking://resources in automatic recall (default false)",
                "default": "false",
                "env_var": "OPENVIKING_RECALL_RESOURCES",
            },
            {
                "key": "commit_token_threshold",
                "description": "Pending-token threshold for background session commit",
                "default": str(_DEFAULT_COMMIT_TOKEN_THRESHOLD),
                "env_var": "OPENVIKING_COMMIT_TOKEN_THRESHOLD",
            },
        ]

    def initialize(self, session_id: str, **kwargs) -> None:
        self._endpoint = os.environ.get("OPENVIKING_ENDPOINT", _DEFAULT_ENDPOINT)
        self._api_key = os.environ.get("OPENVIKING_API_KEY", "")
        self._account = os.environ.get("OPENVIKING_ACCOUNT", "default")
        self._user = os.environ.get("OPENVIKING_USER", "default")
        self._agent = os.environ.get("OPENVIKING_AGENT", "hermes")
        self._session_id = session_id
        self._turn_count = 0
        self._agent_context = str(kwargs.get("agent_context") or "primary")
        self._capture_mode = str(kwargs.get("capture_mode") or "")
        self._parent_session_id = str(kwargs.get("parent_session_id") or "")
        self._auto_capture = _env_bool("OPENVIKING_AUTO_CAPTURE", True)
        if self._agent_context == "background_review" or self._capture_mode == "artifact_only":
            self._auto_capture = False
        self._auto_recall = _env_bool("OPENVIKING_AUTO_RECALL", True)
        self._recall_resources = _env_bool("OPENVIKING_RECALL_RESOURCES", False)
        self._recall_limit = _env_int("OPENVIKING_RECALL_LIMIT", _DEFAULT_RECALL_LIMIT, minimum=1)
        self._recall_score_threshold = _env_float(
            "OPENVIKING_RECALL_SCORE_THRESHOLD",
            _DEFAULT_RECALL_SCORE_THRESHOLD,
            minimum=0.0,
        )
        self._recall_max_injected_chars = _env_int(
            "OPENVIKING_RECALL_MAX_INJECTED_CHARS",
            _DEFAULT_RECALL_MAX_INJECTED_CHARS,
            minimum=0,
        )
        self._commit_token_threshold = _env_int(
            "OPENVIKING_COMMIT_TOKEN_THRESHOLD",
            _DEFAULT_COMMIT_TOKEN_THRESHOLD,
            minimum=0,
        )
        self._commit_keep_recent_count = _env_int(
            "OPENVIKING_COMMIT_KEEP_RECENT_COUNT",
            _DEFAULT_COMMIT_KEEP_RECENT_COUNT,
            minimum=0,
        )

        try:
            self._client = _VikingClient(
                self._endpoint, self._api_key,
                account=self._account, user=self._user, agent=self._agent,
            )
            if not self._client.health():
                logger.warning("OpenViking server at %s is not reachable", self._endpoint)
                self._client = None
                return
        except ImportError:
            logger.warning("httpx not installed — OpenViking plugin disabled")
            self._client = None
            return

        # Create the OpenViking session if it doesn't exist.
        # OpenViking requires explicit session creation before messages can be
        # written. We use the Hermes session_id as the OpenViking session_id
        # so the sync_turn/on_session_end lifecycle maps 1:1.
        try:
            self._client.post("/api/v1/sessions", {"session_id": session_id})
            logger.info("OpenViking session %s created", session_id)
        except Exception as e:
            # Session may already exist (resume, /branch, etc.) — that's fine.
            err_str = str(e).lower()
            if "exist" in err_str or "duplicate" in err_str or "already" in err_str:
                logger.debug("OpenViking session %s already exists", session_id)
            else:
                logger.warning("OpenViking session creation failed: %s", e)

        # Register as the last active provider for atexit safety net
        global _last_active_provider
        _last_active_provider = self

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
                client = _VikingClient(
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

        def _sync():
            try:
                client = _VikingClient(
                    self._endpoint, self._api_key,
                    account=self._account, user=self._user, agent=self._agent,
                )
                sid_part = _url_path_part(sid)
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
                self._commit_if_threshold_reached(client, sid)
                logger.debug(
                    "OpenViking sync_turn: session=%s messages=%s contexts=%d",
                    sid,
                    posted,
                    len(context_uris),
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
                client = _VikingClient(
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

<<<<<<< HEAD
    def _build_memory_uri(self, subdir: str) -> str:
        """Build a viking:// memory URI under the configured user/subdir."""
        slug = uuid.uuid4().hex[:12]
        return f"viking://user/{self._user}/memories/{subdir}/mem_{slug}.md"
=======
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
>>>>>>> 8931a76f0 (restore openviking runtime integration)

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
<<<<<<< HEAD
        """Mirror built-in memory writes to OpenViking via content/write."""
        if not self._client or action != "add" or not content:
=======
        """Mirror built-in memory writes to OpenViking as explicit memories."""
        if not self._client or action not in {"add", "replace"} or not content:
>>>>>>> 8931a76f0 (restore openviking runtime integration)
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

        subdir = _MEMORY_WRITE_TARGET_SUBDIR_MAP.get(target, _DEFAULT_MEMORY_SUBDIR)
        uri = self._build_memory_uri(subdir)

        def _write():
            try:
                client = _VikingClient(
                    self._endpoint, self._api_key,
                    account=self._account, user=self._user, agent=self._agent,
                )
<<<<<<< HEAD
                client.post("/api/v1/content/write", {
                    "uri": uri,
                    "content": content,
                    "mode": "create",
=======
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
>>>>>>> 8931a76f0 (restore openviking runtime integration)
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

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [SEARCH_SCHEMA, READ_SCHEMA, BROWSE_SCHEMA, FS_SCHEMA, REMEMBER_SCHEMA, WRITE_SCHEMA, LINK_SCHEMA, RELATIONS_SCHEMA, FIND_SCHEMA, GREP_SCHEMA, GLOB_SCHEMA, ADD_RESOURCE_SCHEMA, ARCHIVE_SCHEMA, ADD_SKILL_SCHEMA, SYNC_SKILLS_SCHEMA, SYSTEM_SCHEMA, ADMIN_SCHEMA, CONSISTENCY_SCHEMA, REINDEX_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if not self._client:
            return tool_error("OpenViking server not connected")

        try:
            if tool_name == "viking_search":
                return self._tool_search(args)
            elif tool_name == "viking_read":
                return self._tool_read(args)
            elif tool_name == "viking_browse":
                return self._tool_browse(args)
            elif tool_name == "viking_fs":
                return self._tool_fs(args)
            elif tool_name == "viking_remember":
                return self._tool_remember(args)
            elif tool_name == "viking_write":
                return self._tool_write(args)
            elif tool_name == "viking_link":
                return self._tool_link(args)
            elif tool_name == "viking_relations":
                return self._tool_relations(args)
            elif tool_name == "viking_find":
                return self._tool_find(args)
            elif tool_name == "viking_grep":
                return self._tool_grep(args)
            elif tool_name == "viking_glob":
                return self._tool_glob(args)
            elif tool_name == "viking_add_resource":
                return self._tool_add_resource(args)
            elif tool_name == "viking_archive":
                return self._tool_archive(args)
            elif tool_name == "viking_add_skill":
                return self._tool_add_skill(args)
            elif tool_name == "viking_sync_skills":
                return self._tool_sync_skills(args)
            elif tool_name == "viking_system":
                return self._tool_system(args)
            elif tool_name == "viking_admin":
                return self._tool_admin(args)
            elif tool_name == "viking_consistency":
                return self._tool_consistency(args)
            elif tool_name == "viking_reindex":
                return self._tool_reindex(args)
            return tool_error(f"Unknown tool: {tool_name}")
        except Exception as e:
            return tool_error(str(e))

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
            return tool_error("query is required")

        payload: Dict[str, Any] = {"query": query}
        strategy = args.get("strategy", "find")
        if strategy not in ("find", "search"):
            return tool_error("strategy must be 'find' or 'search'")
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
        for key in ("query_plan", "query_results"):
            if key in result:
                output[key] = result.get(key)

        return json.dumps(output, ensure_ascii=False)

    def _tool_read(self, args: dict) -> str:
        uri = args.get("uri", "")
        if not uri:
            return tool_error("uri is required")

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
                return tool_error("uri is required for mkdir")
            payload: Dict[str, Any] = {"uri": uri}
            if args.get("description"):
                payload["description"] = args["description"]
            result = self._unwrap_result(self._client.post("/api/v1/fs/mkdir", payload))
            return json.dumps({"status": "created", "action": action, "uri": uri, "result": result}, ensure_ascii=False)

        if action == "mv":
            from_uri = args.get("from_uri", "")
            to_uri = args.get("to_uri", "")
            if not from_uri:
                return tool_error("from_uri is required for mv")
            if not to_uri:
                return tool_error("to_uri is required for mv")
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
                return tool_error("uri is required for rm")
            payload = {"uri": uri, "recursive": bool(args.get("recursive", False))}
            result = self._unwrap_result(self._client.delete("/api/v1/fs/rm", payload))
            return json.dumps({
                "status": "removed",
                "action": action,
                "uri": uri,
                "recursive": payload["recursive"],
                "result": result,
            }, ensure_ascii=False)

        return tool_error("action must be one of: mkdir, mv, rm")

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

<<<<<<< HEAD
        category = args.get("category", "")
        subdir = _CATEGORY_SUBDIR_MAP.get(category, _DEFAULT_MEMORY_SUBDIR)
        uri = self._build_memory_uri(subdir)

        # Write directly via content/write API.
        # This creates the file, stores the content, and queues vector indexing
        # in a single call — no dependency on session commit / VLM extraction.
        try:
            result = self._client.post("/api/v1/content/write", {
                "uri": uri,
                "content": content,
                "mode": "create",
            })
            written = result.get("result", {}).get("written_bytes", 0)
            return json.dumps({
                "status": "stored",
                "message": f"Memory stored ({written}b) and queued for vector indexing.",
            })
        except Exception as e:
            logger.error("OpenViking content/write failed: %s", e)
            return tool_error(f"Failed to store memory: {e}")
=======
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
            return tool_error("uri is required")
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
                source_path = path
            except UnicodeDecodeError:
                return tool_error(f"content_path must be a UTF-8 text file: {content_path}")
            except Exception as e:
                return tool_error(f"Failed to read content_path: {e}")
        if not content:
            return tool_error("content or content_path is required")

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
            return tool_error("pattern is required")

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
>>>>>>> 8931a76f0 (restore openviking runtime integration)

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


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register OpenViking as a memory provider plugin."""
    ctx.register_memory_provider(OpenVikingMemoryProvider())

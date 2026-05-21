"""OpenViking tool schema definitions."""

from __future__ import annotations

from typing import Any, Dict, List

from .tool_policy import public_tool_names


SEARCH_SCHEMA = {
    "name": "viking_search",
    "description": (
        "Canonical semantic retrieval entry point for the OpenViking knowledge base. "
        "Returns ranked results with viking:// URIs for deeper reading. "
        "For Moyu's fact-memory workflow, use this first as fuzzy recall and "
        "keyword discovery, then confirm official project/task/fact/problem "
        "entities with the fact-memory query.py/ingest.py workflow. "
        "Defaults to OpenViking find() for simple low-latency semantic recall; "
        "use strategy='search' for OpenViking session-aware intent analysis and rerank. "
        "This is not filename/path matching; use viking_glob for AGFS path matches. "
        "Use level to limit results to specific content layers (0=abstract, 1=overview, 2=full)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "strategy": {
                "type": "string", "enum": ["find", "search"],
                "description": "OpenViking retrieval API: find for simple semantic recall (default), search for session-aware intent/rerank.",
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
            "packet": {
                "type": "boolean",
                "description": "Return an explainable retrieval governance packet in addition to legacy results. Default true.",
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
        "Maintenance-layer OpenViking filesystem mutations through official APIs. "
        "Use explicit actions only: mkdir, mv, rm. Do not use for ordinary read/search work."
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
        "Explicitly submit a concise memory hint to OpenViking's memory/extraction system. "
        "This is not the durable write path for fact-memory project/task/fact/problem entities; "
        "use fact-memory duplicate checks and ingest for those structured records. "
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
        "Write exact content directly to a viking:// AGFS URI. Creates parent directories if needed. "
        "This is for OpenViking AGFS content, not the official fact-memory entity write path. "
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
        "Maintenance-layer inspection of OpenViking health, readiness, status, observers, wait state, or Prometheus metrics."
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
        "Maintenance-layer consistency check between AGFS content and vector index for a URI subtree. "
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
        "Maintenance-layer rebuild of semantic products and/or vector index for existing content. "
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
        "Compatibility sugar for finding files by filename or glob-like pattern within a viking:// scope. "
        "For new path-matching work, prefer viking_glob. "
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
        "Canonical AGFS filename/path matching by glob pattern (e.g., '**/*.md', '*.py'). "
        "Like Unix shell glob — supports * and ** wildcards. "
        "Use viking_search for semantic retrieval instead."
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



def get_openviking_tool_schemas() -> List[Dict[str, Any]]:
    schemas_by_name = {
        schema["name"]: schema
        for schema in (
            SEARCH_SCHEMA, READ_SCHEMA, BROWSE_SCHEMA, FS_SCHEMA,
            REMEMBER_SCHEMA, WRITE_SCHEMA, LINK_SCHEMA, RELATIONS_SCHEMA,
            FIND_SCHEMA, GREP_SCHEMA, GLOB_SCHEMA, ADD_RESOURCE_SCHEMA,
            ARCHIVE_SCHEMA, ADD_SKILL_SCHEMA, SYNC_SKILLS_SCHEMA,
            SYSTEM_SCHEMA, ADMIN_SCHEMA, CONSISTENCY_SCHEMA, REINDEX_SCHEMA,
        )
    }
    return [schemas_by_name[name] for name in public_tool_names()]

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


_LOCAL_TOOL_CONTRACTS = {
    "viking_search": {
        "when_to_use": ["Semantic recall, fuzzy keyword discovery, and finding viking:// URIs to read."],
        "when_not_to_use": ["Do not use search results as confirmed factmemory truth or path matching."],
        "required_before_call": ["query"],
        "next_action": "Read promising URIs with viking_read; confirm structured facts through factmemory before writes.",
        "dangerous_misroutes": ["Writing factmemory entities directly from recall-only results.", "Using semantic search for filename/path matching."],
        "write_policy": "read_only_retrieval",
        "recovery_hint": "If results are broad, add scope/level or use viking_glob for path matching.",
    },
    "viking_read": {
        "when_to_use": ["Read a known viking:// URI after search/browse/glob."],
        "when_not_to_use": ["Do not use full reads by default when abstract/overview is enough."],
        "required_before_call": ["uri"],
        "next_action": "Use read content as evidence; promote structured facts through factmemory log + stage + commit.",
        "dangerous_misroutes": ["Treating read content as an automatic confirmed factmemory entity update."],
        "write_policy": "read_only_content_read",
        "recovery_hint": "If summary reads fail or lack detail, retry with level='full' and offset/limit.",
    },
    "viking_browse": {
        "when_to_use": ["List/tree/stat OpenViking AGFS paths."],
        "when_not_to_use": ["Do not use it for semantic search."],
        "required_before_call": ["action"],
        "next_action": "Use returned URIs with viking_read or viking_search scoped to a subtree.",
        "dangerous_misroutes": ["Assuming listed OpenViking paths are local factmemory source files."],
        "write_policy": "read_only_agfs_browse",
        "recovery_hint": "Use stat on a specific URI when tree/list output is ambiguous.",
    },
    "viking_fs": {
        "when_to_use": ["Explicit OpenViking AGFS maintenance: mkdir, mv, rm."],
        "when_not_to_use": ["Do not use for ordinary recall, factmemory writes, or local filesystem changes."],
        "required_before_call": ["action plus uri/from_uri/to_uri required by that action"],
        "next_action": "Verify with viking_browse or viking_read after maintenance.",
        "dangerous_misroutes": ["Removing OpenViking resources because factmemory state looks stale.", "Confusing viking:// paths with local files."],
        "write_policy": "openviking_agfs_mutation_only",
        "recovery_hint": "If unsure, browse/stat first; do not rm without explicit owner intent.",
    },
    "viking_remember": {
        "when_to_use": ["Submit concise recall hints to OpenViking memory/extraction."],
        "when_not_to_use": ["Do not use as durable Project/Task/Fact/Problem write path."],
        "required_before_call": ["content or content_path"],
        "next_action": "For structured truth, also record through factmemory log + entity stage/commit.",
        "dangerous_misroutes": ["Treating auto-extracted OpenViking memory as confirmed factmemory state."],
        "write_policy": "openviking_memory_hint_only",
        "recovery_hint": "Use content_path for large notes; search/read after indexing to verify recall visibility.",
    },
    "viking_write": {
        "when_to_use": ["Write exact OpenViking AGFS content when OpenViking is the intended target."],
        "when_not_to_use": ["Do not use for factmemory entities, daily logs, or local files."],
        "required_before_call": ["uri plus content or content_path"],
        "next_action": "Verify with viking_read; mirror factmemory facts through factmemory workflow instead.",
        "dangerous_misroutes": ["Writing structured truth only to OpenViking and skipping factmemory."],
        "write_policy": "openviking_agfs_write_only",
        "recovery_hint": "Use append carefully; read existing content before overwrite-sensitive updates.",
    },
    "viking_link": {
        "when_to_use": ["Create OpenViking URI relationships to improve retrieval context."],
        "when_not_to_use": ["Do not use as factmemory relation source of truth."],
        "required_before_call": ["from_uri", "to_uri"],
        "next_action": "Inspect with viking_relations after linking.",
        "dangerous_misroutes": ["Assuming OpenViking links update .memory/entities relations."],
        "write_policy": "openviking_relation_write_only",
        "recovery_hint": "Confirm both URIs with viking_read/stat before linking important records.",
    },
    "viking_relations": {
        "when_to_use": ["Inspect OpenViking relationships for a URI."],
        "when_not_to_use": ["Do not use as authoritative factmemory graph_check."],
        "required_before_call": ["uri"],
        "next_action": "Use relation hints for recall; confirm entity graph with factmemory graph_check.",
        "dangerous_misroutes": ["Treating OpenViking relation absence as proof no factmemory relation exists."],
        "write_policy": "read_only_relation_inspection",
        "recovery_hint": "If relations are missing, browse/read source URIs and check factmemory graph separately.",
    },
    "viking_find": {
        "when_to_use": ["Compatibility filename matching."],
        "when_not_to_use": ["Prefer viking_glob for new path matching; do not use for semantic recall."],
        "required_before_call": ["pattern"],
        "next_action": "Read matching URIs or switch to viking_search for semantic needs.",
        "dangerous_misroutes": ["Expecting semantic matches from filename matching."],
        "write_policy": "read_only_path_match",
        "recovery_hint": "Use viking_glob with scope for clearer path matching.",
    },
    "viking_grep": {
        "when_to_use": ["Regex search content in OpenViking AGFS files."],
        "when_not_to_use": ["Do not use for semantic similarity or confirmed factmemory truth."],
        "required_before_call": ["pattern"],
        "next_action": "Read matched URIs around relevant lines.",
        "dangerous_misroutes": ["Using grep misses as proof that a concept is absent semantically."],
        "write_policy": "read_only_text_match",
        "recovery_hint": "Broaden regex/scope or use viking_search when wording may differ.",
    },
    "viking_glob": {
        "when_to_use": ["Canonical OpenViking filename/path matching."],
        "when_not_to_use": ["Do not use for semantic retrieval."],
        "required_before_call": ["pattern"],
        "next_action": "Read or browse matched URIs.",
        "dangerous_misroutes": ["Using glob when the user asked for concept recall."],
        "write_policy": "read_only_path_match",
        "recovery_hint": "Adjust scope or pattern; use ** for recursive matches.",
    },
    "viking_add_resource": {
        "when_to_use": ["Add a URL/local file/directory as an OpenViking resource."],
        "when_not_to_use": ["Do not use as document_memory registry import when local document metadata/dedupe is needed."],
        "required_before_call": ["url"],
        "next_action": "Search/read after processing; use document_import for document-memory registry workflows.",
        "dangerous_misroutes": ["Assuming added resources become confirmed factmemory facts."],
        "write_policy": "openviking_resource_ingest_only",
        "recovery_hint": "Use wait=true for immediate verification, or search after background processing completes.",
    },
    "viking_archive": {
        "when_to_use": ["Search or expand archived Hermes/OpenViking session history."],
        "when_not_to_use": ["Do not treat session recall as confirmed factmemory truth."],
        "required_before_call": ["action plus query or archive_id as required"],
        "next_action": "Use findings as evidence; confirm durable facts through factmemory.",
        "dangerous_misroutes": ["Promoting remembered conversation text directly to entity state."],
        "write_policy": "read_only_session_archive",
        "recovery_hint": "Use expand on a promising archive_id for original context.",
    },
    "viking_add_skill": {
        "when_to_use": ["Add a skill to OpenViking's skill/resource index."],
        "when_not_to_use": ["Do not install or modify live Hermes skills with this tool."],
        "required_before_call": ["data or path"],
        "next_action": "Search/read skill resource after processing; install live skills through the skill workflow only.",
        "dangerous_misroutes": ["Confusing OpenViking skill indexing with enabling a live skill."],
        "write_policy": "openviking_skill_index_write_only",
        "recovery_hint": "Use path for large SKILL.md or directories; wait=true when verification is needed.",
    },
    "viking_sync_skills": {
        "when_to_use": ["Sync existing Hermes skills into OpenViking search."],
        "when_not_to_use": ["Do not use to edit or promote skills."],
        "required_before_call": ["dry_run=true first for review when scope is uncertain."],
        "next_action": "Run without dry_run only after confirming sync scope; then search for skill content.",
        "dangerous_misroutes": ["Treating sync as skill installation or update."],
        "write_policy": "openviking_skill_index_sync",
        "recovery_hint": "Use dry_run to see affected skills before upload.",
    },
    "viking_system": {
        "when_to_use": ["Inspect OpenViking health, readiness, status, wait state, observers, or metrics."],
        "when_not_to_use": ["Do not use for content recall or writes."],
        "required_before_call": ["action"],
        "next_action": "Use failures to guide service/runtime diagnosis.",
        "dangerous_misroutes": ["Treating system readiness as evidence about memory content correctness."],
        "write_policy": "read_only_system_inspection",
        "recovery_hint": "Use observer/metrics for deeper diagnosis after health/ready failures.",
    },
    "viking_admin": {
        "when_to_use": ["Explicit OpenViking multi-tenant admin operations."],
        "when_not_to_use": ["Do not use for ordinary memory, resource, or factmemory work."],
        "required_before_call": ["action and required tenant/user fields; write actions require approval."],
        "next_action": "Verify read state after admin writes.",
        "dangerous_misroutes": ["Changing accounts/users/keys while trying to fix recall content."],
        "write_policy": "admin_write_requires_approval",
        "recovery_hint": "Use list actions first; do not run destructive admin actions without explicit owner intent.",
    },
    "viking_consistency": {
        "when_to_use": ["Check AGFS/vector index consistency for missing search results."],
        "when_not_to_use": ["Do not use as entity graph or factmemory consistency check."],
        "required_before_call": ["Optional scope; default viking:// may be broad."],
        "next_action": "Use viking_reindex when consistency output shows index drift.",
        "dangerous_misroutes": ["Assuming vector consistency proves fact correctness."],
        "write_policy": "read_only_index_diagnostics",
        "recovery_hint": "Narrow scope before broad checks if output is large.",
    },
    "viking_reindex": {
        "when_to_use": ["Rebuild OpenViking semantic products/vector index for existing content."],
        "when_not_to_use": ["Do not use to change source content or factmemory state."],
        "required_before_call": ["scope/mode; wait=true when verification is needed."],
        "next_action": "Run viking_consistency or viking_search after reindexing.",
        "dangerous_misroutes": ["Using reindex to paper over wrong or stale source facts."],
        "write_policy": "openviking_index_maintenance_write",
        "recovery_hint": "Start with vectors_only; use semantic_and_vectors when summaries are stale.",
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
    for schema in schemas_by_name.values():
        schema.update(_LOCAL_TOOL_CONTRACTS[schema["name"]])
    return [schemas_by_name[name] for name in public_tool_names()]

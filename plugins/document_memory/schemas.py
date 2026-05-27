"""Tool schemas for the lightweight document-memory layer."""

DOCUMENT_IMPORT_SCHEMA = {
    "name": "document_import",
    "description": (
        "Register a local file or URL as a document resource and ask OpenViking "
        "to index it under viking://resources/document-memory/<collection>/. "
        "This creates/updates the document registry only; imported text is not "
        "a factmemory fact until explicitly confirmed and promoted."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Local file path, file:// URI, or http(s) URL to import."},
            "collection": {"type": "string", "description": "Collection/domain name, e.g. study_abroad, production_docs, hermes_refs."},
            "title": {"type": "string", "description": "Human title. Defaults to filename or URL basename."},
            "doc_type": {"type": "string", "description": "Optional document type, e.g. pdf, manual, policy, spreadsheet, web."},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "Search/filter tags."},
            "wait": {"type": "boolean", "description": "Wait for OpenViking background indexing when supported. Default false."},
            "force": {"type": "boolean", "description": "Re-index even if the same local file hash already exists. Default false."},
        },
        "required": ["path"],
    },
}

DOCUMENT_SEARCH_SCHEMA = {
    "name": "document_search",
    "description": (
        "Search document resources through OpenViking, optionally scoped by "
        "collection and annotated with local registry metadata. Results are "
        "RAG evidence only, not confirmed factmemory facts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Semantic search query."},
            "collection": {"type": "string", "description": "Optional collection scope."},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional registry tag filter."},
            "limit": {"type": "integer", "description": "Maximum result count. Default 10."},
            "level": {"type": "string", "description": "OpenViking retrieval level filter, e.g. 0,1,2."},
        },
        "required": ["query"],
    },
}

DOCUMENT_READ_SCHEMA = {
    "name": "document_read",
    "description": "Read a registered document or viking:// document URI at abstract, overview, or full detail.",
    "parameters": {
        "type": "object",
        "properties": {
            "doc_id": {"type": "string", "description": "Registered document id."},
            "uri": {"type": "string", "description": "viking:// URI to read directly."},
            "level": {"type": "string", "enum": ["abstract", "overview", "full"], "description": "Detail level. Default overview."},
            "offset": {"type": "integer", "description": "Offset for full reads."},
            "limit": {"type": "integer", "description": "Maximum characters for full reads."},
        },
    },
}

DOCUMENT_LIST_SCHEMA = {
    "name": "document_list",
    "description": "List registered documents from the local document registry.",
    "parameters": {
        "type": "object",
        "properties": {
            "collection": {"type": "string", "description": "Optional collection filter."},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tag filter."},
            "status": {"type": "string", "description": "Optional status filter."},
            "limit": {"type": "integer", "description": "Maximum documents to return. Default 50."},
        },
    },
}

DOCUMENT_LINK_ENTITY_SCHEMA = {
    "name": "document_link_entity",
    "description": "Link a registered document to a factmemory entity id in the registry metadata only.",
    "parameters": {
        "type": "object",
        "properties": {
            "doc_id": {"type": "string", "description": "Registered document id."},
            "entity_id": {"type": "string", "description": "Factmemory entity id, e.g. project/foo or fact/bar."},
            "reason": {"type": "string", "description": "Why this document supports or relates to the entity."},
        },
        "required": ["doc_id", "entity_id"],
    },
}

DOCUMENT_PROMOTE_TO_FACT_SCHEMA = {
    "name": "document_promote_to_fact",
    "description": (
        "Prepare a factmemory evidence packet from a document conclusion. "
        "This never writes factmemory directly; it returns the required "
        "context -> log -> stage -> commit path and source evidence."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "doc_id": {"type": "string", "description": "Registered document id."},
            "conclusion": {"type": "string", "description": "Candidate conclusion to confirm before writing factmemory."},
            "evidence": {"type": "string", "description": "Short quote, section, page, or locator supporting the conclusion."},
            "entity_id": {"type": "string", "description": "Optional target or related factmemory entity id."},
        },
        "required": ["doc_id", "conclusion"],
    },
}

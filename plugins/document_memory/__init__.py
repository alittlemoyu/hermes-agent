"""Lightweight document-memory plugin for Hermes.

The document layer deliberately sits between raw resources and factmemory:
documents are registered locally and indexed through OpenViking, while only
human-confirmed conclusions should be promoted through factmemory's normal
context -> log -> stage -> commit workflow.
"""

from __future__ import annotations

from .schemas import (
    DOCUMENT_IMPORT_SCHEMA,
    DOCUMENT_LINK_ENTITY_SCHEMA,
    DOCUMENT_LIST_SCHEMA,
    DOCUMENT_PROMOTE_TO_FACT_SCHEMA,
    DOCUMENT_READ_SCHEMA,
    DOCUMENT_SEARCH_SCHEMA,
)
from .store import (
    handle_document_import,
    handle_document_link_entity,
    handle_document_list,
    handle_document_promote_to_fact,
    handle_document_read,
    handle_document_search,
)

_TOOLS = (
    ("document_import", DOCUMENT_IMPORT_SCHEMA, handle_document_import, ""),
    ("document_search", DOCUMENT_SEARCH_SCHEMA, handle_document_search, ""),
    ("document_read", DOCUMENT_READ_SCHEMA, handle_document_read, ""),
    ("document_list", DOCUMENT_LIST_SCHEMA, handle_document_list, ""),
    ("document_link_entity", DOCUMENT_LINK_ENTITY_SCHEMA, handle_document_link_entity, ""),
    ("document_promote_to_fact", DOCUMENT_PROMOTE_TO_FACT_SCHEMA, handle_document_promote_to_fact, ""),
)


def register(ctx) -> None:
    """Register document-memory tools. Called by the Hermes plugin loader."""
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="document_memory",
            schema=schema,
            handler=handler,
            emoji=emoji,
        )

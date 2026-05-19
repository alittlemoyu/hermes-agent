"""Tool exposure policy for the OpenViking memory provider.

The provider follows OpenViking concepts first. Public tools should expose one
canonical owner per capability; compatibility aliases stay callable but are not
shown to the model unless explicitly enabled.
"""

from __future__ import annotations

import os
from typing import Tuple


DEFAULT_TOOL_NAMES: Tuple[str, ...] = (
    "viking_search",
    "viking_read",
    "viking_browse",
    "viking_fs",
    "viking_remember",
    "viking_write",
    "viking_link",
    "viking_relations",
    "viking_grep",
    "viking_glob",
    "viking_add_resource",
    "viking_archive",
    "viking_add_skill",
    "viking_sync_skills",
    "viking_system",
    "viking_admin",
    "viking_consistency",
    "viking_reindex",
)

COMPAT_TOOL_NAMES: Tuple[str, ...] = (
    "viking_find",
)

TOOL_LAYERS = {
    "retrieval": ("viking_search", "viking_read", "viking_browse", "viking_archive"),
    "agfs": ("viking_glob", "viking_grep", "viking_fs", "viking_write"),
    "memory": ("viking_remember", "viking_link", "viking_relations"),
    "resources": ("viking_add_resource", "viking_add_skill", "viking_sync_skills"),
    "maintenance": ("viking_system", "viking_admin", "viking_consistency", "viking_reindex"),
    "compat": COMPAT_TOOL_NAMES,
}


def expose_compat_tools() -> bool:
    raw = os.getenv("OPENVIKING_EXPOSE_COMPAT_TOOLS", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def public_tool_names() -> Tuple[str, ...]:
    if expose_compat_tools():
        return DEFAULT_TOOL_NAMES + COMPAT_TOOL_NAMES
    return DEFAULT_TOOL_NAMES

"""OpenViking MemoryProvider adapter for Hermes."""

from __future__ import annotations

import atexit
import logging
import os
import threading
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

from .client import _VikingClient
from .lifecycle import OpenVikingLifecycleMixin
from .tools import OpenVikingToolMixin
from .utils import (
    _DEFAULT_COMMIT_KEEP_RECENT_COUNT,
    _DEFAULT_COMMIT_TOKEN_THRESHOLD,
    _DEFAULT_ENDPOINT,
    _DEFAULT_RECALL_LIMIT,
    _DEFAULT_RECALL_MAX_INJECTED_CHARS,
    _DEFAULT_RECALL_SCORE_THRESHOLD,
    _env_bool,
    _env_float,
    _env_int,
)

logger = logging.getLogger(__name__)


_last_active_provider: Optional["OpenVikingMemoryProvider"] = None


def _client_class():
    import plugins.memory.openviking as openviking_package

    return getattr(openviking_package, "_VikingClient", _VikingClient)


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


class OpenVikingMemoryProvider(OpenVikingLifecycleMixin, OpenVikingToolMixin, MemoryProvider):
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
            self._client = _client_class()(
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



def register(ctx) -> None:
    """Register OpenViking as a memory provider plugin."""
    ctx.register_memory_provider(OpenVikingMemoryProvider())

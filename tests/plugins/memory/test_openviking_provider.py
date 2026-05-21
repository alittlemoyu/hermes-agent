import inspect
import json
import logging
import zipfile
from importlib import reload
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

import plugins.memory.openviking as openviking_module
import plugins.memory.openviking.tool_policy as openviking_tool_policy
from plugins.memory.openviking import OpenVikingMemoryProvider, _VikingClient


def test_openviking_package_is_export_layer():
    init_path = Path(openviking_module.__file__)
    init_source = init_path.read_text()

    assert len(init_source.splitlines()) < 40
    assert "OpenVikingMemoryProvider" in openviking_module.__all__
    assert OpenVikingMemoryProvider.__module__ == "plugins.memory.openviking.provider"
    assert _VikingClient.__module__ == "plugins.memory.openviking.client"


def test_openviking_provider_keeps_lifecycle_and_tools_split():
    assert inspect.getmodule(OpenVikingMemoryProvider.sync_turn).__name__ == "plugins.memory.openviking.lifecycle"
    assert inspect.getmodule(OpenVikingMemoryProvider.handle_tool_call).__name__ == "plugins.memory.openviking.tools"
    assert inspect.getmodule(OpenVikingMemoryProvider.initialize).__name__ == "plugins.memory.openviking.provider"


def _schema_by_name(name: str) -> dict:
    provider = OpenVikingMemoryProvider()
    schemas = {schema["name"]: schema for schema in provider.get_tool_schemas()}
    return schemas[name]


def _schema_names() -> set[str]:
    provider = OpenVikingMemoryProvider()
    return {schema["name"] for schema in provider.get_tool_schemas()}


def test_tool_schema_documents_retrieval_vs_path_matching_boundary():
    search_desc = _schema_by_name("viking_search")["description"]
    glob_desc = _schema_by_name("viking_glob")["description"]

    assert "Canonical semantic retrieval" in search_desc
    assert "find() for simple low-latency semantic recall" in search_desc
    assert "not filename/path matching" in search_desc
    assert "Canonical AGFS filename/path matching" in glob_desc


def test_viking_find_is_hidden_compat_tool_by_default(monkeypatch):
    monkeypatch.delenv("OPENVIKING_EXPOSE_COMPAT_TOOLS", raising=False)
    reload(openviking_tool_policy)

    names = _schema_names()

    assert "viking_glob" in names
    assert "viking_find" not in names


def test_viking_find_can_be_exposed_for_legacy_compat(monkeypatch):
    monkeypatch.setenv("OPENVIKING_EXPOSE_COMPAT_TOOLS", "true")
    reload(openviking_tool_policy)

    find_desc = _schema_by_name("viking_find")["description"]

    assert "prefer viking_glob" in find_desc
    assert "not semantic retrieval" in find_desc

    monkeypatch.delenv("OPENVIKING_EXPOSE_COMPAT_TOOLS", raising=False)
    reload(openviking_tool_policy)


def test_tool_schema_documents_factmemory_write_boundary():
    remember_desc = _schema_by_name("viking_remember")["description"]
    write_desc = _schema_by_name("viking_write")["description"]

    assert "not the durable write path for fact-memory" in remember_desc
    assert "not the official fact-memory entity write path" in write_desc
    assert "content_path" in remember_desc
    assert "content_path" in write_desc


def test_tool_search_sorts_by_raw_score_across_buckets():
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._client.post.return_value = {
        "result": {
            "memories": [
                {"uri": "viking://memories/1", "score": 0.9003, "abstract": "memory result"},
            ],
            "resources": [
                {"uri": "viking://resources/1", "score": 0.9004, "abstract": "resource result"},
            ],
            "skills": [
                {"uri": "viking://skills/1", "score": 0.8999, "abstract": "skill result"},
            ],
            "total": 3,
        }
    }

    result = json.loads(provider._tool_search({"query": "ranking"}))

    assert [entry["uri"] for entry in result["results"]] == [
        "viking://resources/1",
        "viking://memories/1",
        "viking://skills/1",
    ]
    assert [entry["score"] for entry in result["results"]] == [0.9, 0.9, 0.9]
    assert result["total"] == 3


def test_tool_search_sorts_missing_raw_score_after_negative_scores():
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._client.post.return_value = {
        "result": {
            "memories": [
                {"uri": "viking://memories/missing", "abstract": "missing score"},
            ],
            "resources": [
                {"uri": "viking://resources/negative", "score": -0.25, "abstract": "negative score"},
            ],
            "skills": [
                {"uri": "viking://skills/positive", "score": 0.1, "abstract": "positive score"},
            ],
            "total": 3,
        }
    }

    result = json.loads(provider._tool_search({"query": "ranking"}))

    assert [entry["uri"] for entry in result["results"]] == [
        "viking://skills/positive",
        "viking://memories/missing",
        "viking://resources/negative",
    ]
    assert [entry["score"] for entry in result["results"]] == [0.1, 0.0, -0.25]
    assert result["total"] == 3


def test_tool_search_defaults_to_find_endpoint():
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._client.post.return_value = {"result": {"resources": [], "total": 0}}

    result = json.loads(provider._tool_search({"query": "ranking"}))

    provider._client.post.assert_called_once_with(
        "/api/v1/search/find",
        {"query": "ranking"},
    )
    assert result["strategy"] == "find"
    assert result["results"] == []


def test_tool_search_strategy_search_passes_session_and_retrieval_options():
    provider = OpenVikingMemoryProvider()
    provider._session_id = "hermes-session"
    provider._client = MagicMock()
    provider._client.post.return_value = {
        "result": {
            "resources": [
                {
                    "uri": "viking://resources/docs/api.md",
                    "context_type": "resource",
                    "is_leaf": True,
                    "category": "api",
                    "score": 0.77,
                    "abstract": "API reference",
                    "match_reason": "semantic",
                    "relations": [{"uri": "viking://resources/docs/overview.md"}],
                }
            ],
            "query_plan": {"intent": "docs"},
            "query_results": [{"uri": "viking://resources/docs/api.md"}],
            "total": 1,
        }
    }

    result = json.loads(provider._tool_search({
        "query": "session retrieval",
        "strategy": "search",
        "scope": "viking://resources/docs",
        "limit": 5,
        "score_threshold": 0.4,
        "node_limit": 20,
        "since": "2026-05-01",
        "until": "2026-05-15",
        "time_field": "updated_at",
        "include_provenance": True,
        "telemetry": True,
    }))

    provider._client.post.assert_called_once_with(
        "/api/v1/search/search",
        {
            "query": "session retrieval",
            "target_uri": "viking://resources/docs",
            "limit": 5,
            "score_threshold": 0.4,
            "node_limit": 20,
            "since": "2026-05-01",
            "until": "2026-05-15",
            "time_field": "updated_at",
            "include_provenance": True,
            "telemetry": True,
            "session_id": "hermes-session",
        },
    )
    assert result["strategy"] == "search"
    assert result["query_plan"] == {"intent": "docs"}
    assert result["query_results"] == [{"uri": "viking://resources/docs/api.md"}]
    assert result["results"][0]["is_leaf"] is True
    assert result["results"][0]["match_reason"] == "semantic"
    assert result["results"][0]["relations"] == [{"uri": "viking://resources/docs/overview.md"}]


def test_queue_prefetch_uses_search_without_recording_used(monkeypatch):
    class FakeClient:
        calls = []

        def __init__(self, *args, **kwargs):
            pass

        def post(self, path, payload=None, **kwargs):
            self.calls.append(("post", path, payload or {}))
            if path == "/api/v1/search/search":
                return {
                    "result": {
                        "memories": [
                            {
                                "uri": "viking://user/memories/profile.md",
                                "score": 0.8,
                                "abstract": "User likes concise answers",
                                "is_leaf": True,
                                "level": 2,
                            }
                        ]
                    }
                }
            return {"result": {}}

        def get(self, path, params=None, **kwargs):
            self.calls.append(("get", path, params or {}))
            if path == "/api/v1/content/overview":
                return {"result": "Detailed docs overview"}
            if path == "/api/v1/fs/tree":
                return {"result": []}
            return {"result": {}}

    FakeClient.calls = []
    monkeypatch.setattr(openviking_module, "_VikingClient", FakeClient)
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._endpoint = "http://example.test"
    provider._session_id = "sid"
    provider._account = "default"
    provider._user = "default"
    provider._agent = "hermes"

    provider.queue_prefetch("how retrieval works")
    provider._prefetch_thread.join(timeout=5)
    prefetched = provider.prefetch("how retrieval works")

    assert "<openviking_context>" in prefetched
    assert "User likes concise answers" in prefetched
    assert "[memory]" in prefetched
    assert ("post", "/api/v1/search/search", {
        "query": "how retrieval works",
        "session_id": "sid",
        "limit": 24,
        "include_provenance": True,
    }) in FakeClient.calls
    assert not any(call[1].endswith("/used") for call in FakeClient.calls)
    assert not any(call[1] == "/api/v1/search/glob" for call in FakeClient.calls)


def test_queue_prefetch_does_not_recall_resources_by_default(monkeypatch):
    class FakeClient:
        calls = []

        def __init__(self, *args, **kwargs):
            pass

        def post(self, path, payload=None, **kwargs):
            self.calls.append(("post", path, payload or {}))
            return {
                "result": {
                    "resources": [
                        {
                            "uri": "viking://resources/docs/api.md",
                            "score": 0.99,
                            "abstract": "Resource hit",
                            "is_leaf": True,
                            "level": 2,
                        }
                    ]
                }
            }

        def get(self, path, params=None, **kwargs):
            self.calls.append(("get", path, params or {}))
            return {"result": {}}

    FakeClient.calls = []
    monkeypatch.setattr(openviking_module, "_VikingClient", FakeClient)
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._endpoint = "http://example.test"
    provider._session_id = "sid"
    provider._account = "default"
    provider._user = "default"
    provider._agent = "hermes"

    provider.queue_prefetch("resource only hit")
    provider._prefetch_thread.join(timeout=5)

    assert provider.prefetch("resource only hit") == ""


def test_queue_prefetch_includes_session_archive_context(monkeypatch):
    class FakeClient:
        calls = []

        def __init__(self, *args, **kwargs):
            pass

        def get(self, path, params=None, **kwargs):
            self.calls.append(("get", path, params or {}))
            if path == "/api/v1/sessions/sid/context":
                return {
                    "result": {
                        "latest_archive_overview": "Earlier archive summary",
                        "pre_archive_abstracts": [
                            {"archive_id": "old-1", "abstract": "Older abstract"},
                        ],
                        "messages": [{"role": "user", "parts": [{"type": "text", "text": "active"}]}],
                    }
                }
            return {"result": {}}

        def post(self, path, payload=None, **kwargs):
            self.calls.append(("post", path, payload or {}))
            return {"result": {"memories": []}}

    FakeClient.calls = []
    monkeypatch.setattr(openviking_module, "_VikingClient", FakeClient)
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._endpoint = "http://example.test"
    provider._session_id = "sid"

    provider.queue_prefetch("archive context")
    provider._prefetch_thread.join(timeout=5)
    prefetched = provider.prefetch("archive context")

    assert "<openviking_context>" in prefetched
    assert "Earlier archive summary" in prefetched
    assert "Older abstract" in prefetched
    assert "active" not in prefetched


def test_sync_turn_records_used_only_for_explicit_contexts(monkeypatch):
    class FakeClient:
        calls = []

        def __init__(self, *args, **kwargs):
            pass

        def post(self, path, payload=None, **kwargs):
            self.calls.append((path, payload or {}))
            return {"result": {"message_count": len(self.calls)}}

    FakeClient.calls = []
    monkeypatch.setattr(openviking_module, "_VikingClient", FakeClient)
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._endpoint = "http://example.test"
    provider._session_id = "sid"
    provider._account = "default"
    provider._user = "default"
    provider._agent = "hermes"

    provider.sync_turn(
        "question",
        "answer",
        contexts=[
            {"uri": "viking://resources/docs/api.md", "context_type": "resource", "abstract": "API"},
            {"uri": "viking://resources/docs/api.md", "context_type": "resource", "abstract": "API"},
        ],
    )
    provider._sync_thread.join(timeout=5)

    assert FakeClient.calls[-1] == (
        "/api/v1/sessions/sid/used",
        {"contexts": ["viking://resources/docs/api.md"]},
    )


def test_sync_turn_failure_leaves_persistent_queue_item(monkeypatch, tmp_path):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, path, payload=None, **kwargs):
            raise RuntimeError("server offline")

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(openviking_module, "_VikingClient", FakeClient)
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._endpoint = "http://example.test"
    provider._session_id = "sid"
    provider._account = "default"
    provider._user = "default"
    provider._agent = "hermes"

    provider.sync_turn("question", "answer")
    provider._sync_thread.join(timeout=5)

    queue_files = list((tmp_path / ".hermes" / "openviking-session-sync-queue").glob("*.json"))
    assert len(queue_files) == 1
    queued = json.loads(queue_files[0].read_text(encoding="utf-8"))
    assert queued["session_id"] == "sid"
    assert queued["attempts"] == 1
    assert "server offline" in queued["last_error"]
    assert [payload["role"] for payload in queued["payloads"]] == ["user", "assistant"]


def test_initialize_drains_persistent_queue_and_deletes_successful_item(monkeypatch, tmp_path):
    class FakeClient:
        calls = []

        def __init__(self, *args, **kwargs):
            pass

        def health(self):
            return True

        def post(self, path, payload=None, **kwargs):
            self.calls.append((path, payload or {}))
            return {"result": {}}

        def get(self, path, params=None, **kwargs):
            self.calls.append((path, params or {}))
            return {"result": {"pending_tokens": 0}}

    home = tmp_path / ".hermes"
    queue_dir = home / "openviking-session-sync-queue"
    queue_dir.mkdir(parents=True)
    (queue_dir / "001.json").write_text(json.dumps({
        "version": "openviking_session_sync_queue.v1",
        "session_id": "old-sid",
        "payloads": [{"role": "user", "parts": [{"type": "text", "text": "old"}]}],
        "attempts": 0,
        "created_at": "2026-05-20T00:00:00Z",
        "updated_at": "2026-05-20T00:00:00Z",
        "last_error": "",
    }), encoding="utf-8")
    FakeClient.calls = []
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("OPENVIKING_ENDPOINT", "http://example.test")
    monkeypatch.setattr(openviking_module, "_VikingClient", FakeClient)

    provider = OpenVikingMemoryProvider()
    provider.initialize("new-sid")

    assert not list(queue_dir.glob("*.json"))
    assert ("/api/v1/sessions/old-sid/messages", {"role": "user", "parts": [{"type": "text", "text": "old"}]}) in FakeClient.calls


def test_session_sync_queue_drain_limit(monkeypatch, tmp_path):
    class FakeClient:
        calls = []

        def post(self, path, payload=None, **kwargs):
            self.calls.append((path, payload or {}))
            return {"result": {}}

        def get(self, path, params=None, **kwargs):
            return {"result": {"pending_tokens": 0}}

    home = tmp_path / ".hermes"
    queue_dir = home / "openviking-session-sync-queue"
    queue_dir.mkdir(parents=True)
    for index in range(101):
        (queue_dir / f"{index:03d}.json").write_text(json.dumps({
            "version": "openviking_session_sync_queue.v1",
            "session_id": "sid",
            "payloads": [{"role": "user", "parts": [{"type": "text", "text": str(index)}]}],
            "attempts": 0,
            "created_at": "2026-05-20T00:00:00Z",
            "updated_at": "2026-05-20T00:00:00Z",
            "last_error": "",
        }), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    provider = OpenVikingMemoryProvider()

    posted = provider._drain_session_sync_queue(FakeClient(), limit=100)

    assert posted == 100
    assert len(list(queue_dir.glob("*.json"))) == 1
    assert (queue_dir / "100.json").exists()


def test_background_review_initialize_disables_auto_capture_but_keeps_client(monkeypatch):
    class FakeClient:
        calls = []

        def __init__(self, *args, **kwargs):
            pass

        def health(self):
            return True

        def post(self, path, payload=None, **kwargs):
            self.calls.append((path, payload or {}))
            return {"result": {}}

    FakeClient.calls = []
    monkeypatch.setenv("OPENVIKING_ENDPOINT", "http://example.test")
    monkeypatch.setattr(openviking_module, "_VikingClient", FakeClient)

    provider = OpenVikingMemoryProvider()
    provider.initialize(
        "sid",
        agent_context="background_review",
        capture_mode="artifact_only",
        parent_session_id="sid",
    )

    assert provider._client is not None
    assert provider._agent_context == "background_review"
    assert provider._capture_mode == "artifact_only"
    assert provider._parent_session_id == "sid"
    assert provider._auto_capture is False

    provider.sync_turn("review prompt", "internal review answer")
    assert not any(path.endswith("/messages") for path, _payload in FakeClient.calls)


def test_background_review_memory_write_is_marked_and_deduped(monkeypatch):
    class ImmediateThread:
        def __init__(self, *, target=None, daemon=None, name=None):
            self._target = target

        def start(self):
            if self._target:
                self._target()

    class FakeClient:
        calls = []

        def __init__(self, *args, **kwargs):
            pass

        def post(self, path, payload=None, **kwargs):
            self.calls.append((path, payload or {}))
            return {"result": {}}

    FakeClient.calls = []
    monkeypatch.setattr(openviking_module, "_VikingClient", FakeClient)
    monkeypatch.setattr(openviking_module.threading, "Thread", ImmediateThread)

    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._endpoint = "http://example.test"
    provider._session_id = "sid"
    provider._account = "default"
    provider._user = "default"
    provider._agent = "hermes"
    provider._agent_context = "background_review"
    provider._capture_mode = "artifact_only"

    metadata = {
        "write_origin": "background_review",
        "execution_context": "background_review",
        "session_id": "sid",
    }
    provider.on_memory_write("add", "memory", "User prefers concise updates", metadata=metadata)
    provider.on_memory_write("add", "memory", "User prefers concise updates", metadata=metadata)

    writes = [call for call in FakeClient.calls if call[0] == "/api/v1/sessions/sid/messages"]
    assert len(writes) == 1
    payload = writes[0][1]
    text = payload["parts"][0]["text"]
    assert payload["role"] == "user"
    assert "[Self-evolution memory - memory]" in text
    assert "Source: background_review" in text
    assert "User prefers concise updates" in text


def test_sync_turn_captures_message_parts_and_skips_system_policy(monkeypatch):
    class FakeClient:
        calls = []

        def __init__(self, *args, **kwargs):
            pass

        def post(self, path, payload=None, **kwargs):
            self.calls.append((path, payload or {}))
            return {"result": {}}

        def get(self, path, params=None, **kwargs):
            self.calls.append((path, params or {}))
            return {"result": {"pending_tokens": 0}}

    FakeClient.calls = []
    monkeypatch.setattr(openviking_module, "_VikingClient", FakeClient)
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._endpoint = "http://example.test"
    provider._session_id = "sid"
    provider._pending_context_parts = [
        {
            "type": "context",
            "uri": "viking://user/memories/profile.md",
            "context_type": "memory",
            "abstract": "profile",
        }
    ]

    messages = [
        {"role": "system", "content": "runtime policy"},
        {"role": "user", "content": "<openviking_context>ctx</openviking_context>\nQuestion"},
        {
            "role": "assistant",
            "content": "I will search",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {"name": "search", "arguments": "{\"q\":\"needle\"}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "tool output"},
        {"role": "assistant", "content": "Final answer"},
    ]

    provider.sync_turn("Question", "Final answer", messages=messages)
    provider._sync_thread.join(timeout=5)

    writes = [call for call in FakeClient.calls if call[0] == "/api/v1/sessions/sid/messages"]
    assert [payload["role"] for _, payload in writes] == [
        "user", "assistant", "assistant", "assistant",
    ]
    assert writes[0][1]["parts"] == [{"type": "text", "text": "Question"}]
    assert {"type": "context", "uri": "viking://user/memories/profile.md", "context_type": "memory", "abstract": "profile"} in writes[1][1]["parts"]
    assert writes[1][1]["parts"][1]["type"] == "tool"
    assert writes[1][1]["parts"][1]["tool_input"] == {"q": "needle"}
    assert writes[2][1]["parts"][0]["tool_output"] == "tool output"
    assert not any("runtime policy" in json.dumps(payload) for _, payload in writes)
    assert (
        "/api/v1/sessions/sid/used",
        {"contexts": ["viking://user/memories/profile.md"]},
    ) in FakeClient.calls

    FakeClient.calls = []
    provider.sync_turn("Question", "Final answer", messages=messages)
    provider._sync_thread.join(timeout=5)
    assert not any(call[0] == "/api/v1/sessions/sid/messages" for call in FakeClient.calls)

    changed = list(messages)
    changed[-1] = {"role": "assistant", "content": "Changed final answer"}
    provider.sync_turn("Question", "Changed final answer", messages=changed)
    provider._sync_thread.join(timeout=5)
    changed_writes = [call for call in FakeClient.calls if call[0] == "/api/v1/sessions/sid/messages"]
    assert changed_writes == [
        ("/api/v1/sessions/sid/messages", {"role": "assistant", "parts": [{"type": "text", "text": "Changed final answer"}]})
    ]


def test_sync_turn_strips_relevant_memories_and_commits_above_threshold(monkeypatch):
    class FakeClient:
        calls = []

        def __init__(self, *args, **kwargs):
            pass

        def post(self, path, payload=None, **kwargs):
            self.calls.append(("post", path, payload or {}))
            return {"result": {"task_id": "task-1", "archived": True}}

        def get(self, path, params=None, **kwargs):
            self.calls.append(("get", path, params or {}))
            if path == "/api/v1/sessions/sid":
                return {"result": {"pending_tokens": 25000}}
            return {"result": {}}

    FakeClient.calls = []
    monkeypatch.setattr(openviking_module, "_VikingClient", FakeClient)
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._endpoint = "http://example.test"
    provider._session_id = "sid"
    provider._account = "default"
    provider._user = "default"
    provider._agent = "hermes"

    provider.sync_turn(
        "<relevant-memories>secret</relevant-memories>\nquestion",
        "answer",
    )
    provider._sync_thread.join(timeout=5)

    assert FakeClient.calls[0] == (
        "post",
        "/api/v1/sessions/sid/messages",
        {"role": "user", "parts": [{"type": "text", "text": "question"}]},
    )
    assert (
        "post",
        "/api/v1/sessions/sid/commit",
        {"keep_recent_count": 10},
    ) in FakeClient.calls


def test_pending_token_commit_skips_missing_session_and_supports_modes():
    provider = OpenVikingMemoryProvider()
    provider._session_id = "sid"
    provider._client = MagicMock()

    client = MagicMock()
    client.get.side_effect = RuntimeError("not found")
    assert provider._maybe_commit_session(
        client,
        "sid",
        mode="pending_tokens",
        pending_token_threshold=10,
        keep_recent_count=3,
        reason="test",
    ) is None
    client.post.assert_not_called()

    client.reset_mock()
    assert provider._maybe_commit_session(
        client,
        "sid",
        mode="never",
        pending_token_threshold=10,
        keep_recent_count=3,
        reason="test",
    ) is None
    client.get.assert_not_called()
    client.post.assert_not_called()

    client.post.return_value = {"result": {"archived": True}}
    assert provider._maybe_commit_session(
        client,
        "sid",
        mode="always",
        pending_token_threshold=10,
        keep_recent_count=3,
        reason="test",
    ) == {"archived": True}
    client.post.assert_called_once_with(
        "/api/v1/sessions/sid/commit",
        {"keep_recent_count": 3},
    )


def test_on_pre_compress_commits_wait_true_keep_recent_zero():
    provider = OpenVikingMemoryProvider()
    provider._session_id = "sid"
    provider._client = MagicMock()
    provider._client.post.return_value = {
        "result": {
            "task_id": "task-1",
            "archive_uri": "viking://session/sid/history/archive-1",
            "archived": True,
        }
    }
    provider._client.get.return_value = {
        "result": {
            "status": "completed",
            "result": {"memories_extracted": {"profile": 1}},
        }
    }

    result = provider.on_pre_compress([])

    provider._client.post.assert_called_once_with("/api/v1/sessions/sid/commit", {})
    assert provider._client.get.call_args_list[0] == call("/api/v1/tasks/task-1")
    assert "archive-1" in result


def test_tool_archive_search_and_expand_call_official_endpoints():
    provider = OpenVikingMemoryProvider()
    provider._session_id = "sid"
    provider._client = MagicMock()
    provider._client.post.return_value = {"result": {"matches": [{"line": 1, "content": "hit"}]}}

    search = json.loads(provider._tool_archive({"action": "search", "query": "needle"}))

    provider._client.post.assert_called_once_with("/api/v1/search/grep", {
        "uri": "viking://session/sid/history",
        "pattern": "needle",
        "case_insensitive": True,
    })
    assert search["result"]["matches"][0]["content"] == "hit"

    provider._client.reset_mock()
    provider._client.get.return_value = {"result": {"messages": [{"role": "user"}]}}
    expand = json.loads(provider._tool_archive({"action": "expand", "archive_id": "archive-1"}))

    provider._client.get.assert_called_once_with("/api/v1/sessions/sid/archives/archive-1")
    assert expand["result"]["messages"] == [{"role": "user"}]


def test_tool_archive_search_falls_back_to_session_context():
    provider = OpenVikingMemoryProvider()
    provider._session_id = "sid"
    provider._client = MagicMock()
    provider._client.post.return_value = {"result": {"matches": [], "count": 0}}
    provider._client.get.return_value = {
        "result": {
            "latest_archive_overview": "The deploy color decision was azure.",
            "pre_archive_abstracts": [
                {"archive_id": "a1", "abstract": "Older notes about packaging."},
            ],
            "messages": [
                {"role": "assistant", "parts": [{"type": "tool", "tool_output": "azure check"}]},
            ],
        }
    }

    result = json.loads(provider._tool_archive({"action": "search", "query": "deploy azure"}))

    provider._client.post.assert_called_once()
    provider._client.get.assert_called_once_with(
        "/api/v1/sessions/sid/context",
        params={"token_budget": 64000},
    )
    assert result["result"]["fallback"] == "session_context"
    assert result["result"]["matches"][0]["section"] == "latest_archive_overview"


def test_poll_commit_task_reads_nested_official_result(caplog):
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._client.get.return_value = {
        "result": {
            "status": "completed",
            "result": {
                "memories_extracted": {"profile": 1, "preference": 2},
                "archive_uri": "",
            },
        }
    }

    with caplog.at_level(logging.INFO, logger="plugins.memory.openviking"):
        provider._poll_commit_task("task-1", max_wait=1)

    provider._client.get.assert_called_once_with("/api/v1/tasks/task-1")
    assert "total=3" in caplog.text


def test_tool_add_resource_uploads_existing_local_file(tmp_path):
    sample = tmp_path / "sample.md"
    sample.write_text("# Local resource\n", encoding="utf-8")
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._client.upload_temp_file.return_value = "upload_sample.md"
    provider._client.post.side_effect = [
        {
            "status": "ok",
            "result": {"root_uri": "viking://resources/sample"},
        },
        {
            "status": "ok",
            "result": {
                "Embedding": {"processed": 2, "error_count": 0},
                "Semantic": {"processed": 1, "error_count": 0},
            },
        },
    ]

    result = json.loads(provider._tool_add_resource({
        "url": str(sample),
        "reason": "local test",
        "wait": True,
    }))

    provider._client.upload_temp_file.assert_called_once_with(sample)
    assert provider._client.post.call_args_list == [
        call("/api/v1/resources", {
            "reason": "local test",
            "wait": True,
            "source_name": "sample.md",
            "temp_file_id": "upload_sample.md",
        }),
        call("/api/v1/system/wait", {"timeout": 60}),
    ]
    assert result["status"] == "added_and_processed"
    assert result["root_uri"] == "viking://resources/sample"
    assert result["processed"] == 3
    assert result["errors"] == 0
    assert result["source_path"] == str(sample)


def test_tool_add_resource_uploads_file_uri(tmp_path):
    sample = tmp_path / "sample.md"
    sample.write_text("# Local resource\n", encoding="utf-8")
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._client.upload_temp_file.return_value = "upload_sample.md"
    provider._client.post.return_value = {
        "status": "ok",
        "result": {"root_uri": "viking://resources/sample"},
    }

    result = json.loads(provider._tool_add_resource({
        "url": sample.as_uri(),
        "reason": "file uri test",
    }))

    provider._client.upload_temp_file.assert_called_once_with(sample)
    provider._client.post.assert_called_once_with("/api/v1/resources", {
        "reason": "file uri test",
        "source_name": "sample.md",
        "temp_file_id": "upload_sample.md",
    })
    assert result["status"] == "added"
    assert result["root_uri"] == "viking://resources/sample"


def test_tool_add_resource_uploads_existing_local_directory_and_cleans_zip(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("# Guide\n", encoding="utf-8")
    nested = docs / "nested"
    nested.mkdir()
    (nested / "api.md").write_text("# API\n", encoding="utf-8")
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    uploaded_paths = []
    provider._client.upload_temp_file.side_effect = (
        lambda path: uploaded_paths.append(path) or "upload_docs.zip"
    )
    provider._client.post.side_effect = [
        {
            "status": "ok",
            "result": {"root_uri": "viking://resources/docs"},
        },
        {
            "status": "ok",
            "result": {
                "Embedding": {"processed": 2, "error_count": 0},
                "Semantic": {"processed": 2, "error_count": 0},
            },
        },
    ]

    result = json.loads(provider._tool_add_resource({
        "url": str(docs),
        "reason": "directory test",
        "wait": True,
    }))

    assert uploaded_paths
    assert uploaded_paths[0].suffix == ".zip"
    assert not uploaded_paths[0].exists()
    assert provider._client.post.call_args_list == [
        call("/api/v1/resources", {
            "reason": "directory test",
            "wait": True,
            "source_name": "docs",
            "temp_file_id": "upload_docs.zip",
        }),
        call("/api/v1/system/wait", {"timeout": 60}),
    ]
    assert result["status"] == "added_and_processed"
    assert result["root_uri"] == "viking://resources/docs"


def test_tool_add_resource_directory_zip_skips_symlink_escape(tmp_path):
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("do not upload\n", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("# Guide\n", encoding="utf-8")
    link = docs / "leak.txt"
    try:
        link.symlink_to(secret)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in test environment: {exc}")

    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    archive_entries = {}

    def inspect_upload(path):
        with zipfile.ZipFile(path) as archive:
            archive_entries["names"] = archive.namelist()
            archive_entries["payloads"] = {
                name: archive.read(name)
                for name in archive.namelist()
            }
        return "upload_docs.zip"

    provider._client.upload_temp_file.side_effect = inspect_upload
    provider._client.post.return_value = {
        "status": "ok",
        "result": {"root_uri": "viking://resources/docs"},
    }

    json.loads(provider._tool_add_resource({"url": str(docs)}))

    assert archive_entries["names"] == ["guide.md"]
    assert b"do not upload" not in b"".join(archive_entries["payloads"].values())


def test_tool_add_resource_cleans_local_directory_zip_when_add_fails(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("# Guide\n", encoding="utf-8")
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    uploaded_paths = []
    provider._client.upload_temp_file.side_effect = (
        lambda path: uploaded_paths.append(path) or "upload_docs.zip"
    )
    provider._client.post.side_effect = RuntimeError("add failed")

    with pytest.raises(RuntimeError, match="add failed"):
        provider._tool_add_resource({"url": str(docs)})

    assert uploaded_paths
    assert not uploaded_paths[0].exists()


def test_tool_add_resource_cleans_local_directory_zip_when_upload_fails(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("# Guide\n", encoding="utf-8")
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    uploaded_paths = []

    def fail_upload(path):
        uploaded_paths.append(path)
        raise RuntimeError("upload failed")

    provider._client.upload_temp_file.side_effect = fail_upload

    with pytest.raises(RuntimeError, match="upload failed"):
        provider._tool_add_resource({"url": str(docs)})

    assert uploaded_paths
    assert not uploaded_paths[0].exists()
    provider._client.post.assert_not_called()


def test_tool_add_resource_rejects_missing_local_path(tmp_path):
    missing = tmp_path / "missing.md"
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()

    result = json.loads(provider._tool_add_resource({"url": str(missing)}))

    assert result["error"] == f"Local resource path does not exist: {missing}"
    provider._client.upload_temp_file.assert_not_called()
    provider._client.post.assert_not_called()


def test_tool_add_resource_sends_remote_url_as_path():
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._client.post.return_value = {
        "status": "ok",
        "result": {"root_uri": "viking://resources/remote"},
    }

    provider._tool_add_resource({"url": "https://example.com/doc.md"})

    provider._client.upload_temp_file.assert_not_called()
    provider._client.post.assert_called_once_with("/api/v1/resources", {
        "path": "https://example.com/doc.md",
    })


def test_tool_add_skill_posts_inline_data_unchanged():
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    skill = {"name": "calc", "description": "Calculate", "inputSchema": {"type": "object"}}
    provider._client.post.return_value = {
        "status": "ok",
        "result": {
            "name": "calc",
            "uri": "viking://agent/skills/calc/SKILL.md",
            "root_uri": "viking://agent/skills/calc",
        },
    }

    result = json.loads(provider._tool_add_skill({"data": skill}))

    provider._client.post.assert_called_once_with("/api/v1/skills", {"data": skill})
    assert result["status"] == "added"
    assert result["name"] == "calc"
    assert result["uri"] == "viking://agent/skills/calc/SKILL.md"


def test_tool_add_skill_uploads_skill_file(tmp_path):
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    skill_file = tmp_path / "my-skill" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text("---\nname: my-skill\ndescription: Test\n---\n", encoding="utf-8")
    provider._client.upload_temp_file.return_value = "temp-file-1"
    provider._client.post.return_value = {"result": {"uri": "viking://agent/skills/my-skill/SKILL.md"}}

    result = json.loads(provider._tool_add_skill({"path": str(skill_file), "wait": False}))

    provider._client.upload_temp_file.assert_called_once_with(skill_file)
    provider._client.post.assert_called_once_with(
        "/api/v1/skills",
        {"wait": False, "source_name": "my-skill", "temp_file_id": "temp-file-1"},
    )
    assert result["uri"] == "viking://agent/skills/my-skill/SKILL.md"


def test_tool_add_skill_uploads_skill_directory_and_removes_zip(tmp_path):
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    skill_dir = tmp_path / "skill-dir"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: skill-dir\ndescription: Test\n---\n", encoding="utf-8")
    (skill_dir / "helper.py").write_text("print('hi')\n", encoding="utf-8")
    seen_upload_path = {}

    def capture_upload(path):
        seen_upload_path["path"] = path
        assert path.exists()
        assert zipfile.is_zipfile(path)
        return "temp-dir-1"

    provider._client.upload_temp_file.side_effect = capture_upload
    provider._client.post.return_value = {"result": {"auxiliary_files": ["helper.py"]}}

    result = json.loads(provider._tool_add_skill({"path": str(skill_dir)}))

    provider._client.post.assert_called_once_with(
        "/api/v1/skills",
        {"source_name": "skill-dir", "temp_file_id": "temp-dir-1"},
    )
    assert result["auxiliary_files"] == ["helper.py"]
    assert not seen_upload_path["path"].exists()


def test_tool_add_skill_requires_exactly_one_source(tmp_path):
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()

    result = json.loads(provider._tool_add_skill({}))
    assert result["error"] == "Exactly one of data or path is required"

    result = json.loads(provider._tool_add_skill({"data": "x", "path": str(tmp_path)}))
    assert result["error"] == "Exactly one of data or path is required"
    provider._client.post.assert_not_called()


def test_tool_system_maps_status_wait_observer_and_metrics():
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._client.get.return_value = {"status": "ok", "result": {"initialized": True}}

    status = json.loads(provider._tool_system({"action": "status"}))
    provider._client.get.assert_called_once_with("/api/v1/system/status")
    assert status["result"] == {"initialized": True}

    provider._client.reset_mock()
    provider._client.post.return_value = {"status": "ok", "result": {"pending": 0}}
    wait = json.loads(provider._tool_system({"action": "wait", "timeout": 2}))
    provider._client.post.assert_called_once_with("/api/v1/system/wait", {"timeout": 2})
    assert wait["result"] == {"pending": 0}

    provider._client.reset_mock()
    provider._client.get.return_value = {"status": "ok", "result": {"name": "queue"}}
    observer = json.loads(provider._tool_system({"action": "observer", "component": "queue"}))
    provider._client.get.assert_called_once_with("/api/v1/observer/queue")
    assert observer["component"] == "queue"

    provider._client.reset_mock()
    provider._client.get_text.return_value = (
        "# HELP openviking_http_requests_total Total\n"
        "openviking_http_requests_total{route=\"/health\"} 1\n"
        "python_gc_objects_collected_total 3\n"
    )
    metrics = json.loads(provider._tool_system({
        "action": "metrics",
        "filter_prefix": "openviking_",
        "max_chars": 30,
    }))
    provider._client.get_text.assert_called_once_with("/metrics")
    assert "openviking_http_requests_total" in metrics["metrics"]
    assert "python_gc" not in metrics["metrics"]
    assert metrics["truncated"] is True


def test_tool_admin_read_actions_do_not_request_approval(monkeypatch):
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._client.get.return_value = {"status": "ok", "result": [{"account_id": "default"}]}
    approval = MagicMock()
    monkeypatch.setattr(openviking_module, "check_tool_action_approval", approval, raising=False)

    result = json.loads(provider._tool_admin({"action": "list_accounts"}))

    provider._client.get.assert_called_once_with("/api/v1/admin/accounts")
    approval.assert_not_called()
    assert result["result"] == [{"account_id": "default"}]


def test_tool_admin_write_denial_prevents_http(monkeypatch):
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()

    def deny(*args, **kwargs):
        return {"approved": False, "message": "denied for test"}

    import tools.approval as approval_module
    monkeypatch.setattr(approval_module, "check_tool_action_approval", deny)

    result = json.loads(provider._tool_admin({
        "action": "delete_account",
        "account_id": "acme",
    }))

    assert result["error"] == "denied for test"
    provider._client.delete.assert_not_called()


def test_tool_admin_write_approval_then_calls_official_endpoint(monkeypatch):
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._client.put.return_value = {
        "status": "ok",
        "result": {"account_id": "acme", "user_id": "bob", "role": "admin"},
    }

    import tools.approval as approval_module
    monkeypatch.setattr(
        approval_module,
        "check_tool_action_approval",
        lambda *args, **kwargs: {"approved": True, "message": None},
    )

    result = json.loads(provider._tool_admin({
        "action": "set_role",
        "account_id": "acme",
        "user_id": "bob",
        "role": "ADMIN",
    }))

    provider._client.put.assert_called_once_with(
        "/api/v1/admin/accounts/acme/users/bob/role",
        {"role": "admin"},
    )
    assert result["result"]["role"] == "admin"


def test_tool_add_resource_passes_watch_interval_and_resource_target():
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._client.post.return_value = {
        "status": "ok",
        "result": {
            "root_uri": "viking://resources/docs",
            "source_path": "https://example.com/doc.md",
            "errors": ["minor parse warning"],
        },
    }

    result = json.loads(provider._tool_add_resource({
        "url": "https://example.com/doc.md",
        "to": "viking://resources/docs",
        "watch_interval": 2.5,
    }))

    provider._client.post.assert_called_once_with("/api/v1/resources", {
        "to": "viking://resources/docs",
        "watch_interval": 2.5,
        "path": "https://example.com/doc.md",
    })
    assert result["status"] == "added"
    assert result["root_uri"] == "viking://resources/docs"
    assert result["source_path"] == "https://example.com/doc.md"
    assert result["errors"] == ["minor parse warning"]


@pytest.mark.parametrize("field", ["to", "parent"])
def test_tool_add_resource_rejects_non_resource_targets(field):
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()

    result = json.loads(provider._tool_add_resource({
        "url": "https://example.com/doc.md",
        field: "viking://user/memories/doc.md",
    }))

    assert result["error"] == f"{field} must be under viking://resources/"
    provider._client.post.assert_not_called()


@pytest.mark.parametrize("url", [
    "git@github.com:org/repo.git",
    "git@ssh.dev.azure.com:v3/org/project/repo",
    "ssh://git@github.com/org/repo.git",
    "git://github.com/org/repo.git",
])
def test_tool_add_resource_sends_git_remote_sources_as_path(url):
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._client.post.return_value = {
        "status": "ok",
        "result": {"root_uri": "viking://resources/repo"},
    }

    provider._tool_add_resource({"url": url})

    provider._client.upload_temp_file.assert_not_called()
    provider._client.post.assert_called_once_with("/api/v1/resources", {
        "path": url,
    })


def test_tool_add_resource_wait_falls_back_to_queued_response_on_wait_error():
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._client.post.side_effect = [
        {
            "status": "ok",
            "result": {"root_uri": "viking://resources/remote"},
        },
        RuntimeError("wait timed out"),
    ]

    result = json.loads(provider._tool_add_resource({
        "url": "https://example.com/doc.md",
        "wait": True,
        "timeout": 7,
    }))

    assert provider._client.post.call_args_list == [
        call("/api/v1/resources", {
            "path": "https://example.com/doc.md",
            "wait": True,
            "timeout": 7,
        }),
        call("/api/v1/system/wait", {"timeout": 7}),
    ]
    assert result["status"] == "added_wait_failed"
    assert result["root_uri"] == "viking://resources/remote"
    assert result["wait_error"] == "wait timed out"


def test_tool_fs_mkdir_calls_official_endpoint():
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._client.post.return_value = {"status": "ok", "result": {"created": True}}

    result = json.loads(provider._tool_fs({
        "action": "mkdir",
        "uri": "viking://resources/docs",
        "description": "Docs",
    }))

    provider._client.post.assert_called_once_with(
        "/api/v1/fs/mkdir",
        {"uri": "viking://resources/docs", "description": "Docs"},
    )
    assert result["status"] == "created"
    assert result["result"] == {"created": True}


def test_tool_fs_mv_calls_official_endpoint():
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._client.post.return_value = {"status": "ok", "result": {"moved": True}}

    result = json.loads(provider._tool_fs({
        "action": "mv",
        "from_uri": "viking://resources/old.md",
        "to_uri": "viking://resources/new.md",
    }))

    provider._client.post.assert_called_once_with(
        "/api/v1/fs/mv",
        {"from_uri": "viking://resources/old.md", "to_uri": "viking://resources/new.md"},
    )
    assert result["status"] == "moved"


def test_tool_fs_rm_calls_delete_endpoint():
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._client.delete.return_value = {"status": "ok", "result": {"removed": True}}

    result = json.loads(provider._tool_fs({
        "action": "rm",
        "uri": "viking://resources/docs",
        "recursive": True,
    }))

    provider._client.delete.assert_called_once_with(
        "/api/v1/fs/rm",
        {"uri": "viking://resources/docs", "recursive": True},
    )
    assert result["status"] == "removed"
    assert result["recursive"] is True


def test_tool_write_reads_large_content_from_file(tmp_path):
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._client.post.return_value = {
        "status": "ok",
        "result": {"written_bytes": 19},
    }
    content_file = tmp_path / "large-note.md"
    content_file.write_text("large content body", encoding="utf-8")

    result = json.loads(provider._tool_write({
        "uri": "viking://user/memories/notes/large.md",
        "content_path": str(content_file),
    }))

    provider._client.post.assert_called_once_with(
        "/api/v1/content/write",
        {
            "uri": "viking://user/memories/notes/large.md",
            "content": "large content body",
            "mode": "create",
        },
    )
    assert result["status"] == "written"
    assert result["uri"] == "viking://user/memories/notes/large.md"


def test_tool_write_optionally_deletes_content_path_after_success(tmp_path):
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._client.post.return_value = {"status": "ok", "result": {"written_bytes": 4}}
    content_file = tmp_path / "temp-transfer.md"
    content_file.write_text("body", encoding="utf-8")

    result = json.loads(provider._tool_write({
        "uri": "viking://user/memories/notes/temp.md",
        "content_path": str(content_file),
        "delete_content_path_after_write": True,
    }))

    assert result["status"] == "written"
    assert result["deleted_content_path"] is True
    assert not content_file.exists()


def test_tool_write_keeps_content_path_when_write_fails(tmp_path):
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._client.post.side_effect = RuntimeError("server rejected body")
    content_file = tmp_path / "temp-transfer.md"
    content_file.write_text("body", encoding="utf-8")

    result = json.loads(provider._tool_write({
        "uri": "viking://user/memories/notes/temp.md",
        "content_path": str(content_file),
        "delete_content_path_after_write": True,
    }))

    assert result["error"] == "Write failed: server rejected body"
    assert content_file.exists()


def test_tool_write_requires_exactly_one_content_source(tmp_path):
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    content_file = tmp_path / "note.md"
    content_file.write_text("body", encoding="utf-8")

    result = json.loads(provider._tool_write({
        "uri": "viking://user/memories/notes/large.md",
        "content": "inline",
        "content_path": str(content_file),
    }))
    assert result["error"] == "Provide exactly one of content or content_path"

    result = json.loads(provider._tool_write({
        "uri": "viking://user/memories/notes/large.md",
    }))
    assert result["error"] == "content or content_path is required"
    provider._client.post.assert_not_called()


def test_tool_remember_agent_categories_use_configured_agent_id():
    provider = OpenVikingMemoryProvider()
    provider._agent = "codex"
    provider._tool_write = MagicMock(return_value=json.dumps({"status": "written"}))

    provider._tool_remember({
        "content": "learned pattern",
        "category": "pattern",
        "topic": "reviews",
    })

    provider._tool_write.assert_called_once_with({
        "uri": "viking://agent/codex/memories/patterns/reviews.md",
        "content": "learned pattern",
        "append": True,
    })


def test_tool_remember_reads_larger_note_from_file(tmp_path):
    provider = OpenVikingMemoryProvider()
    provider._tool_write = MagicMock(return_value=json.dumps({"status": "written"}))
    content_file = tmp_path / "memory-note.md"
    content_file.write_text("large memory note", encoding="utf-8")

    result = json.loads(provider._tool_remember({
        "content_path": str(content_file),
        "category": "preference",
        "topic": "style",
    }))

    assert result["status"] == "written"
    provider._tool_write.assert_called_once_with({
        "uri": "viking://user/memories/preferences/style.md",
        "content": "large memory note",
        "append": True,
    })


def test_tool_remember_requires_exactly_one_content_source(tmp_path):
    provider = OpenVikingMemoryProvider()
    provider._tool_write = MagicMock(return_value=json.dumps({"status": "written"}))
    content_file = tmp_path / "memory-note.md"
    content_file.write_text("large memory note", encoding="utf-8")

    result = json.loads(provider._tool_remember({
        "content": "inline",
        "content_path": str(content_file),
    }))
    assert result["error"] == "Provide exactly one of content or content_path"

    result = json.loads(provider._tool_remember({}))
    assert result["error"] == "content or content_path is required"
    provider._tool_write.assert_not_called()


def test_queue_prefetch_uses_configured_agent_header(monkeypatch):
    class FakeClient:
        calls = []
        init_kwargs = []

        def __init__(self, *args, **kwargs):
            self.init_kwargs.append(kwargs)

        def post(self, path, payload=None, **kwargs):
            self.calls.append(("post", path, payload or {}))
            return {"result": {}}

        def get(self, path, params=None, **kwargs):
            self.calls.append(("get", path, params or {}))
            return {"result": []}

    FakeClient.calls = []
    monkeypatch.setattr(openviking_module, "_VikingClient", FakeClient)
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._endpoint = "http://example.test"
    provider._session_id = "sid"
    provider._account = "default"
    provider._user = "default"
    provider._agent = "codex"

    provider.queue_prefetch("agent context")
    provider._prefetch_thread.join(timeout=5)

    assert FakeClient.init_kwargs[-1]["agent"] == "codex"
    assert ("post", "/api/v1/search/search", {
        "query": "agent context",
        "session_id": "sid",
        "limit": 24,
        "include_provenance": True,
    }) in FakeClient.calls


def test_tool_consistency_reports_missing_records():
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._client.post.return_value = {
        "status": "ok",
        "result": {
            "ok": False,
            "expected_count": 3,
            "missing_record_count": 1,
            "missing_records_truncated": False,
            "missing_records": [
                {
                    "uri": "viking://resources/docs/README.md",
                    "path": "README.md",
                    "level": 2,
                    "key": "README.md#level=2",
                }
            ],
        },
    }

    result = json.loads(provider._tool_consistency({"scope": "viking://resources/docs"}))

    provider._client.post.assert_called_once_with(
        "/api/v1/system/consistency",
        {"uri": "viking://resources/docs"},
    )
    assert result["ok"] is False
    assert result["expected_count"] == 3
    assert result["missing_count"] == 1
    assert result["missing_records"] == [
        {"uri": "viking://resources/docs/README.md", "path": "README.md", "level": 2}
    ]


def test_tool_reindex_passes_mode_and_wait_to_content_reindex():
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._client.post.return_value = {
        "status": "ok",
        "result": {
            "status": "completed",
            "scanned_records": 4,
            "rebuilt_records": 3,
            "unsupported_records": 1,
            "failed_records": 0,
            "duration_ms": 123,
            "warnings": ["skipped unsupported record"],
        },
    }

    result = json.loads(provider._tool_reindex({
        "scope": "viking://resources/docs",
        "mode": "semantic_and_vectors",
        "wait": False,
    }))

    provider._client.post.assert_called_once_with(
        "/api/v1/content/reindex",
        {
            "uri": "viking://resources/docs",
            "mode": "semantic_and_vectors",
            "wait": False,
        },
    )
    assert result["status"] == "completed"
    assert result["scanned"] == 4
    assert result["rebuilt"] == 3
    assert result["unsupported"] == 1
    assert result["failed"] == 0
    assert result["duration_ms"] == 123
    assert result["warnings"] == ["skipped unsupported record"]


def test_viking_client_upload_temp_file_uses_multipart_identity_headers(tmp_path, monkeypatch):
    sample = tmp_path / "sample.md"
    sample.write_text("# Local resource\n", encoding="utf-8")
    client = _VikingClient(
        "https://example.com",
        api_key="test-key",
        account="test-account",
        user="test-user",
        agent="test-agent",
    )
    captured_kwargs = {}

    def capture_httpx_post(url, **kwargs):
        captured_kwargs.update(kwargs)
        return SimpleNamespace(
            status_code=200,
            text="",
            json=lambda: {"status": "ok", "result": {"temp_file_id": "upload_sample.md"}},
            raise_for_status=lambda: None,
        )

    monkeypatch.setattr(client._httpx, "post", capture_httpx_post)

    assert client.upload_temp_file(sample) == "upload_sample.md"

    assert "files" in captured_kwargs
    assert "json" not in captured_kwargs
    headers = captured_kwargs["headers"]
    assert headers["X-OpenViking-Account"] == "test-account"
    assert headers["X-OpenViking-User"] == "test-user"
    assert headers["X-OpenViking-Agent"] == "test-agent"
    assert headers["X-API-Key"] == "test-key"
    assert "Content-Type" not in headers


def test_viking_client_delete_uses_json_payload_and_headers(monkeypatch):
    client = _VikingClient(
        "https://example.com",
        api_key="test-key",
        account="test-account",
        user="test-user",
        agent="test-agent",
    )
    captured = {}

    def capture_request(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured.update(kwargs)
        return SimpleNamespace(
            status_code=200,
            text="",
            json=lambda: {"status": "ok", "result": {"removed": True}},
            raise_for_status=lambda: None,
        )

    monkeypatch.setattr(client._httpx, "request", capture_request)

    assert client.delete("/api/v1/fs/rm", {"uri": "viking://resources/docs"}) == {
        "status": "ok",
        "result": {"removed": True},
    }
    assert captured["method"] == "DELETE"
    assert captured["url"] == "https://example.com/api/v1/fs/rm"
    assert captured["json"] == {"uri": "viking://resources/docs"}
    assert captured["headers"]["X-OpenViking-Agent"] == "test-agent"


def test_viking_client_raises_structured_server_error():
    client = _VikingClient.__new__(_VikingClient)
    response = SimpleNamespace(
        status_code=403,
        text='{"status":"error"}',
        json=lambda: {
            "status": "error",
            "error": {
                "code": "PERMISSION_DENIED",
                "message": "direct host filesystem paths are not allowed",
            },
        },
        raise_for_status=lambda: None,
    )

    with pytest.raises(RuntimeError, match="PERMISSION_DENIED"):
        client._parse_response(response)


def test_viking_client_headers_include_bearer_when_api_key_set():
    client = _VikingClient(
        "https://example.com",
        api_key="test-key",
        account="acct",
        user="usr",
        agent="hermes",
    )
    headers = client._headers()
    assert headers["X-API-Key"] == "test-key"
    assert headers["Authorization"] == "Bearer test-key"


def test_viking_client_headers_send_tenant_when_default():
    # account/user set to the literal string "default". OpenViking 0.3.x
    # requires X-OpenViking-Account and X-OpenViking-User for ROOT API key
    # requests to tenant-scoped APIs — omitting them causes
    # INVALID_ARGUMENT errors even when account="default".
    client = _VikingClient(
        "https://example.com",
        api_key="test-key",
        account="default",
        user="default",
        agent="hermes",
    )
    headers = client._headers()
    assert headers["X-OpenViking-Account"] == "default"
    assert headers["X-OpenViking-User"] == "default"
    assert headers["X-OpenViking-Agent"] == "hermes"
    assert headers["Authorization"] == "Bearer test-key"


def test_viking_client_headers_send_tenant_when_empty_falls_back_to_default(monkeypatch):
    # Empty account/user strings fall back to "default" via the constructor.
    # Headers are sent even for the default value — ROOT API keys need them.
    monkeypatch.delenv("OPENVIKING_ACCOUNT", raising=False)
    monkeypatch.delenv("OPENVIKING_USER", raising=False)
    client = _VikingClient(
        "https://example.com",
        api_key="",
        account="",
        user="",
        agent="hermes",
    )
    headers = client._headers()
    assert headers["X-OpenViking-Account"] == "default"
    assert headers["X-OpenViking-User"] == "default"
    assert "Authorization" not in headers
    assert "X-API-Key" not in headers


def test_viking_client_headers_sent_with_real_tenant_values():
    client = _VikingClient(
        "https://example.com",
        api_key="test-key",
        account="real-account",
        user="real-user",
        agent="hermes",
    )
    headers = client._headers()
    assert headers["X-OpenViking-Account"] == "real-account"
    assert headers["X-OpenViking-User"] == "real-user"


def test_viking_client_health_sends_auth_headers(monkeypatch):
    client = _VikingClient(
        "https://example.com",
        api_key="test-key",
        account="",
        user="",
        agent="hermes",
    )
    captured = {}

    def capture_get(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers") or {}
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(client._httpx, "get", capture_get)
    assert client.health() is True
    assert captured["url"] == "https://example.com/health"
    assert captured["headers"]["Authorization"] == "Bearer test-key"

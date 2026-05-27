import json

from plugins.document_memory import store


class FakeVikingClient:
    instances = []

    def __init__(self, *args, **kwargs):
        self.calls = []
        self.uploads = []
        FakeVikingClient.instances.append(self)

    def upload_temp_file(self, path):
        self.uploads.append(path)
        return "temp-file-1"

    def post(self, path, payload=None, **kwargs):
        self.calls.append(("post", path, payload or {}))
        if path == "/api/v1/resources":
            return {"result": {"root_uri": "viking://resources/document-memory/study/doc.txt"}}
        if path == "/api/v1/search/find":
            return {
                "result": {
                    "total": 1,
                    "resources": [
                        {
                            "uri": "viking://resources/document-memory/study/doc.txt/chunk-1",
                            "score": 0.8,
                            "abstract": "IELTS requirement evidence",
                        }
                    ],
                }
            }
        if path == "/api/v1/system/wait":
            return {"result": {"resources": {"processed": 1, "error_count": 0}}}
        raise AssertionError(f"unexpected post: {path}")

    def get(self, path, params=None, **kwargs):
        self.calls.append(("get", path, params or {}))
        if path == "/api/v1/content/overview":
            return {"result": {"content": "overview text"}}
        if path == "/api/v1/content/read":
            return {"result": {"content": "full text"}}
        raise AssertionError(f"unexpected get: {path}")


def _use_fake_store(tmp_path, monkeypatch):
    FakeVikingClient.instances = []
    monkeypatch.setenv("DOCUMENT_MEMORY_ROOT", str(tmp_path / "documents"))
    monkeypatch.setattr(store, "_CLIENT_FACTORY", FakeVikingClient)


def test_document_import_registers_and_indexes_local_file(tmp_path, monkeypatch):
    _use_fake_store(tmp_path, monkeypatch)
    source = tmp_path / "doc.txt"
    source.write_text("hello document", encoding="utf-8")

    result = json.loads(store.handle_document_import({
        "path": str(source),
        "collection": "study",
        "tags": ["study_abroad", "IELTS"],
    }))

    assert result["status"] == "indexed"
    assert result["document"]["collection"] == "study"
    assert result["document"]["tags"] == ["study_abroad", "IELTS"]
    assert result["document"]["openviking_uri"] == "viking://resources/document-memory/study/doc.txt"
    assert (tmp_path / "documents" / "registry.jsonl").exists()
    assert (tmp_path / "documents" / "index.json").exists()
    assert FakeVikingClient.instances[0].calls[0][1] == "/api/v1/resources"


def test_document_import_duplicate_does_not_reindex(tmp_path, monkeypatch):
    _use_fake_store(tmp_path, monkeypatch)
    source = tmp_path / "doc.txt"
    source.write_text("hello document", encoding="utf-8")

    first = json.loads(store.handle_document_import({"path": str(source), "collection": "study"}))
    second = json.loads(store.handle_document_import({"path": str(source), "collection": "study"}))

    assert first["status"] == "indexed"
    assert second["status"] == "duplicate"
    assert len(FakeVikingClient.instances) == 1


def test_document_search_enriches_results_with_registry_document(tmp_path, monkeypatch):
    _use_fake_store(tmp_path, monkeypatch)
    source = tmp_path / "doc.txt"
    source.write_text("hello document", encoding="utf-8")
    imported = json.loads(store.handle_document_import({
        "path": str(source),
        "collection": "study",
        "tags": ["IELTS"],
    }))

    result = json.loads(store.handle_document_search({
        "query": "language requirement",
        "collection": "study",
        "tags": ["IELTS"],
    }))

    assert result["status"] == "ok"
    assert result["results"][0]["document"]["doc_id"] == imported["doc_id"]
    assert result["results"][0]["summary"] == "IELTS requirement evidence"


def test_document_read_uses_registered_openviking_uri(tmp_path, monkeypatch):
    _use_fake_store(tmp_path, monkeypatch)
    source = tmp_path / "doc.txt"
    source.write_text("hello document", encoding="utf-8")
    imported = json.loads(store.handle_document_import({"path": str(source), "collection": "study"}))

    result = json.loads(store.handle_document_read({"doc_id": imported["doc_id"], "level": "overview"}))

    assert result["status"] == "ok"
    assert result["uri"] == "viking://resources/document-memory/study/doc.txt"
    assert result["content"] == "overview text"


def test_document_promote_to_fact_returns_factmemory_workflow_only(tmp_path, monkeypatch):
    _use_fake_store(tmp_path, monkeypatch)
    source = tmp_path / "doc.txt"
    source.write_text("hello document", encoding="utf-8")
    imported = json.loads(store.handle_document_import({"path": str(source), "collection": "study"}))

    result = json.loads(store.handle_document_promote_to_fact({
        "doc_id": imported["doc_id"],
        "conclusion": "UCL requires IELTS 7.0",
        "evidence": "page 3",
    }))

    assert result["status"] == "needs_factmemory_confirmation"
    assert result["evidence_packet"]["doc_id"] == imported["doc_id"]
    assert result["recommended_workflow"][0].startswith("factmemory_context")

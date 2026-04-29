import json
from types import SimpleNamespace

import importlib
import os



class FakeRegistry:
    def __init__(self):
        self.should_process_called = False
        self.register_called = False
        self.mark_status_called = False
        self.last_status = None

    def should_process(self, source_path, content_hash, document_id=None):
        self.should_process_called = True
        return True, None

    def register(self, document_id, source_path, content_hash):
        self.register_called = True
        return {"document_id": document_id, "version": 1}

    def mark_status(self, document_id, source_path, status):
        self.mark_status_called = True
        self.last_status = status
        return True


class FakeIndexer:
    def __init__(self):
        self.indexed = []

    def index(self, markdown, document_id, version, source_path, *args, **kwargs):
        self.indexed.append((document_id, version))
        return [{"id": "d1"}]


class FakeSearch:
    def __init__(self):
        self.deleted = []

    async def delete_chunks(self, document_id):
        self.deleted.append(document_id)


def _make_msg(payload: dict):
    return json.dumps(payload).encode("utf-8")


def test_process_incoming_cv_flow(monkeypatch):
    # ensure DocumentRegistry.from_settings returns our fake before importing module
    fake_registry = FakeRegistry()
    # patch the classmethod on infra.document_registry
    import infra.document_registry as reg_mod
    monkeypatch.setattr(reg_mod.DocumentRegistry, "from_settings", classmethod(lambda cls: fake_registry))
    # import ingestion_triggers after patching registry
    triggers = importlib.import_module("ingestion_triggers")

    # fake processor returns predictable result
    monkeypatch.setattr(triggers, "document_processor", SimpleNamespace(process=lambda b, mime_type, filename, source_path: {
        "extracted_text": "Test text",
        "markdown": "# Test",
        "content_hash": "h123",
        "metadata": {"processed_at": "2026-01-01T00:00:00Z"},
    }))

    # fake download returns bytes
    monkeypatch.setattr(triggers, "_download_blob_sync", lambda c, b: b"PDF_BYTES")

    uploaded = {}
    def fake_upload(container, blob_name, markdown, metadata):
        uploaded["container"] = container
        uploaded["blob_name"] = blob_name
        uploaded["markdown"] = markdown
        uploaded["metadata"] = metadata

    monkeypatch.setattr(triggers, "_upload_markdown_sync", fake_upload)

    # patch indexer and search
    monkeypatch.setattr(triggers, "DocumentIndexer", lambda: FakeIndexer())
    monkeypatch.setattr(triggers, "SearchService", lambda: FakeSearch())

    payload = {
        "blob": "incoming-cv/sample.docx",
        "filename": "sample.docx",
        "source_path": "incoming/sample.docx",
    }

    msg = _make_msg(payload)
    # call function
    triggers.process_incoming_cv(msg)

    # assertions
    assert fake_registry.should_process_called
    assert fake_registry.register_called
    assert fake_registry.mark_status_called
    assert fake_registry.last_status == triggers.DocumentRegistry.STATUS_PROCESSED
    assert uploaded["container"] == triggers.settings.storage_container_normalized_markdown
    assert uploaded["blob_name"].endswith("v1.md")

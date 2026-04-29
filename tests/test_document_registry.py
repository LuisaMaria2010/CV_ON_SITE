import pytest
import types
from datetime import datetime, timezone

from azure.core.exceptions import ResourceNotFoundError

import infra.document_registry as registry_mod


class FakeTableClient:
    def __init__(self, entities=None):
        self.entities = list(entities or [])

    @classmethod
    def from_connection_string(cls, conn_str, table_name=None):
        return cls(entities=[])

    def query_entities(self, query_filter=None):
        # support simple filters: "hash eq 'x'", "source_path eq 'x'", "RowKey eq 'x'"
        if not query_filter:
            return []
        if "hash eq" in query_filter:
            val = query_filter.split("hash eq '")[1].rsplit("'", 1)[0]
            return [e for e in self.entities if e.get("hash") == val]
        if "source_path eq" in query_filter:
            val = query_filter.split("source_path eq '")[1].rsplit("'", 1)[0]
            return [e for e in self.entities if e.get("source_path") == val]
        if "RowKey eq" in query_filter or "RowKey eq '" in query_filter:
            val = query_filter.split("RowKey eq '")[1].rsplit("'", 1)[0]
            return [e for e in self.entities if e.get("RowKey") == val]
        return []

    def get_entity(self, partition_key, row_key):
        for e in self.entities:
            if e.get("PartitionKey") == partition_key and e.get("RowKey") == row_key:
                return e
        raise ResourceNotFoundError()

    def upsert_entity(self, entity=None, mode=None):
        # merge semantics
        pk = entity.get("PartitionKey")
        rk = entity.get("RowKey")
        for idx, e in enumerate(list(self.entities)):
            if e.get("PartitionKey") == pk and e.get("RowKey") == rk:
                merged = dict(e)
                merged.update(entity)
                self.entities[idx] = merged
                return
        # else insert
        self.entities.append(dict(entity))


class FakeServiceClient:
    def __init__(self, conn):
        pass

    @classmethod
    def from_connection_string(cls, conn_str):
        return cls(conn_str)

    def create_table_if_not_exists(self, table_name=None):
        return None


@pytest.fixture(autouse=True)
def patch_table_classes(monkeypatch):
    fake = FakeTableClient(entities=[])
    # Ensure DocumentRegistry.__init__ uses our fake instance
    monkeypatch.setattr(registry_mod, "TableClient", FakeTableClient)
    monkeypatch.setattr(registry_mod, "TableServiceClient", FakeServiceClient)
    # override classmethod to return the shared fake instance
    monkeypatch.setattr(FakeTableClient, "from_connection_string", classmethod(lambda cls, conn_str, table_name=None: fake))
    monkeypatch.setattr(FakeServiceClient, "from_connection_string", classmethod(lambda cls, conn_str: FakeServiceClient(conn_str)))
    yield fake


def test_should_process_returns_false_when_hash_exists(patch_table_classes):
    # prepare an existing entity with hash
    existing = {
        "PartitionKey": "incoming_file",
        "RowKey": "doc-a",
        "document_id": "doc-a",
        "source_path": "incoming/old.docx",
        "hash": "abc123",
        "version": 1,
    }
    patch_table_classes.entities.append(existing)

    reg = registry_mod.DocumentRegistry(connection_string="x", table_name="t")
    proc, ver = reg.should_process(source_path="incoming/new.docx", content_hash="abc123")
    assert proc is False
    assert ver == 1
    # alias added
    found = reg.find_by_hash("abc123")
    assert "incoming/new.docx" in (found.get("source_paths") or found.get("source_path") or "")


def test_should_process_version_when_same_source_hash_changed(patch_table_classes):
    existing = {
        "PartitionKey": "incoming_file",
        "RowKey": "doc-b",
        "document_id": "doc-b",
        "source_path": "incoming/one.docx",
        "hash": "oldhash",
        "version": 1,
    }
    patch_table_classes.entities.append(existing)

    reg = registry_mod.DocumentRegistry(connection_string="x", table_name="t")
    proc, ver = reg.should_process(source_path="incoming/one.docx", content_hash="newhash")
    assert proc is True
    assert ver == 2


def test_register_increments_version_on_hash_change(patch_table_classes):
    reg = registry_mod.DocumentRegistry(connection_string="x", table_name="t")
    # new register
    ent = reg.register(document_id="My Doc", source_path="incoming/a.docx", content_hash="h1")
    assert ent.get("version") == 1
    # register same doc id with different hash -> version 2
    ent2 = reg.register(document_id="My Doc", source_path="incoming/a.docx", content_hash="h2")
    assert ent2.get("version") == 2


def test_mark_status_updates_entity(patch_table_classes):
    reg = registry_mod.DocumentRegistry(connection_string="x", table_name="t")
    ent = reg.register(document_id="Status Doc", source_path="incoming/s.docx", content_hash="hx")
    assert ent.get("status") == registry_mod.DocumentRegistry.STATUS_PROCESSING
    ok = reg.mark_status(document_id="Status Doc", source_path="incoming/s.docx", status=registry_mod.DocumentRegistry.STATUS_PROCESSED)
    assert ok is True
    # fetch entity and check status
    found = reg.lookup("incoming/s.docx")
    assert found.get("status") == registry_mod.DocumentRegistry.STATUS_PROCESSED

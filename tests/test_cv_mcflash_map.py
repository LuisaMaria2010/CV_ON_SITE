"""Tests per la tabella CV -> MCFlash letta da Azure Table storage (infra/cv_mcflash_map)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import infra.cv_mcflash_map as cv_map_mod


class _FakeTable:
    def __init__(self, rows, fail=False):
        self.rows, self.fail, self.calls = rows, fail, 0

    def query_entities(self, query_filter, select=None):
        self.calls += 1
        if self.fail:
            raise ConnectionError("storage giu'")
        pk = query_filter.split("'")[1]
        return [r for r in self.rows if r["PartitionKey"] == pk]


@pytest.fixture
def table(monkeypatch):
    box = {"table": _FakeTable([])}
    monkeypatch.setattr(cv_map_mod, "get_table_client", lambda: box["table"])
    cv_map_mod.clear_cache()
    yield box
    cv_map_mod.clear_cache()


def _row(doc, mc_id, pk="cv"):
    return {"PartitionKey": pk, "RowKey": doc, "id_mcflash": mc_id}


def test_ids_are_normalised_to_strings(table):
    table["table"] = _FakeTable([_row("skill_tecnico_dca_-mc", 1884), _row("skill-tecnico_dca_-mc", "1853"),
                                 _row("senza_id", None), _row("altra_partizione", 1, pk="x")])
    assert cv_map_mod.get_cv_mcflash_map() == {"skill_tecnico_dca_-mc": "1884", "skill-tecnico_dca_-mc": "1853"}


def test_map_is_cached_between_calls(table):
    table["table"] = _FakeTable([_row("doc1", 1)])
    cv_map_mod.get_cv_mcflash_map()
    cv_map_mod.get_cv_mcflash_map()
    assert table["table"].calls == 1


def test_refresh_after_ttl(table, monkeypatch):
    table["table"] = _FakeTable([_row("doc1", 1)])
    monkeypatch.setenv("CV_MCFLASH_MAP_TTL_SECONDS", "0")
    cv_map_mod.get_cv_mcflash_map()
    table["table"].rows.append(_row("doc2", 2))
    assert cv_map_mod.get_cv_mcflash_map() == {"doc1": "1", "doc2": "2"}


def test_storage_failure_keeps_last_good_copy(table, monkeypatch):
    table["table"] = _FakeTable([_row("doc1", 1)])
    monkeypatch.setenv("CV_MCFLASH_MAP_TTL_SECONDS", "0")
    cv_map_mod.get_cv_mcflash_map()
    table["table"].fail = True
    assert cv_map_mod.get_cv_mcflash_map() == {"doc1": "1"}


def test_storage_failure_without_copy_is_an_error(table):
    table["table"] = _FakeTable([], fail=True)
    with pytest.raises(ConnectionError):
        cv_map_mod.get_cv_mcflash_map()


def _upload_module():
    import importlib.util
    path = Path(__file__).parent.parent / "scripts" / "upload_cv_mcflash_map.py"
    spec = importlib.util.spec_from_file_location("upload_cv_mcflash_map", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_upload_never_touches_dashboard_rows():
    upload = _upload_module()
    current = {
        "vecchio_ricostruito": {"id_mcflash": 1, "criterio": ""},
        "caricato_da_dashboard": {"id_mcflash": 2, "criterio": "dashboard", "file": "a.docx", "codice": "AAA"},
        "collegato_da_ricerca": {"id_mcflash": 3, "criterio": "dashboard-ricerca", "file": "b.pdf", "codice": "BBB"},
    }
    wanted = {
        "caricato_da_dashboard": {"id_mcflash": 9, "file": "a.docx", "codice": "AAA", "criterio": ""},  # stesso CV
        "altro_cv_di_3": {"id_mcflash": 3, "file": "c.pdf", "codice": "BBB", "criterio": ""},  # stesso candidato
        "nuovo": {"id_mcflash": 4, "file": "d.pdf", "codice": "DDD", "criterio": ""},
    }
    to_upsert, to_delete, skipped = upload.plan_map(wanted, current)
    assert to_upsert == ["nuovo"]
    assert to_delete == ["vecchio_ricostruito"]
    assert sorted(skipped) == ["altro_cv_di_3", "caricato_da_dashboard"]


def test_versioned_json_is_valid():
    """scripts/cv_mcflash_map.json (solo in locale) e' quello caricato in tabella da upload_cv_mcflash_map.py."""
    path = Path(__file__).parent.parent / "scripts" / "cv_mcflash_map.json"
    if not path.exists():
        pytest.skip("scripts/cv_mcflash_map.json e' solo in locale (contiene dati dei candidati)")
    documents = json.loads(path.read_text(encoding="utf-8"))["documents"]
    assert documents
    ids = [e["id_mcflash"] for e in documents.values()]
    assert all(isinstance(i, int) for i in ids)
    assert len(ids) == len(set(ids)), "un solo CV per record MCFlash"
    assert not any(set("/\\#?") & set(doc) for doc in documents), "document_id non validi come RowKey"

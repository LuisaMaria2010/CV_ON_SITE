"""Tests per GET /api/candidates, GET /api/candidates/{id}, GET /api/candidates/{id}/cv e POST /api/candidates/match.

MCFlash, CvMcflashMap e DocumentRegistry sono sostituiti da finti in memoria.
"""
from __future__ import annotations

import asyncio
import json

import azure.functions as func
import pytest

import functions.dashboard_api as api

RECORDS = [
    {"id": 1, "nome": "DCA", "nomi": "Davide Carli", "sede": "Torino", "ruolo": "PM", "abilitato": True, "isTalentUp": False},
    {"id": 2, "nome": "DCA", "nomi": "Daniela Caruso", "sede": "Roma", "ruolo": "Java", "abilitato": True, "isTalentUp": False},
    {"id": 3, "nome": "MRO", "nomi": "Mario Rossi", "sede": "Milano", "ruolo": "SAP", "abilitato": True, "isTalentUp": False},
    {"id": 4, "nome": "MRO", "nomi": "Mario Rossi", "sede": "Roma", "ruolo": "SAP", "abilitato": False, "isTalentUp": False},
]


class _FakeClient:
    def __init__(self, records):
        self.records, self.calls = records, 0

    async def fetch_page(self, *, limit, offset):
        self.calls += 1
        return self.records[offset:offset + limit]


class _FakeRegistry:
    @staticmethod
    def _normalize_document_id(document_id):
        return document_id.lower()

    def list_documents(self):
        return {"skill_tecnico_dca_mc": {"version": 3, "status": "processed",
                                         "source_path": "/incoming-cv/Skill_Tecnico_DCA_MC.docx"}}


@pytest.fixture
def fakes(monkeypatch):
    client = _FakeClient(RECORDS)
    monkeypatch.setattr(api, "get_mcflash_candidates_client", lambda: client)
    monkeypatch.setattr(api.cv_mcflash_map, "read_file_entries", lambda: [
        {"document_id": "Skill_Tecnico_DCA_MC", "id_mcflash": "1", "file": "Skill_Tecnico_DCA_MC.pdf"},
    ])
    monkeypatch.setattr(api.DocumentRegistry, "from_settings", classmethod(lambda cls: _FakeRegistry()))
    monkeypatch.setattr(api, "_records_cache", None)
    monkeypatch.setattr(api, "_records_loaded_at", 0.0)
    monkeypatch.setattr(api, "_records_lock", asyncio.Lock())
    monkeypatch.setattr(api, "_registry", None)
    monkeypatch.setattr(api, "get_writer", lambda: object())  # niente MCFlash finto: solo i record dell'API
    return client


def _get(url, params=None, route_params=None):
    return func.HttpRequest(method="GET", url=url, body=b"", params=params or {}, route_params=route_params or {})


def _post(url, body):
    return func.HttpRequest(method="POST", url=url, body=json.dumps(body).encode(),
                            headers={"content-type": "application/json"}, params={}, route_params={})


def _call(handler, req):
    resp = asyncio.run(handler(req))
    return resp.status_code, json.loads(resp.get_body())


def test_list_only_eligible_with_cv_info(fakes):
    status, env = _call(api.candidates, _get("/api/candidates"))
    assert status == 200
    data = env["data"]
    assert data["total"] == 3
    assert [r["id"] for r in data["items"]] == ["2", "1", "3"]  # ordine per nome
    davide = next(r for r in data["items"] if r["id"] == "1")
    assert davide["cv"] == {"document_id": "Skill_Tecnico_DCA_MC", "file": "Skill_Tecnico_DCA_MC.pdf",
                            "version": 3, "status": "processed",
                            "source_path": "/incoming-cv/Skill_Tecnico_DCA_MC.docx"}
    assert data["warnings"] == []


def test_list_filters_and_pagination(fakes):
    _, env = _call(api.candidates, _get("/api/candidates", {"has_cv": "false", "tutti": "true"}))
    assert [r["id"] for r in env["data"]["items"]] == ["2", "3", "4"]
    _, env = _call(api.candidates, _get("/api/candidates", {"q": "rossi", "sede": "roma", "tutti": "true"}))
    assert [r["id"] for r in env["data"]["items"]] == ["4"]
    _, env = _call(api.candidates, _get("/api/candidates", {"limit": "1", "offset": "1"}))
    assert env["data"]["total"] == 3 and [r["id"] for r in env["data"]["items"]] == ["1"]
    status, _ = _call(api.candidates, _get("/api/candidates", {"offset": "-1"}))
    assert status == 400


def test_records_are_cached(fakes):
    _call(api.candidates, _get("/api/candidates"))
    _call(api.candidates, _get("/api/candidates"))
    assert fakes.calls == 1
    _call(api.candidates, _get("/api/candidates", {"refresh": "true"}))
    assert fakes.calls == 2


def test_list_survives_storage_failure(fakes, monkeypatch):
    def boom():
        raise ConnectionError("storage giu'")
    monkeypatch.setattr(api.cv_mcflash_map, "read_file_entries", boom)
    status, env = _call(api.candidates, _get("/api/candidates"))
    assert status == 200
    assert env["data"]["total"] == 3
    assert all(r["cv"] is None for r in env["data"]["items"])
    assert env["data"]["warnings"]


def test_detail_found_and_not_found(fakes):
    status, env = _call(api.candidate_detail, _get("/api/candidates/4", route_params={"id": "4"}))
    assert status == 200
    assert env["data"]["nome_completo"] == "Mario Rossi" and env["data"]["idoneo"] is False
    status, _ = _call(api.candidate_detail, _get("/api/candidates/99", route_params={"id": "99"}))
    assert status == 404


@pytest.mark.parametrize("body, esito, ids", [
    ({"nome": "Davide", "cognome": "Carli"}, "aggiorna", ["1"]),
    ({"nome": "Diego", "cognome": "Casini"}, "nuovo", []),
    ({"nome": "Giulia", "cognome": "Bianchi"}, "nuovo", []),
    ({"nome": "Mario", "cognome": "Rossi"}, "scegli", ["3", "4"]),
    ({"nome_completo": "CARLI Davide"}, "aggiorna", ["1"]),
])
def test_match(fakes, body, esito, ids):
    status, env = _call(api.candidates_match, _post("/api/candidates/match", body))
    assert status == 200
    assert env["data"]["esito"] == esito
    assert [r["id"] for r in env["data"]["candidati"]] == ids


def test_match_explicit_trittico_wins(fakes):
    _, env = _call(api.candidates_match, _post("/api/candidates/match",
                                               {"trittico": "mro", "nome": "Mario", "cognome": "Rossi"}))
    assert env["data"]["trittico"] == "MRO"
    assert len(env["data"]["stesso_trittico"]) == 2


def test_match_requires_name(fakes):
    status, _ = _call(api.candidates_match, _post("/api/candidates/match", {"trittico": "DCA"}))
    assert status == 400


def test_mcflash_down_is_502(fakes, monkeypatch):
    async def down(*, limit, offset):
        raise api.MCFlashApiError("MCFlash API network error: timeout")
    monkeypatch.setattr(fakes, "fetch_page", down)
    status, env = _call(api.candidates, _get("/api/candidates"))
    assert status == 502
    assert env["error"].startswith("MCFlash non raggiungibile")


class _FakeStorage:
    def __init__(self, blobs):
        self.blobs, self.calls = blobs, []

    async def download_bytes(self, blob_name, container=None):
        self.calls.append((container, blob_name))
        if (container, blob_name) not in self.blobs:
            from azure.core.exceptions import ResourceNotFoundError
            raise ResourceNotFoundError("missing")
        return self.blobs[(container, blob_name)]


def test_candidate_cv_file(fakes, monkeypatch):
    store = _FakeStorage({("incoming-cv", "Skill_Tecnico_DCA_MC.docx"): b"DOCX"})
    monkeypatch.setattr(api, "storage", store)
    resp = asyncio.run(api.candidate_cv(_get("/api/candidates/1/cv", route_params={"id": "1"})))
    assert resp.status_code == 200 and resp.get_body() == b"DOCX"
    assert resp.mimetype.endswith("wordprocessingml.document")
    assert 'filename="Skill_Tecnico_DCA_MC.docx"' in resp.headers["Content-Disposition"]


def test_candidate_cv_not_found(fakes, monkeypatch):
    monkeypatch.setattr(api, "storage", _FakeStorage({}))
    assert _call(api.candidate_cv, _get("/api/candidates/3/cv", route_params={"id": "3"}))[0] == 404  # senza CV
    assert _call(api.candidate_cv, _get("/api/candidates/1/cv", route_params={"id": "1"}))[0] == 404  # blob assente
    assert _call(api.candidate_cv, _get("/api/candidates/99/cv", route_params={"id": "99"}))[0] == 404


def test_blob_location_falls_back_to_map_file():
    assert api._blob_location({"source_path": "/incoming-cv/a/b.pdf"}) == ("incoming-cv", "a/b.pdf")
    assert api._blob_location({"source_path": None, "file": "x.pdf"}) == (api.settings.storage_container_incoming, "x.pdf")


class _FakeWriter:
    def __init__(self, fail=False):
        self.removed, self.fail = [], fail

    def hide(self, id_mcflash, *, user=None):
        if self.fail:
            from infra.mcflash_writer import MCFlashWriteError
            raise MCFlashWriteError("HTTP 405")
        self.removed.append((id_mcflash, user))


def _hide(id_, user=None):
    return func.HttpRequest(method="POST", url=f"/api/candidates/{id_}/nascondi", body=b"",
                            params={"user": user} if user else {}, route_params={"id": str(id_)})


def test_hide_candidate_on_mcflash(fakes, monkeypatch):
    writer = _FakeWriter()
    monkeypatch.setattr(api, "get_writer", lambda: writer)
    status, env = _call(api.candidate_hide, _hide(1, user="tester"))
    assert status == 200 and env["data"] == {"id": "1", "nascosto": True, "modalita": "reale"}
    assert writer.removed == [("1", "tester")]
    assert api._records_cache is None  # la prossima lista rilegge MCFlash
    assert _call(api.candidate_hide, _hide(999))[0] == 404


def test_hide_candidate_mcflash_error(fakes, monkeypatch):
    monkeypatch.setattr(api, "get_writer", lambda: _FakeWriter(fail=True))
    status, env = _call(api.candidate_hide, _hide(1))
    assert status == 502 and "MCFlash non ha nascosto il candidato" in env["error"]

"""Tests dei candidati probabili (functions/probabili_api.py) con storage e tabella in memoria."""
from __future__ import annotations

import asyncio
import json

import azure.functions as func
import pytest
from azure.core.exceptions import ResourceNotFoundError

import functions.drafts_api as drafts
import functions.probabili_api as api
from services import probable_candidates as pc

DRAFT_ID = "0123456789abcdef0123456789abcdef"
EDITED = {"first_name": "Mario", "last_name": "Rossi", "full_name": "Mario Rossi", "trittico": "MRO",
          "email": "mario@example.com", "phone": "+39 333", "age": 30, "location": "Roma",
          "catalog_role": "Sviluppatore Java", "languages": [{"lang": "inglese", "level": "Avanzato"}],
          "mcflash": {"seniority": "Senior", "budget": 350}}


class _MemoryStorage:
    def __init__(self):
        self.blobs: dict[tuple[str, str], bytes] = {}

    async def upload_bytes(self, data, blob_name, folder=None, container=None):
        self.blobs[(container, blob_name)] = data

    async def download_bytes(self, blob_name, folder=None, container=None):
        try:
            return self.blobs[(container, blob_name)]
        except KeyError:
            raise ResourceNotFoundError("missing")

    async def delete(self, blob_name, folder=None, container=None):
        self.blobs.pop((container, blob_name), None)

    async def list_names(self, container=None, prefix=None):
        return [name for (c, name) in self.blobs if c == container]


class _MemoryTable:
    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}

    def get_entity(self, pk, rk):
        try:
            return dict(self.rows[(pk, rk)])
        except KeyError:
            raise ResourceNotFoundError("missing")

    def upsert_entity(self, entity):
        self.rows[(entity["PartitionKey"], entity["RowKey"])] = dict(entity)

    def query_entities(self, _filter):
        return [dict(e) for e in self.rows.values()]


@pytest.fixture
def world(monkeypatch):
    mem, table = _MemoryStorage(), _MemoryTable()
    monkeypatch.setattr(drafts, "storage", mem)
    monkeypatch.setattr(api, "_store", pc.ProbableStore(table))
    return mem, table


def _req(method, body=None, route=None, params=None):
    raw = json.dumps(body).encode() if body is not None else b""
    return func.HttpRequest(method=method, url="/api/candidati-probabili", body=raw,
                            headers={"content-type": "application/json"},
                            params=params or {}, route_params=route or {})


def _call(handler, req):
    resp = asyncio.run(handler(req))
    return resp.status_code, json.loads(resp.get_body())


def _draft(**extra):
    draft = {"kind": "crea", "edited": EDITED, "extracted": EDITED, "origin": "recruiter",
             "original_blob": "2026/10/20/x_cv.pdf", "original_filename": "cv.pdf", "user": "recruiter1", **extra}
    status, _ = _call(drafts.save_draft, _req("PUT", draft, {"draft_id": DRAFT_ID}))
    assert status == 200


def _propose(user="recruiter1"):
    return _call(api.propose, _req("POST", {"draft_id": DRAFT_ID, "user": user}))


def test_propose_writes_only_the_table(world):
    mem, table = world
    _draft()
    before = set(mem.blobs)
    status, env = _propose()
    assert status == 200, env
    row = env["data"]
    assert row["stato"] == "proposto" and row["recruiter"] == "recruiter1"
    assert (row["trittico"], row["nome_completo"], row["ruolo"], row["eta"], row["sede"]) == \
        ("MRO", "Mario Rossi", "Sviluppatore Java", 30, "Roma")
    assert row["lingue"] == "Inglese: Avanzato" and row["seniority"] == "Senior" and row["budget"] == 350
    assert row["email"] == "mario@example.com" and row["cv"] == {"file": "cv.pdf"}
    # niente indice: nessun blob nuovo, tantomeno in incoming-cv; cambia solo la bozza
    assert set(mem.blobs) == before
    assert not any(c == "incoming-cv" for c, _ in mem.blobs)
    assert list(table.rows) == [("prob", DRAFT_ID)]
    draft = asyncio.run(drafts.load_draft(DRAFT_ID))
    assert draft["submitted_at"] == row["proposto_il"] and draft["origin"] == "recruiter"


def test_submitted_draft_leaves_the_resume_list(world):
    _draft()
    _, env = _call(drafts.list_drafts, _req("GET", params={"origin": "recruiter"}))
    assert [d["draft_id"] for d in env["data"]] == [DRAFT_ID]
    _, env = _call(drafts.list_drafts, _req("GET", params={"origin": "admin"}))
    assert env["data"] == []
    _propose()
    _, env = _call(drafts.list_drafts, _req("GET", params={"origin": "recruiter"}))
    assert env["data"] == []


def test_propose_validation(world):
    assert _call(api.propose, _req("POST", {"draft_id": "x"}))[0] == 400
    assert _propose()[0] == 404
    _draft(edited={"first_name": "Mario"})
    status, env = _propose()
    assert status == 400 and "nome e cognome" in env["error"]


def test_propose_without_cv_or_sheet_is_rejected(world):
    _draft(original_blob=None)
    status, env = _propose()
    assert status == 400 and "CV o scheda colloquio" in env["error"]


def test_propose_from_sheet_only_keeps_interview_notes(world):
    colloquio = {"recruiter": "TST", "data_colloquio": "20/10/2026", "ral_attesa": "30/32.000 €",
                 "impressioni": "Motivato", "testo": "TARIFFA: 325\n..."}
    _draft(original_blob=None, original_filename=None, edited={**EDITED, "colloquio": colloquio})
    status, env = _propose()
    assert status == 200, env
    row = env["data"]
    assert row["cv"] is None and row["data_colloquio"] == "20/10/2026" and row["ral_attesa"] == "30/32.000 €"
    assert row["colloquio"]["impressioni"] == "Motivato" and "testo" not in row["colloquio"]


def test_list_only_one_recruiter(world):
    _draft()
    _propose(user="Giulia")
    _, env = _call(api.list_probables, _req("GET", params={"recruiter": "giulia"}))
    assert len(env["data"]) == 1
    _, env = _call(api.list_probables, _req("GET", params={"recruiter": "marco"}))
    assert env["data"] == []


def test_propose_twice_returns_same_row(world):
    _draft()
    _, first = _propose()
    _, second = _propose()
    assert first["data"]["proposto_il"] == second["data"]["proposto_il"]


def test_list_and_discard(world):
    _draft()
    _propose()
    _, env = _call(api.list_probables, _req("GET"))
    assert [r["draft_id"] for r in env["data"]] == [DRAFT_ID]
    status, env = _call(api.discard, _req("POST", {"user": "admin"}, {"draft_id": DRAFT_ID}))
    assert status == 200 and env["data"]["stato"] == "scartato" and env["data"]["revisore"] == "admin"
    _, env = _call(api.list_probables, _req("GET", params={"stato": "proposto"}))
    assert env["data"] == []
    assert _call(api.list_probables, _req("GET", params={"stato": "boh"}))[0] == 400


def test_commit_done_approves_with_admin_data(world):
    _draft()
    _propose()
    draft = asyncio.run(drafts.load_draft(DRAFT_ID))
    draft["edited"] = {**EDITED, "location": "Torino", "mcflash": {"seniority": "Medium", "site": "mcflashrisorse"}}
    op = {"id": "e" * 32, "id_mcflash": "900001", "user": "admin", "completed_at": "2026-10-20T10:00:00+00:00"}
    asyncio.run(api.on_commit_done(draft, op))
    _, env = _call(api.list_probables, _req("GET"))
    row = env["data"][0]
    assert (row["stato"], row["id_mcflash"], row["revisore"]) == ("approvato", "900001", "admin")
    assert row["sede"] == "Torino" and row["seniority"] == "Medium" and row["site"] == "mcflashrisorse"
    # approvato: non si scarta e non si ripropone
    assert _call(api.discard, _req("POST", {}, {"draft_id": DRAFT_ID}))[0] == 400


def test_commit_done_ignores_admin_drafts(world):
    _, table = world
    asyncio.run(api.on_commit_done({"draft_id": DRAFT_ID, "edited": EDITED}, {"id": "e" * 32}))
    assert table.rows == {}

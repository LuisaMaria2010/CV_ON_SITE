"""Tests per le bozze della dashboard (functions/drafts_api.py) con uno storage in memoria."""
from __future__ import annotations

import asyncio
import json

import azure.functions as func
import pytest
from azure.core.exceptions import ResourceNotFoundError

import functions.drafts_api as api

DRAFT_ID = "0123456789abcdef0123456789abcdef"
EXTRACTED = {"first_name": "Mario", "last_name": "Rossi", "role": "Dev"}


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


@pytest.fixture
def store(monkeypatch):
    mem = _MemoryStorage()
    monkeypatch.setattr(api, "storage", mem)
    return mem


def _req(method, body=None, route=None, params=None):
    raw = json.dumps(body).encode() if body is not None else b""
    return func.HttpRequest(method=method, url="/api/drafts", body=raw,
                            headers={"content-type": "application/json"},
                            params=params or {}, route_params=route or {})


def _call(handler, req):
    resp = asyncio.run(handler(req))
    body = resp.get_body()
    try:
        return resp.status_code, json.loads(body)
    except ValueError:
        return resp.status_code, body


def _save(body, draft_id=DRAFT_ID):
    return _call(api.save_draft, _req("PUT", body, {"draft_id": draft_id}))


def test_save_and_read_keeps_original_extraction(store):
    status, env = _save({"kind": "crea", "extracted": EXTRACTED, "edited": EXTRACTED,
                         "original_blob": "2026/10/05/x_cv.pdf", "original_filename": "cv.pdf"})
    assert status == 200 and env["data"]["created_at"] == env["data"]["updated_at"]
    edited = {**EXTRACTED, "role": "Senior Dev"}
    _save({"kind": "crea", "edited": edited, "extracted": {"first_name": "Altro"}})
    _, env = _call(api.get_draft, _req("GET", route={"draft_id": DRAFT_ID}))
    draft = env["data"]
    assert draft["extracted"] == EXTRACTED  # il backup non cambia
    assert draft["edited"]["role"] == "Senior Dev"
    assert draft["original_blob"] == "2026/10/05/x_cv.pdf"


def test_crea_can_become_aggiorna(store):
    _save({"kind": "crea", "edited": EXTRACTED})
    status, env = _save({"kind": "aggiorna", "id_mcflash": 1884, "edited": EXTRACTED})
    assert status == 200 and env["data"]["kind"] == "aggiorna" and env["data"]["id_mcflash"] == "1884"


def test_validation(store):
    assert _save({"kind": "crea", "edited": {}}, draft_id="../etc")[0] == 400
    assert _save({"kind": "boh", "edited": {}})[0] == 400
    status, env = _save({"kind": "aggiorna", "edited": {}})
    assert status == 400 and "id_mcflash" in env["error"]
    assert _save({"kind": "crea"})[0] == 400


def test_get_missing_is_404(store):
    assert _call(api.get_draft, _req("GET", route={"draft_id": DRAFT_ID}))[0] == 404


def test_list_filters_and_summarizes(store):
    ids = ["a" * 32, "b" * 32, "c" * 32]
    _save({"kind": "crea", "edited": EXTRACTED}, ids[0])
    _save({"kind": "aggiorna", "id_mcflash": 1884, "edited": EXTRACTED}, ids[1])
    _save({"kind": "aggiorna", "id_mcflash": "7", "edited": {}}, ids[2])
    _, env = _call(api.list_drafts, _req("GET", params={"kind": "aggiorna", "id_mcflash": "1884"}))
    rows = env["data"]
    assert [r["draft_id"] for r in rows] == [ids[1]]
    assert rows[0]["nome"] == "Mario Rossi" and rows[0]["trittico"] == "MRO" and rows[0]["id_mcflash"] == "1884"
    _, env = _call(api.list_drafts, _req("GET"))
    assert len(env["data"]) == 3


def test_delete(store):
    _save({"kind": "crea", "edited": {}})
    assert _call(api.delete_draft, _req("DELETE", route={"draft_id": DRAFT_ID}))[0] == 200
    assert _call(api.get_draft, _req("GET", route={"draft_id": DRAFT_ID}))[0] == 404


def test_original_file(store):
    store.blobs[(api.settings.storage_container_original_uploads, "2026/x_cv.pdf")] = b"%PDF-1.4"
    _save({"kind": "crea", "edited": {}, "original_blob": "2026/x_cv.pdf", "original_filename": "Mio CV.pdf"})
    resp = asyncio.run(api.draft_original(_req("GET", route={"draft_id": DRAFT_ID})))
    assert resp.status_code == 200 and resp.get_body() == b"%PDF-1.4"
    assert resp.mimetype == "application/pdf"
    assert 'filename="Mio CV.pdf"' in resp.headers["Content-Disposition"]


def test_original_missing(store):
    _save({"kind": "crea", "edited": {}})
    assert _call(api.draft_original, _req("GET", route={"draft_id": DRAFT_ID}))[0] == 404

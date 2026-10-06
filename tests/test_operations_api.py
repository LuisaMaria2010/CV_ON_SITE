"""Tests per POST /api/cv/commit e /api/operations (functions/operations_api.py)."""
from __future__ import annotations

import asyncio
import dataclasses
import json

import azure.functions as func
import pytest

import functions.drafts_api as drafts_api
import functions.operations_api as api
from services import cv_commit
from tests.test_cv_commit import EDITED, _Table, _World
from tests.test_drafts_api import _MemoryStorage

DRAFT_ID = "d" * 32


@pytest.fixture
def env(monkeypatch):
    world = _World()
    drafts_storage = _MemoryStorage()
    monkeypatch.setattr(drafts_api, "storage", drafts_storage)
    store = cv_commit.OperationStore(_Table())
    monkeypatch.setattr(api, "_store", store)
    # come default_deps: i passi completati si salvano nella tabella delle operazioni
    monkeypatch.setattr(api, "_deps", lambda: dataclasses.replace(world.deps(), save_op=store.save))
    monkeypatch.setattr(api.dashboard_api, "invalidate_candidates_cache", lambda: None)
    draft = {"draft_id": DRAFT_ID, "kind": "crea", "id_mcflash": None, "edited": EDITED,
             "filename": "Skill Tecnico_MRO_MC Engineering.pdf", "original_blob": "x/cv.docx"}
    asyncio.run(drafts_api._write(draft))
    return world, store


def _req(method, body=None, route=None, params=None):
    return func.HttpRequest(method=method, url="/api/x", body=json.dumps(body or {}).encode(),
                            headers={"content-type": "application/json"}, params=params or {},
                            route_params=route or {})


def _call(handler, req):
    resp = asyncio.run(handler(req))
    return resp.status_code, json.loads(resp.get_body())


def test_commit_then_poll_until_indexed(env):
    world, store = env
    status, envl = _call(api.cv_commit_route, _req("POST", {"draft_id": DRAFT_ID}))
    op = envl["data"]
    assert status == 200 and op["status"] == "in_corso" and op["step_mappa"] == "in_attesa"
    # la bozza e' legata all'operazione e non si modifica piu'
    draft = asyncio.run(drafts_api.load_draft(DRAFT_ID))
    assert draft["operation_id"] == op["id"]
    status, envl = _call(drafts_api.save_draft, _req("PUT", {"kind": "crea", "edited": {}}, {"draft_id": DRAFT_ID}))
    assert status == 400 and "confermata" in envl["error"]
    # una seconda conferma restituisce la stessa operazione
    _, again = _call(api.cv_commit_route, _req("POST", {"draft_id": DRAFT_ID}))
    assert again["data"]["id"] == op["id"] and len(world.writer.created) == 1
    # l'ingestione indicizza: il controllo dello stato completa la mappa
    world.index(store.get(op["id"]))
    _, polled = _call(api.get_operation, _req("GET", route={"op_id": op["id"]}))
    assert polled["data"]["status"] == "completata"
    assert asyncio.run(drafts_api.load_draft(DRAFT_ID))["committed_at"]
    _, listed = _call(api.list_operations, _req("GET", params={"stato": "completata"}))
    assert [o["id"] for o in listed["data"]] == [op["id"]]


def test_commit_validation(env):
    assert _call(api.cv_commit_route, _req("POST", {"draft_id": "e" * 32}))[0] == 404
    asyncio.run(drafts_api._write({"draft_id": "f" * 32, "kind": "crea", "edited": {"first_name": "Mario"}}))
    status, envl = _call(api.cv_commit_route, _req("POST", {"draft_id": "f" * 32}))
    assert status == 400 and "trittico" in envl["error"]


def test_retry_after_failure(env):
    world, store = env
    world.writer.fail = True
    _, envl = _call(api.cv_commit_route, _req("POST", {"draft_id": DRAFT_ID}))
    op = envl["data"]
    assert op["status"] == "fallita"
    world.writer.fail = False
    _, retried = _call(api.retry_operation, _req("POST", route={"op_id": op["id"]}))
    assert retried["data"]["step_indice"] == "ok" and retried["data"]["status"] == "in_corso"


def test_unknown_operation(env):
    assert _call(api.get_operation, _req("GET", route={"op_id": "a" * 32}))[0] == 404
    assert _call(api.get_operation, _req("GET", route={"op_id": "../x"}))[0] == 400

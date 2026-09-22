"""
Tests per la visibilita' dei candidati non MCFlash.

Regola: solo un utente `internal` vede i candidati con `id_mcflash` null.

`user_type` arriva dal frontend nel body di /api/ai-matcher-wrapper, viaggia
all'agente dentro CONTEXT_JSON e torna al server come campo di
invoke_searcher_wrapper, dove viene applicato il FILTRO. L'agente trasporta il
valore, non decide: se lo omette o lo sbaglia si filtra comunque (fail-closed),
quindi il modello non puo' causare una fuga.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import azure.functions as func

import functions.matcher_api as matcher_api
import functions.search_api as search_api
from utils.visibility import (
    USER_TYPE_EXTERNAL,
    USER_TYPE_INTERNAL,
    can_view_non_mcflash,
    resolve_user_type,
)


# =========================================================
# 1. Risoluzione di user_type
# =========================================================

@pytest.mark.parametrize("value", ["internal", "INTERNAL", "Interno", "interna"])
def test_internal_values_are_recognised(value):
    assert resolve_user_type({"user_type": value}) == USER_TYPE_INTERNAL


@pytest.mark.parametrize("payload", [
    {"user_type": "external"},
    {"user_type": "esterno"},
    {"user_type": "intrno"},      # refuso: non deve valere come internal
    {"user_type": ""},
    {"user_type": None},
    {},                            # campo assente
    None,                          # body assente
])
def test_everything_else_is_external(payload):
    assert resolve_user_type(payload) == USER_TYPE_EXTERNAL


def test_camelcase_alias():
    assert resolve_user_type({"userType": "internal"}) == USER_TYPE_INTERNAL


def test_can_view_helper():
    assert can_view_non_mcflash("internal") is True
    assert can_view_non_mcflash("external") is False
    assert can_view_non_mcflash(None) is False


# =========================================================
# 2. Filtro lato server in /api/searcher-wrapper
# =========================================================

def _hit(doc: str, *, mcflash_id: Any, full_name: str) -> dict[str, Any]:
    hit: dict[str, Any] = {
        "document_id": doc,
        "full_name": full_name,
        "name": full_name,
        "role": "java developer",
        "skills": ["java"],
        "score": 0.9,
        "semantic_evidence": f"Esperienza di {full_name}.",
    }
    if mcflash_id is not None:
        hit["mcflash_profile"] = {"id": mcflash_id, "nome": full_name, "ruolo": "java developer"}
        hit["strict_mcflash_name_match"] = True
    return hit


@pytest.fixture
def pipeline(monkeypatch):
    """1 candidato MCFlash + 1 solo-CV; intercetta cosa vede il valutatore."""
    hits = [
        _hit("d1", mcflash_id=4242, full_name="ELO"),
        _hit("d2", mcflash_id=None, full_name="Mario Rossi"),
    ]

    async def _fake(payload):
        return {
            "hits": hits,
            "meta": {"total": len(hits), "relaxed": False, "relaxed_criteria": [], "hard_select": {}},
            "suggestions": [],
        }

    seen: dict[str, Any] = {}

    def _fake_eval(original_request, candidates):
        seen["candidates"] = list(candidates)
        return {"candidates": list(candidates), "verdict": "ok", "clarifying_questions": []}

    monkeypatch.setattr(search_api, "_run_search_pipeline", _fake)
    monkeypatch.setattr(search_api, "_run_candidate_coherence_evaluator", _fake_eval)
    return seen


def _search(user_type: Any) -> dict:
    body: dict[str, Any] = {
        "search_request": {"role": "java developer", "skills": ["java"]},
        "original_request": "un java dev",
    }
    if user_type is not None:
        body["user_type"] = user_type
    req = func.HttpRequest(
        method="POST",
        url="/api/searcher-wrapper",
        body=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        params={},
        route_params={},
    )
    out = asyncio.run(search_api.searcher_wrapper(req))
    env = out if isinstance(out, dict) else json.loads(out.get_body().decode())
    return env.get("data") if isinstance(env.get("data"), dict) else env


def test_internal_sees_every_candidate(pipeline):
    data = _search("internal")
    assert [h["id_mcflash"] for h in data["search_response"]["hits"]] == [4242, None]
    assert data["search_meta"]["total_candidates"] == 2


@pytest.mark.parametrize("user_type,caso", [
    ("external", "esterno esplicito"),
    (None, "campo omesso dall'agente"),
    ("", "campo vuoto"),
    ("intrno", "refuso"),
    ("qualsiasi-altra-cosa", "valore ignoto"),
])
def test_anything_but_internal_filters(pipeline, user_type, caso):
    data = _search(user_type)
    assert [h["id_mcflash"] for h in data["search_response"]["hits"]] == [4242], caso
    assert data["search_meta"]["total_candidates"] == 1, caso


def test_filtered_candidate_never_leaves_the_function(pipeline):
    data = _search("external")
    assert "Mario Rossi" not in json.dumps(data, ensure_ascii=False)


def test_filtered_candidate_never_reaches_the_evaluator(pipeline):
    """Il filtro e' a monte del valutatore: lo scartato non entra nel suo prompt."""
    _search("external")
    assert [c["id_mcflash"] for c in pipeline["candidates"]] == [4242]


# =========================================================
# 3. Propagazione da /api/ai-matcher-wrapper
# =========================================================

class _Resp:
    output_text = "ok"
    id = "r1"


@pytest.fixture
def agent(monkeypatch):
    box: dict[str, Any] = {}

    def _fake(*, agent_name, message, model_name, previous_response_id):
        box["message"] = message
        return _Resp()

    monkeypatch.setattr(matcher_api, "_run_foundry_agent", _fake)
    monkeypatch.setattr(matcher_api, "_response_to_plain_dict", lambda r: {"id": r.id})
    monkeypatch.setattr(matcher_api, "_chat_history_load_thread", lambda u, c: None)
    monkeypatch.setattr(matcher_api, "_chat_history_append_turn", lambda **kw: {"turn_count": 1})
    return box


def _matcher(body: dict) -> dict:
    req = func.HttpRequest(
        method="POST",
        url="/api/ai-matcher-wrapper",
        body=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        params={},
        route_params={},
    )
    out = asyncio.run(matcher_api.ai_matcher_wrapper(req))
    env = out if isinstance(out, dict) else json.loads(out.get_body().decode())
    return env.get("data") if isinstance(env.get("data"), dict) else env


def _context(message: str) -> dict:
    return json.loads(message.split("CONTEXT_JSON:", 1)[1].strip())


# Il payload esatto che manda il frontend.
_BASE = {
    "user_id": "u-test-001",
    "chat_id": "chat-test-001",
    "user_request": "quali sono i migliori esperti in python?",
}


def test_user_type_reaches_the_agent(agent):
    _matcher({**_BASE, "user_type": "internal"})
    assert _context(agent["message"])["session"]["user_type"] == USER_TYPE_INTERNAL

    _matcher({**_BASE, "user_type": "external"})
    assert _context(agent["message"])["session"]["user_type"] == USER_TYPE_EXTERNAL


def test_missing_user_type_becomes_external(agent):
    _matcher(_BASE)
    assert _context(agent["message"])["session"]["user_type"] == USER_TYPE_EXTERNAL


def test_context_session_carries_only_what_the_agent_needs(agent):
    _matcher({**_BASE, "user_type": "external"})
    assert set(_context(agent["message"])["session"]) == {"user_id", "chat_id", "user_type"}


def test_caller_cannot_raise_user_type_via_context(agent):
    """Un `context.session` iniettato non deve sovrascrivere il valore risolto."""
    _matcher({**_BASE, "user_type": "external",
              "context": {"session": {"user_type": "internal"}}})
    assert _context(agent["message"])["session"]["user_type"] == USER_TYPE_EXTERNAL


def test_http_response_reports_the_user_type(agent):
    data = _matcher({**_BASE, "user_type": "external"})
    assert data["session"]["user_type"] == USER_TYPE_EXTERNAL
    assert data["session"]["can_view_non_mcflash"] is False

"""`candidates` di ai-matcher-wrapper = gli hit dell'ultima ricerca, mai il trailer dell'agente."""
from __future__ import annotations

import asyncio
import json

import azure.functions as func
import pytest

import functions.matcher_api as matcher_api
import functions.search_api as search_api
from infra.foundry_agent import candidates_from_search, search_hits_from_response
from utils.roles import drop_generic_role


def _hit(mc_id, code):
    return {"id_mcflash": mc_id, "nome": code, "ruolo": "x"}


HITS = [_hit(1719, "LBO"), _hit(1737, "DVA"), _hit(1757, "SDO"), _hit(1791, "ARU"), _hit(1683, "MLU"), _hit(1913, "SDG")]


def _raw(*calls):
    """Risposta Foundry come arriva: output del tool = JSON dentro JSON."""
    items = []
    for hits in calls:
        inner = {"success": True, "data": {"search_response": {"hits": hits}, "verdict": "weak"}}
        items += [{"type": "openapi_call"},
                  {"type": "openapi_call_output", "output": json.dumps({"response": json.dumps(inner)})}]
    return {"id": "r1", "output": items + [{"type": "message"}]}


def _ids(cands):
    return [c["id_mcflash"] for c in cands]


def test_hits_are_read_from_the_tool_output():
    assert _ids(search_hits_from_response(_raw(HITS))[0]) == _ids(HITS)


def test_candidates_are_all_hits_in_order():
    assert _ids(candidates_from_search(search_hits_from_response(_raw(HITS)))) == _ids(HITS)


def test_homonyms_keep_their_own_id():
    hits = [_hit(1857, "MMA"), _hit(148, "MMA")]
    assert candidates_from_search([hits]) == [{"id_mcflash": 1857, "trigramma": "MMA"},
                                              {"id_mcflash": 148, "trigramma": "MMA"}]


def test_retry_uses_last_search():
    assert _ids(candidates_from_search([[], [_hit(2, "BBB")]])) == [2]


def test_trigram_is_trimmed():
    assert candidates_from_search([[_hit(1881, "AMI  ")]]) == [{"id_mcflash": 1881, "trigramma": "AMI"}]


def test_empty_search_is_an_empty_list_not_none():
    assert candidates_from_search([[]]) == []


def test_no_search_this_turn_is_none():
    assert candidates_from_search([]) is None


# ---------------------------------------------------------------- endpoint

def _call_endpoint(monkeypatch, output_text, raw, thread=None):
    saved = {}

    class _Resp:
        id = "r1"

    _Resp.output_text = output_text
    monkeypatch.setattr(matcher_api, "_run_foundry_agent", lambda **kw: _Resp())
    monkeypatch.setattr(matcher_api, "_response_to_plain_dict", lambda r: raw)
    monkeypatch.setattr(matcher_api, "_chat_history_load_thread", lambda u, c: thread)
    monkeypatch.setattr(matcher_api, "_chat_history_append_turn", lambda **kw: saved.update(kw) or {"turn_count": 1})
    body = {"user_id": "u", "chat_id": "c", "user_request": "SAP Basis", "site": "mcflashcandidati"}
    req = func.HttpRequest(method="POST", url="/api/ai-matcher-wrapper", body=json.dumps(body).encode(),
                           headers={"content-type": "application/json"}, params={}, route_params={})
    out = asyncio.run(matcher_api.ai_matcher_wrapper(req))
    return (out if isinstance(out, dict) else json.loads(out.get_body().decode()))["data"], saved


def test_endpoint_ignores_partial_trailer(monkeypatch):
    """Caso reale 'SAP Basis': il trailer ne aveva 3, la ricerca 6."""
    text = "LBO, DVA, SDO...\n\n<<<CANDIDATES_JSON>>>\n" + json.dumps(
        {"candidates": [{"id_mcflash": 1719, "trigramma": "LBO"}]})
    data, saved = _call_endpoint(monkeypatch, text, _raw(HITS))
    assert _ids(data["candidates"]) == _ids(HITS)
    assert "CANDIDATES_JSON" not in data["answer"]
    assert _ids(saved["last_candidates"]) == _ids(HITS)


def test_endpoint_without_trailer(monkeypatch):
    """Caso reale 'Vue.js': nessun trailer."""
    data, _ = _call_endpoint(monkeypatch, "Ecco i profili ...", _raw(HITS))
    assert _ids(data["candidates"]) == _ids(HITS)


def test_follow_up_without_search_uses_stored_candidates(monkeypatch):
    stored = [{"id_mcflash": 1719, "trigramma": "LBO"}, {"id_mcflash": 1737, "trigramma": "DVA"}]
    data, saved = _call_endpoint(monkeypatch, "LBO ha lavorato su ...", {"id": "r2", "output": [{"type": "message"}]},
                                 thread={"last_candidates": json.dumps(stored)})
    assert data["candidates"] == stored
    assert saved["last_candidates"] is None  # nessuna nuova ricerca: resta l'elenco salvato


def test_first_turn_without_search_has_no_candidates(monkeypatch):
    data, _ = _call_endpoint(monkeypatch, "Mi serve qualche dettaglio in piu'", {"id": "r3", "output": []})
    assert data["candidates"] == []


# ---------------------------------------------------------------- ruoli generici

@pytest.mark.parametrize("role", ["Figura", "figura senior", "Un profilo", "Risorsa esperta", "consulente", "Qualcuno"])
def test_generic_roles_are_dropped(role):
    assert drop_generic_role(role) is None


@pytest.mark.parametrize("role", ["Consulente SAP", "Java Developer", "Project Manager", "Data Scientist", "Tecnico di rete"])
def test_real_roles_are_kept(role):
    assert drop_generic_role(role) == role


def test_searcher_wrapper_drops_generic_role(monkeypatch):
    seen = {}

    async def _fake(payload):
        seen["payload"] = payload
        return {"hits": [], "meta": {}, "suggestions": []}

    monkeypatch.setattr(search_api, "_run_search_pipeline", _fake)
    body = {"site": "mcflashcandidati",
            "search_request": {"role": "Figura", "query": "esperienza recommendation engine"}}
    req = func.HttpRequest(method="POST", url="/api/searcher-wrapper", body=json.dumps(body).encode(),
                           headers={"content-type": "application/json"}, params={}, route_params={})
    asyncio.run(search_api.searcher_wrapper(req))
    assert "role" not in seen["payload"]
    assert seen["payload"]["query"] == "esperienza recommendation engine"


def _call_searcher_wrapper(search_request):
    body = {"site": "mcflashcandidati", "search_request": search_request}
    req = func.HttpRequest(method="POST", url="/api/searcher-wrapper", body=json.dumps(body).encode(),
                           headers={"content-type": "application/json"}, params={}, route_params={})
    resp = asyncio.run(search_api.searcher_wrapper(req))
    return json.loads(resp.get_body())["data"]["search_meta"]


def test_searcher_wrapper_filters_only_is_not_executed(monkeypatch):
    async def _fail(payload):
        raise AssertionError("la ricerca non deve partire")

    monkeypatch.setattr(search_api, "_run_search_pipeline", _fail)
    meta = _call_searcher_wrapper({"language": "Inglese: Avanzato"})
    assert meta["search_executed"] is False
    assert meta["not_executed_reason"] == "missing_query_skills_role"


def test_searcher_wrapper_timeout_is_not_executed(monkeypatch):
    async def _slow(payload):
        await asyncio.sleep(5)

    monkeypatch.setattr(search_api, "_run_search_pipeline", _slow)
    monkeypatch.setattr(search_api, "_searcher_timeout_seconds", lambda: 0.05)
    meta = _call_searcher_wrapper({"query": "assessment infrastrutturale"})
    assert meta["search_executed"] is False
    assert meta["not_executed_reason"] == "search_timeout"


def test_searcher_wrapper_executed_search(monkeypatch):
    async def _fake(payload):
        return {"hits": [], "meta": {}, "suggestions": []}

    monkeypatch.setattr(search_api, "_run_search_pipeline", _fake)
    meta = _call_searcher_wrapper({"query": "assessment infrastrutturale"})
    assert meta["search_executed"] is True
    assert meta["not_executed_reason"] is None


def _wrapper_data(monkeypatch, meta, search_request):
    hits = [{"full_name": "AAA", "skills": ["java"]},
            {"full_name": "BBB", "skills": ["java"], "second_pass_extension": True}]

    async def _fake(payload):
        return {"hits": hits, "meta": meta, "suggestions": []}

    monkeypatch.setattr(search_api, "_run_search_pipeline", _fake)
    monkeypatch.setattr(search_api, "_run_candidate_coherence_evaluator",
                        lambda req, h: {"candidates": h, "verdict": "unknown", "clarifying_questions": []})
    body = {"site": "mcflashcandidati", "search_request": search_request}
    req = func.HttpRequest(method="POST", url="/api/searcher-wrapper", body=json.dumps(body).encode(),
                           headers={"content-type": "application/json"}, params={}, route_params={})
    return json.loads(asyncio.run(search_api.searcher_wrapper(req)).get_body())["data"]


def test_widened_search_has_no_strict_hits(monkeypatch):
    data = _wrapper_data(monkeypatch, {"hard_select": {"widened": True}},
                         {"skills": ["java"], "seniority": "senior", "location": "Roma"})
    assert [h["match_type"] for h in data["search_response"]["hits"]] == ["widened", "hybrid_extension"]
    assert data["search_meta"]["strict_candidates"] == 0


def test_not_widened_search_keeps_strict_hits(monkeypatch):
    data = _wrapper_data(monkeypatch, {}, {"skills": ["java"]})
    assert [h["match_type"] for h in data["search_response"]["hits"]] == ["strict", "hybrid_extension"]
    assert data["search_meta"]["strict_candidates"] == 1


def test_past_availability_date_rolls_to_next_year():
    from datetime import date
    today = date(2026, 9, 30)
    assert search_api._roll_past_date_forward("2026-08-01", today) == "2027-08-01"
    assert search_api._roll_past_date_forward("2026-10-15", today) is None
    assert search_api._roll_past_date_forward("2026-09-30", today) is None
    assert search_api._roll_past_date_forward("2024-02-29", today) == "2027-02-28"
    assert search_api._roll_past_date_forward("2024-11-10", today) == "2026-11-10"
    assert search_api._roll_past_date_forward("immediata", today) is None

"""
Tests per il nuovo candidate coherence evaluator (sostituisce il match evaluator
deterministico e /api/response-judger):
- _run_candidate_coherence_evaluator: riordino LLM con guardrail sugli indici
- /api/match-evaluator-wrapper: route standalone
- /api/searcher-wrapper: chiamata automatica dell'evaluator dopo la ricerca
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import azure.functions as func
import pytest

import function_app as fa


# =========================================================
# Helpers
# =========================================================

def _make_request(body: dict, url: str = "/api/match-evaluator-wrapper") -> func.HttpRequest:
    return func.HttpRequest(
        method="POST",
        url=url,
        body=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        params={},
        route_params={},
    )


def _parse_response(response) -> dict:
    envelope = response if isinstance(response, dict) else json.loads(response.get_body().decode())
    return envelope["data"] if isinstance(envelope.get("data"), dict) else envelope


def _hit(id_mcflash: Any, nome: str) -> dict[str, Any]:
    return {
        "id_mcflash": id_mcflash,
        "nome": nome,
        "ruolo": "Backend Developer",
        "eta": 30,
        "seniority": "Senior",
        "location": "Milano",
        "workmode": None,
        "lingue": "Italian",
        "disponibilita": "Immediata",
        "budget": 300,
        "skills": ["python"],
        "semantic_snippet": None,
    }


def _unexpected_call(*_args, **_kwargs):
    raise AssertionError("must not be called")


class _FakeJudgeClient:
    def __init__(self, content: str | None = None, exc: Exception | None = None):
        self.content = content
        self.exc = exc
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))])


# =========================================================
# _run_candidate_coherence_evaluator — short-circuits
# =========================================================

class TestShortCircuits:

    def test_single_hit_skips_llm(self, monkeypatch):
        monkeypatch.setattr(fa, "_get_judge_client", _unexpected_call)

        hits = [_hit(1, "A")]
        result = fa._run_candidate_coherence_evaluator("cerco python dev", hits)

        assert result == {"candidates": hits, "verdict": "unknown", "clarifying_questions": []}

    def test_empty_hits_skips_llm(self, monkeypatch):
        monkeypatch.setattr(fa, "_get_judge_client", _unexpected_call)

        result = fa._run_candidate_coherence_evaluator("cerco python dev", [])

        assert result == {"candidates": [], "verdict": "unknown", "clarifying_questions": []}

    def test_empty_original_request_skips_llm(self, monkeypatch):
        monkeypatch.setattr(fa, "_get_judge_client", _unexpected_call)

        hits = [_hit(1, "A"), _hit(2, "B")]
        result = fa._run_candidate_coherence_evaluator("", hits)

        assert result["candidates"] == hits
        assert result["verdict"] == "unknown"

    def test_no_client_configured_falls_back(self, monkeypatch):
        monkeypatch.setattr(fa, "_get_judge_client", lambda: None)

        hits = [_hit(1, "A"), _hit(2, "B")]
        result = fa._run_candidate_coherence_evaluator("cerco python dev", hits)

        assert result["candidates"] == hits
        assert result["verdict"] == "unknown"


# =========================================================
# _run_candidate_coherence_evaluator — LLM path
# =========================================================

class TestLLMPath:

    def test_valid_reorder_and_questions(self, monkeypatch):
        hits = [_hit(10, "A"), _hit(20, "B"), _hit(30, "C")]
        fake_client = _FakeJudgeClient(content=json.dumps({
            "verdict": "partial",
            "ordered_idx": [2, 0, 1],
            "clarifying_questions": ["Serve full remote o ibrido?"],
        }))
        monkeypatch.setattr(fa, "_get_judge_client", lambda: fake_client)

        result = fa._run_candidate_coherence_evaluator("cerco python dev", hits)

        assert [c["id_mcflash"] for c in result["candidates"]] == [30, 10, 20]
        assert result["verdict"] == "partial"
        assert result["clarifying_questions"] == ["Serve full remote o ibrido?"]
        assert len(fake_client.calls) == 1

    def test_field_values_never_altered_by_llm(self, monkeypatch):
        # Only ordered_idx is ever consumed from the LLM response — candidate
        # dicts returned are the exact original objects, never re-serialized
        # from LLM-provided field values.
        hits = [_hit(10, "A"), _hit(20, "B")]
        fake_client = _FakeJudgeClient(content=json.dumps({
            "verdict": "strong",
            "ordered_idx": [1, 0],
            "nome": "HACKED",
            "clarifying_questions": [],
        }))
        monkeypatch.setattr(fa, "_get_judge_client", lambda: fake_client)

        result = fa._run_candidate_coherence_evaluator("cerco python dev", hits)

        assert result["candidates"][0] is hits[1]
        assert result["candidates"][1] is hits[0]
        assert result["candidates"][0]["nome"] == "B"

    def test_invalid_ordering_keeps_original_order(self, monkeypatch):
        hits = [_hit(10, "A"), _hit(20, "B"), _hit(30, "C")]
        # Missing index 2 -> invalid permutation, must fall back to original order.
        fake_client = _FakeJudgeClient(content=json.dumps({
            "verdict": "weak",
            "ordered_idx": [0, 1],
            "clarifying_questions": ["Puoi specificare il livello di seniority?"],
        }))
        monkeypatch.setattr(fa, "_get_judge_client", lambda: fake_client)

        result = fa._run_candidate_coherence_evaluator("cerco python dev", hits)

        assert result["candidates"] == hits
        assert result["verdict"] == "weak"
        assert result["clarifying_questions"] == ["Puoi specificare il livello di seniority?"]

    def test_duplicate_index_keeps_original_order(self, monkeypatch):
        hits = [_hit(10, "A"), _hit(20, "B"), _hit(30, "C")]
        fake_client = _FakeJudgeClient(content=json.dumps({
            "verdict": "strong",
            "ordered_idx": [0, 0, 2],
            "clarifying_questions": [],
        }))
        monkeypatch.setattr(fa, "_get_judge_client", lambda: fake_client)

        result = fa._run_candidate_coherence_evaluator("cerco python dev", hits)

        assert result["candidates"] == hits

    def test_llm_exception_falls_back(self, monkeypatch):
        hits = [_hit(10, "A"), _hit(20, "B")]
        fake_client = _FakeJudgeClient(exc=RuntimeError("boom"))
        monkeypatch.setattr(fa, "_get_judge_client", lambda: fake_client)

        result = fa._run_candidate_coherence_evaluator("cerco python dev", hits)

        assert result["candidates"] == hits
        assert result["verdict"] == "unknown"

    def test_malformed_json_falls_back(self, monkeypatch):
        hits = [_hit(10, "A"), _hit(20, "B")]
        fake_client = _FakeJudgeClient(content="not json at all")
        monkeypatch.setattr(fa, "_get_judge_client", lambda: fake_client)

        result = fa._run_candidate_coherence_evaluator("cerco python dev", hits)

        assert result["candidates"] == hits
        assert result["verdict"] == "unknown"

    def test_unrecognised_verdict_normalized_to_unknown(self, monkeypatch):
        hits = [_hit(10, "A"), _hit(20, "B")]
        fake_client = _FakeJudgeClient(content=json.dumps({
            "verdict": "totally_made_up",
            "ordered_idx": [0, 1],
            "clarifying_questions": [],
        }))
        monkeypatch.setattr(fa, "_get_judge_client", lambda: fake_client)

        result = fa._run_candidate_coherence_evaluator("cerco python dev", hits)

        assert result["verdict"] == "unknown"

    def test_clarifying_questions_capped_at_three(self, monkeypatch):
        hits = [_hit(10, "A"), _hit(20, "B")]
        fake_client = _FakeJudgeClient(content=json.dumps({
            "verdict": "weak",
            "ordered_idx": [0, 1],
            "clarifying_questions": ["q1", "q2", "q3", "q4"],
        }))
        monkeypatch.setattr(fa, "_get_judge_client", lambda: fake_client)

        result = fa._run_candidate_coherence_evaluator("cerco python dev", hits)

        assert result["clarifying_questions"] == ["q1", "q2", "q3"]


# =========================================================
# /api/match-evaluator-wrapper — standalone route
# =========================================================

class TestMatchEvaluatorWrapperRoute:

    @pytest.mark.asyncio
    async def test_reorders_candidates_from_search_response(self, monkeypatch):
        hits = [_hit(10, "A"), _hit(20, "B")]
        fake_client = _FakeJudgeClient(content=json.dumps({
            "verdict": "strong",
            "ordered_idx": [1, 0],
            "clarifying_questions": [],
        }))
        monkeypatch.setattr(fa, "_get_judge_client", lambda: fake_client)

        req = _make_request({
            "original_request": "cerco python dev",
            "search_response": {"hits": hits},
        })
        data = _parse_response(await fa.match_evaluator_wrapper(req))

        assert [c["id_mcflash"] for c in data["candidates"]] == [20, 10]
        assert data["verdict"] == "strong"

    @pytest.mark.asyncio
    async def test_accepts_candidates_key_directly(self, monkeypatch):
        hits = [_hit(10, "A")]
        monkeypatch.setattr(fa, "_get_judge_client", _unexpected_call)

        req = _make_request({
            "original_request": "cerco python dev",
            "candidates": hits,
        })
        data = _parse_response(await fa.match_evaluator_wrapper(req))

        # Single candidate -> short-circuit, no LLM call, unchanged.
        assert data["candidates"] == hits
        assert data["verdict"] == "unknown"

    @pytest.mark.asyncio
    async def test_missing_body_is_rejected(self, monkeypatch):
        req = func.HttpRequest(
            method="POST",
            url="/api/match-evaluator-wrapper",
            body=b"",
            headers={"content-type": "application/json"},
            params={},
            route_params={},
        )
        envelope = await fa.match_evaluator_wrapper(req)
        envelope = envelope if isinstance(envelope, dict) else json.loads(envelope.get_body().decode())
        assert envelope["success"] is False
        assert envelope["error"] is not None


# =========================================================
# /api/searcher-wrapper — automatic chaining
# =========================================================

class TestSearcherWrapperAutoEvaluates:

    @pytest.mark.asyncio
    async def test_verdict_and_questions_surface_in_response(self, monkeypatch):
        raw_hits = [_hit(10, "A"), _hit(20, "B"), _hit(30, "C")]

        async def fake_run_search_pipeline(_payload):
            return {"hits": raw_hits, "meta": {}, "suggestions": []}

        monkeypatch.setattr(fa, "_run_search_pipeline", fake_run_search_pipeline)
        monkeypatch.setattr(
            fa,
            "_build_minimal_search_hit",
            lambda hit, requested_skills: hit,
        )

        fake_client = _FakeJudgeClient(content=json.dumps({
            "verdict": "partial",
            "ordered_idx": [2, 1, 0],
            "clarifying_questions": ["Serve disponibilita' immediata?"],
        }))
        monkeypatch.setattr(fa, "_get_judge_client", lambda: fake_client)

        req = _make_request(
            {"search_request": {"query": "cerco python dev", "top": 10}},
            url="/api/searcher-wrapper",
        )
        data = _parse_response(await fa.searcher_wrapper(req))

        assert [c["id_mcflash"] for c in data["search_response"]["hits"]] == [30, 20, 10]
        assert data["verdict"] == "partial"
        assert data["clarifying_questions"] == ["Serve disponibilita' immediata?"]

    @pytest.mark.asyncio
    async def test_single_hit_does_not_call_llm(self, monkeypatch):
        raw_hits = [_hit(10, "A")]

        async def fake_run_search_pipeline(_payload):
            return {"hits": raw_hits, "meta": {}, "suggestions": []}

        monkeypatch.setattr(fa, "_run_search_pipeline", fake_run_search_pipeline)
        monkeypatch.setattr(fa, "_build_minimal_search_hit", lambda hit, requested_skills: hit)
        monkeypatch.setattr(fa, "_get_judge_client", _unexpected_call)

        req = _make_request(
            {"search_request": {"query": "cerco python dev", "top": 10}},
            url="/api/searcher-wrapper",
        )
        data = _parse_response(await fa.searcher_wrapper(req))

        assert data["search_response"]["hits"] == raw_hits
        assert data["verdict"] == "unknown"
        assert data["clarifying_questions"] == []

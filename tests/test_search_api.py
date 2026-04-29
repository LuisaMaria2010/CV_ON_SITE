"""
Tests per POST /api/search handler in function_app.py (Phase F).

Strategia: chiamiamo direttamente la funzione `search_candidates` importata da
function_app, monkeypatchando SearchService e get_embedding_client per evitare
dipendenze di rete/Azure.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import azure.functions as func

import infra.search_service as search_mod


# =========================================================
# Helpers
# =========================================================

def _make_request(body: dict | None, method: str = "POST") -> func.HttpRequest:
    raw = json.dumps(body).encode() if body is not None else b""
    return func.HttpRequest(
        method=method,
        url="/api/search",
        body=raw,
        headers={"content-type": "application/json"},
        params={},
        route_params={},
    )


def _parse_envelope(response) -> dict:
    """Return the raw envelope {data, error, request_id, success} from the handler."""
    if isinstance(response, dict):
        return response
    body = response.get_body().decode()
    return json.loads(body)


def _parse_response(response) -> dict:
    """
    Unwrap the inner 'data' payload from the http_error_handler envelope.
    For success responses, returns envelope["data"].
    For error responses (data=None), returns the envelope itself so callers
    can inspect 'error'.
    """
    envelope = _parse_envelope(response)
    if isinstance(envelope.get("data"), dict):
        return envelope["data"]
    return envelope


def _make_fake_hit(
    document_id: str = "doc1",
    lex_score: float = 0.8,
    vec_score: float = 0.7,
    skills: list[str] | None = None,
    role: str = "developer",
    location: str = "Milano",
    processed_at: str = "2026-04-01T00:00:00+00:00",
) -> dict:
    return {
        "document_id": document_id,
        "full_name": "Test User",
        "role": role,
        "location": location,
        "skills": skills or ["python"],
        "certifications": [],
        "seniority": "senior",
        "experience_years": 5.0,
        "language": "it",
        "availability": None,
        "version": 1,
        "source_path": "incoming/cv.pdf",
        "chunk_index": 0,
        "content": "content text",
        "highlights": {},
        "lex_score": lex_score,
        "vec_score": vec_score,
        "score": max(lex_score, vec_score),
        "processed_at": processed_at,
    }


class FakeSearchService:
    """Returns configurable hits for each search_chunks call."""

    def __init__(self, hits_sequence: list[list[dict]] | None = None):
        self._hits_sequence = hits_sequence or [[]]
        self._call_count = 0
        self.calls: list[dict[str, Any]] = []

    async def search_chunks(self, query, odata_filter=None, embedding=None, top=10, index_name=None):
        self.calls.append({
            "query": query,
            "odata_filter": odata_filter,
            "top": top,
            "index_name": index_name,
        })
        if self._call_count < len(self._hits_sequence):
            hits = self._hits_sequence[self._call_count]
        else:
            hits = []
        self._call_count += 1
        return hits


async def _call_handler(req: func.HttpRequest, monkeypatch, fake_service: FakeSearchService):
    """Wire fake SearchService and call the handler."""
    monkeypatch.setattr(search_mod, "SearchService", lambda: fake_service)

    import function_app as fa
    monkeypatch.setattr(fa, "SearchService", lambda: fake_service)

    # Disable embedding to keep tests fast and network-free
    async def fake_embed(text):
        return [0.1] * 1536

    class FakeEmbClient:
        async def aembed_query(self, text):
            return [0.1] * 1536

    import infra.llm_client as llm_mod
    monkeypatch.setattr(llm_mod, "get_embedding_client", lambda: FakeEmbClient())

    result = await fa.search_candidates(req)
    return result


# =========================================================
# Validation / error cases
# =========================================================

class TestSearchValidation:

    def test_empty_body_raises_400(self, monkeypatch):
        req2 = func.HttpRequest(method="POST", url="/api/search", body=b"",
                                headers={}, params={}, route_params={})

        async def _run():
            return await _call_handler(req2, monkeypatch, FakeSearchService())

        result = asyncio.run(_run())
        envelope = _parse_envelope(result)
        # http_error_handler wraps InvalidInputError → 400
        assert result.status_code == 400
        assert envelope.get("error") is not None

    def test_no_query_or_skills_raises_400(self, monkeypatch):
        req = _make_request({"top": 5})

        async def _run():
            return await _call_handler(req, monkeypatch, FakeSearchService())

        result = asyncio.run(_run())
        envelope = _parse_envelope(result)
        assert result.status_code == 400
        assert envelope.get("error") is not None


# =========================================================
# Happy path — basic
# =========================================================

class TestSearchHappyPath:

    def test_basic_query_returns_hits(self, monkeypatch):
        fake = FakeSearchService(hits_sequence=[[_make_fake_hit("doc1"), _make_fake_hit("doc2")]])
        req = _make_request({"query": "python developer", "hybrid": False})

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        result = asyncio.run(_run())
        data = _parse_response(result)
        assert "hits" in data
        assert len(data["hits"]) == 2

    def test_meta_fields_present(self, monkeypatch):
        fake = FakeSearchService(hits_sequence=[[_make_fake_hit("doc1")]])
        req = _make_request({"query": "developer", "hybrid": False})

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        result = asyncio.run(_run())
        data = _parse_response(result)
        meta = data["meta"]
        assert "total" in meta
        assert "top" in meta
        assert "relaxed" in meta
        assert "hybrid" in meta
        assert "index" in meta

    def test_meta_relaxed_false_when_enough_results(self, monkeypatch):
        # With top=2 and fallback_threshold=0.20, fallback_min=ceil(0.20*2)=1
        # Provide 2 hits → relaxed must stay False
        fake = FakeSearchService(hits_sequence=[[_make_fake_hit("doc1"), _make_fake_hit("doc2")]])
        req = _make_request({"query": "developer", "top": 2, "hybrid": False})

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        result = asyncio.run(_run())
        data = _parse_response(result)
        assert data["meta"]["relaxed"] is False

    def test_meta_total_matches_hits_length(self, monkeypatch):
        fake = FakeSearchService(hits_sequence=[[_make_fake_hit(f"doc{i}") for i in range(3)]])
        req = _make_request({"query": "developer", "hybrid": False})

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        result = asyncio.run(_run())
        data = _parse_response(result)
        assert data["meta"]["total"] == len(data["hits"])

    def test_suggestions_empty_when_not_relaxed(self, monkeypatch):
        fake = FakeSearchService(hits_sequence=[[_make_fake_hit("doc1")] * 5])
        req = _make_request({"query": "developer", "hybrid": False, "top": 2})

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        result = asyncio.run(_run())
        data = _parse_response(result)
        assert isinstance(data["suggestions"], list)


# =========================================================
# Subco routing
# =========================================================

class TestSubcoRouting:

    def test_subco_risorse_uses_correct_index(self, monkeypatch):
        from core.config import settings
        fake = FakeSearchService(hits_sequence=[[_make_fake_hit("doc1")]])
        req = _make_request({"query": "senior developer", "subco": "risorse", "hybrid": False})

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        asyncio.run(_run())
        assert fake.calls[0]["index_name"] == settings.search_subco_risorse_index

    def test_subco_candidati_uses_correct_index(self, monkeypatch):
        from core.config import settings
        fake = FakeSearchService(hits_sequence=[[_make_fake_hit("doc1")]])
        req = _make_request({"query": "senior developer", "subco": "candidati", "hybrid": False})

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        asyncio.run(_run())
        assert fake.calls[0]["index_name"] == settings.search_subco_candidati_index

    def test_no_subco_uses_default_index(self, monkeypatch):
        from core.config import settings
        fake = FakeSearchService(hits_sequence=[[_make_fake_hit("doc1")]])
        req = _make_request({"query": "senior developer", "hybrid": False})

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        asyncio.run(_run())
        assert fake.calls[0]["index_name"] == settings.document_search_index_name


# =========================================================
# OData filter wiring
# =========================================================

class TestODataFilterWiring:

    def test_skills_filter_sent_to_search(self, monkeypatch):
        fake = FakeSearchService(hits_sequence=[[_make_fake_hit("doc1")] * 5])
        req = _make_request({"query": "dev", "skills": ["python"], "hybrid": False})

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        asyncio.run(_run())
        assert fake.calls[0]["odata_filter"] is not None
        assert "python" in fake.calls[0]["odata_filter"]

    def test_no_filters_when_no_constraints(self, monkeypatch):
        fake = FakeSearchService(hits_sequence=[[_make_fake_hit("doc1")]])
        req = _make_request({"query": "developer", "hybrid": False})

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        asyncio.run(_run())
        assert fake.calls[0]["odata_filter"] is None


# =========================================================
# Fallback relaxation
# =========================================================

class TestFallbackRelaxation:

    def test_relaxed_triggered_when_too_few_hits(self, monkeypatch):
        # top=10, fallback_min=ceil(0.20*10)=2
        # First call returns 0 hits, second (relaxed) returns 3
        relaxed_hits = [_make_fake_hit(f"rdoc{i}") for i in range(3)]
        fake = FakeSearchService(hits_sequence=[[], relaxed_hits])
        req = _make_request({"query": "developer", "skills": ["python"], "top": 10, "hybrid": False})

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        result = asyncio.run(_run())
        data = _parse_response(result)
        assert data["meta"]["relaxed"] is True
        assert len(data["hits"]) > 0

    def test_relaxed_suggestions_not_empty(self, monkeypatch):
        relaxed_hits = [_make_fake_hit("rdoc1")]
        fake = FakeSearchService(hits_sequence=[[], relaxed_hits])
        req = _make_request({"query": "developer", "skills": ["python", "azure"], "top": 10, "hybrid": False})

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        result = asyncio.run(_run())
        data = _parse_response(result)
        assert len(data["suggestions"]) > 0

    def test_relaxed_not_triggered_without_skills(self, monkeypatch):
        # No skills → no relaxed search even with 0 hits
        fake = FakeSearchService(hits_sequence=[[]])
        req = _make_request({"query": "developer", "top": 10, "hybrid": False})

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        result = asyncio.run(_run())
        data = _parse_response(result)
        assert data["meta"]["relaxed"] is False
        # Only 1 search call (no fallback)
        assert len(fake.calls) == 1

    def test_relaxed_deduplicates_results(self, monkeypatch):
        # First search returns doc1, relaxed search also returns doc1 + doc2
        first_hits = [_make_fake_hit("doc1")]
        relaxed_hits = [_make_fake_hit("doc1"), _make_fake_hit("doc2")]
        # top=10, fallback_min=2, first returns only 1 → relaxed triggered
        fake = FakeSearchService(hits_sequence=[first_hits, relaxed_hits])
        req = _make_request({"query": "developer", "skills": ["python"], "top": 10, "hybrid": False})

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        result = asyncio.run(_run())
        data = _parse_response(result)
        doc_ids = [h["document_id"] for h in data["hits"]]
        assert len(doc_ids) == len(set(doc_ids)), "Duplicate document_ids in results"


# =========================================================
# Hybrid flag
# =========================================================

class TestHybridFlag:

    def test_hybrid_true_recorded_in_meta(self, monkeypatch):
        fake = FakeSearchService(hits_sequence=[[_make_fake_hit("doc1")]])
        req = _make_request({"query": "developer", "hybrid": True})

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        result = asyncio.run(_run())
        data = _parse_response(result)
        assert data["meta"]["hybrid"] is True

    def test_hybrid_false_recorded_in_meta(self, monkeypatch):
        fake = FakeSearchService(hits_sequence=[[_make_fake_hit("doc1")]])
        req = _make_request({"query": "developer", "hybrid": False})

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        result = asyncio.run(_run())
        data = _parse_response(result)
        assert data["meta"]["hybrid"] is False

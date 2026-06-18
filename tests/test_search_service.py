"""
Tests per infra/search_service.py — Phase F:
  - search_chunks: lexical pass, vector pass, merge per document_id
  - upsert_chunks: delega corretta al client
  - delete_chunks: cerca e cancella per document_id
Tutti i test usano mock del SearchClient Azure per evitare dipendenze di rete.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import infra.search_service as search_mod
from infra.search_service import SearchService


# =========================================================
# Helpers
# =========================================================

def _make_hit(doc_id: str, score: float, fields: dict | None = None) -> dict:
    base = {
        "document_id": doc_id,
        "@search.score": score,
        "@search.highlights": {},
        "full_name": "Test User",
        "role": "dev",
        "location": "Milano",
        "skills": ["python"],
        "certifications": [],
        "seniority": "senior",
        "experience_years": 5.0,
        "language": "it",
        "availability": None,
        "version": 1,
        "chunk_index": 0,
        "section": None,
        "content": "text content",
        "source_path": "incoming/cv.pdf",
        "processed_at": "2026-04-01T00:00:00+00:00",
    }
    if fields:
        base.update(fields)
    return base


def _make_async_iter(items: list):
    """Return an async iterable that yields items."""
    async def _gen():
        for item in items:
            yield item
    class _AsyncIter:
        def __aiter__(self):
            return _gen()
    return _AsyncIter()


# =========================================================
# search_chunks — lexical only
# =========================================================

class TestSearchChunksLexical:

    def test_lexical_results_returned(self, monkeypatch):
        hits = [_make_hit("doc1", 0.8), _make_hit("doc2", 0.6)]

        async def fake_search(search_text=None, filter=None, top=None,
                              highlight_fields=None, select=None,
                              vector_queries=None, **kwargs):
            return _make_async_iter(hits)

        fake_client = MagicMock()
        fake_client.search = fake_search
        fake_client.close = AsyncMock()

        with patch("infra.search_service.SearchClient", return_value=fake_client), \
             patch("infra.search_service.DefaultAzureCredential", return_value=MagicMock()), \
             patch("infra.search_service.AzureKeyCredential", side_effect=lambda k: MagicMock()):
            svc = SearchService()

            async def _run():
                return await svc.search_chunks(query="python developer", embedding=None, top=5)

            results = asyncio.run(_run())

        assert len(results) == 2
        doc_ids = {r["document_id"] for r in results}
        assert "doc1" in doc_ids
        assert "doc2" in doc_ids

    def test_lex_score_set(self, monkeypatch):
        hits = [_make_hit("doc1", 0.75)]

        async def fake_search(**kwargs):
            return _make_async_iter(hits)

        fake_client = MagicMock()
        fake_client.search = fake_search
        fake_client.close = AsyncMock()

        with patch("infra.search_service.SearchClient", return_value=fake_client), \
             patch("infra.search_service.DefaultAzureCredential", return_value=MagicMock()), \
             patch("infra.search_service.AzureKeyCredential", side_effect=lambda k: MagicMock()):
            svc = SearchService()

            async def _run():
                return await svc.search_chunks(query="test", embedding=None, top=5)

            results = asyncio.run(_run())

        assert results[0]["lex_score"] == 0.75
        assert results[0]["vec_score"] == 0.0


# =========================================================
# search_chunks — merge on document_id
# =========================================================

class TestSearchChunksMerge:

    def test_merge_lex_and_vec_for_same_doc(self, monkeypatch):
        lex_hits = [_make_hit("doc1", 0.8)]
        vec_hits = [_make_hit("doc1", 0.9, {"@search.score": 0.9})]

        call_count = [0]

        async def fake_search(search_text=None, vector_queries=None, **kwargs):
            call_count[0] += 1
            if search_text is not None:
                return _make_async_iter(lex_hits)
            return _make_async_iter(vec_hits)

        fake_client = MagicMock()
        fake_client.search = fake_search
        fake_client.close = AsyncMock()

        embedding = [0.1] * 1536
        with patch("infra.search_service.SearchClient", return_value=fake_client), \
             patch("infra.search_service.DefaultAzureCredential", return_value=MagicMock()), \
             patch("infra.search_service.AzureKeyCredential", side_effect=lambda k: MagicMock()):
            svc = SearchService()

            async def _run():
                return await svc.search_chunks(query="python", embedding=embedding, top=5)

            results = asyncio.run(_run())

        # Same document_id merged into 1 result
        assert len(results) == 1
        r = results[0]
        assert r["lex_score"] == 0.8
        assert r["vec_score"] == 0.9

    def test_different_docs_not_merged(self, monkeypatch):
        lex_hits = [_make_hit("doc1", 0.8)]
        vec_hits = [_make_hit("doc2", 0.7, {"@search.score": 0.7})]

        async def fake_search(search_text=None, vector_queries=None, **kwargs):
            if search_text is not None:
                return _make_async_iter(lex_hits)
            return _make_async_iter(vec_hits)

        fake_client = MagicMock()
        fake_client.search = fake_search
        fake_client.close = AsyncMock()

        embedding = [0.1] * 1536
        with patch("infra.search_service.SearchClient", return_value=fake_client), \
             patch("infra.search_service.DefaultAzureCredential", return_value=MagicMock()), \
             patch("infra.search_service.AzureKeyCredential", side_effect=lambda k: MagicMock()):
            svc = SearchService()

            async def _run():
                return await svc.search_chunks(query="q", embedding=embedding, top=5)

            results = asyncio.run(_run())

        doc_ids = {r["document_id"] for r in results}
        assert "doc1" in doc_ids
        assert "doc2" in doc_ids

    def test_vec_only_when_no_query(self, monkeypatch):
        vec_hits = [_make_hit("doc1", 0.95, {"@search.score": 0.95})]

        async def fake_search(search_text=None, vector_queries=None, **kwargs):
            if vector_queries:
                return _make_async_iter(vec_hits)
            return _make_async_iter([])

        fake_client = MagicMock()
        fake_client.search = fake_search
        fake_client.close = AsyncMock()

        embedding = [0.2] * 1536
        with patch("infra.search_service.SearchClient", return_value=fake_client), \
             patch("infra.search_service.DefaultAzureCredential", return_value=MagicMock()), \
             patch("infra.search_service.AzureKeyCredential", side_effect=lambda k: MagicMock()):
            svc = SearchService()

            async def _run():
                return await svc.search_chunks(query="", embedding=embedding, top=5)

            results = asyncio.run(_run())

        # query="" means lexical pass skipped; should still get vec results
        assert any(r["vec_score"] > 0 for r in results)


# =========================================================
# search_chunks — normalised output fields
# =========================================================

class TestSearchChunksOutputFields:

    def test_output_fields_present(self, monkeypatch):
        hits = [_make_hit("doc1", 0.5)]

        async def fake_search(**kwargs):
            return _make_async_iter(hits)

        fake_client = MagicMock()
        fake_client.search = fake_search
        fake_client.close = AsyncMock()

        with patch("infra.search_service.SearchClient", return_value=fake_client), \
             patch("infra.search_service.DefaultAzureCredential", return_value=MagicMock()), \
             patch("infra.search_service.AzureKeyCredential", side_effect=lambda k: MagicMock()):
            svc = SearchService()

            async def _run():
                return await svc.search_chunks(query="q", top=5)

            results = asyncio.run(_run())

        expected_keys = {
            "document_id", "full_name", "role", "location", "skills",
            "certifications", "seniority", "experience_years", "language",
            "availability", "version", "source_path", "chunk_index", "content",
            "highlights", "lex_score", "vec_score", "score",
        }
        assert expected_keys.issubset(results[0].keys())

    def test_score_is_max_lex_vec(self, monkeypatch):
        hits = [_make_hit("doc1", 0.4)]

        async def fake_search(**kwargs):
            return _make_async_iter(hits)

        fake_client = MagicMock()
        fake_client.search = fake_search
        fake_client.close = AsyncMock()

        with patch("infra.search_service.SearchClient", return_value=fake_client), \
             patch("infra.search_service.DefaultAzureCredential", return_value=MagicMock()), \
             patch("infra.search_service.AzureKeyCredential", side_effect=lambda k: MagicMock()):
            svc = SearchService()

            async def _run():
                return await svc.search_chunks(query="q", top=5)

            results = asyncio.run(_run())

        r = results[0]
        assert r["score"] == max(r["lex_score"], r["vec_score"])


class _CaptionObj:
    def __init__(self, text: str):
        self.text = text


class TestSearchChunksSemanticCaptions:

    def test_semantic_caption_object_is_supported(self, monkeypatch):
        lex_hits = [_make_hit("doc1", 0.8, {"id": "chunk-1"})]
        semantic_hits = [
            _make_hit(
                "doc1",
                0.7,
                {
                    "id": "chunk-1",
                    "@search.reranker_score": 2.3,
                    "@search.captions": [_CaptionObj("Strong Java backend experience")],
                    "@search.highlights": {"content": ["<em>Java</em> backend"]},
                },
            )
        ]

        async def fake_search(search_text=None, query_type=None, **kwargs):
            if query_type == "semantic":
                return _make_async_iter(semantic_hits)
            return _make_async_iter(lex_hits)

        fake_client = MagicMock()
        fake_client.search = fake_search
        fake_client.close = AsyncMock()

        with patch("infra.search_service.SearchClient", return_value=fake_client), \
             patch("infra.search_service.DefaultAzureCredential", return_value=MagicMock()), \
             patch("infra.search_service.AzureKeyCredential", side_effect=lambda k: MagicMock()):
            svc = SearchService()

            async def _run():
                return await svc.search_chunks(query="java developer", embedding=None, top=5)

            results = asyncio.run(_run())

        assert len(results) == 1
        assert results[0]["semantic_score"] == 2.3
        assert "Strong Java backend experience" in (results[0]["semantic_evidence"] or "")


# =========================================================
# upsert_chunks
# =========================================================

class TestUpsertChunks:

    def test_calls_upload_documents(self, monkeypatch):
        uploaded = []

        async def fake_upload(docs):
            uploaded.extend(docs)

        fake_client = MagicMock()
        fake_client.upload_documents = fake_upload
        fake_client.close = AsyncMock()

        with patch("infra.search_service.SearchClient", return_value=fake_client), \
             patch("infra.search_service.DefaultAzureCredential", return_value=MagicMock()), \
             patch("infra.search_service.AzureKeyCredential", side_effect=lambda k: MagicMock()):
            svc = SearchService()
            chunk_docs = [{"id": "c1", "content": "text"}, {"id": "c2", "content": "more"}]

            async def _run():
                await svc.upsert_chunks(chunk_docs)

            asyncio.run(_run())

        assert len(uploaded) == 2
        assert uploaded[0]["id"] == "c1"

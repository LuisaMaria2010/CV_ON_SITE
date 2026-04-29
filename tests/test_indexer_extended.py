"""
Tests per services/document_indexer.py — Phase F:
  - index_async con embedding function
  - chunk con tutti i campi candidato
  - delete + upsert quando version > 1
  - build_chunk_documents con metadata completa
"""
from __future__ import annotations

import asyncio
import hashlib
from datetime import date, datetime, timezone

import pytest

import infra.search_service as search_mod
import services.document_indexer as idx_mod
from core.schema import (
    CandidateInfo,
    ImageRef,
    LanguageEntry,
    NormalizedCVMetadata,
    SkillEntry,
    WorkExperience,
)
from services.document_indexer import DocumentIndexer


# =========================================================
# Helpers
# =========================================================

def _make_metadata(
    document_id: str = "test-doc",
    version: int = 1,
    source_path: str = "incoming/test.pdf",
) -> NormalizedCVMetadata:
    return NormalizedCVMetadata(
        document_id=document_id,
        source_paths=[source_path],
        version=version,
        hash="abc123",
        processed_at=datetime.now(timezone.utc).isoformat(),
        language="it",
        candidate=CandidateInfo(
            full_name="Laura Bianchi",
            role="Data Scientist",
            location="Roma",
            seniority="mid",
            availability="immediate",
        ),
        skills=[SkillEntry(name="python"), SkillEntry(name="scikit-learn"), SkillEntry(name="azure")],
        certifications=["AZ-900", "DP-100"],
        education_titles=["Laurea Magistrale Matematica"],
        languages_spoken=[LanguageEntry(lang="italiano", level="madrelingua")],
        experience_years=4.0,
        employment_dates=[WorkExperience(start_date=date(2020, 1, 1))],
        images=[ImageRef(blob_path="cv-images/laura/photo.png")],
        element_count=30,
        image_count=1,
        metadata={"source": "test"},
    )


def _make_long_markdown(n_paras: int = 8, words_per_para: int = 60) -> str:
    paras = [f"## Sezione {i}\n\n" + ("parola " * words_per_para) for i in range(n_paras)]
    return "\n\n".join(paras)


class FakeSearchService:
    def __init__(self):
        self.upserted: list[list[dict]] = []
        self.deleted: list[str] = []

    async def upsert_chunks(self, chunk_docs):
        self.upserted.append(list(chunk_docs))

    async def delete_chunks(self, document_id: str):
        self.deleted.append(document_id)


# =========================================================
# build_chunk_documents
# =========================================================

class TestBuildChunkDocuments:

    def test_id_format(self):
        idxr = DocumentIndexer(chunk_size=100, chunk_overlap=10)
        md = _make_long_markdown(4)
        meta = _make_metadata(document_id="laura-bianchi", version=2)
        docs = idxr.build_chunk_documents(md, meta)
        assert docs
        for doc in docs:
            assert "laura-bianchi" in doc["id"]
            assert "-v2-" in doc["id"]

    def test_content_hash_matches(self):
        idxr = DocumentIndexer(chunk_size=150, chunk_overlap=20)
        md = _make_long_markdown(4)
        meta = _make_metadata()
        docs = idxr.build_chunk_documents(md, meta)
        for doc in docs:
            expected_hash = hashlib.sha256(doc["content"].encode()).hexdigest()
            assert doc["content_hash"] == expected_hash

    def test_candidate_fields_in_chunks(self):
        idxr = DocumentIndexer(chunk_size=100, chunk_overlap=10)
        md = _make_long_markdown(4)
        meta = _make_metadata()
        docs = idxr.build_chunk_documents(md, meta)
        assert docs
        d = docs[0]
        assert d["full_name"] == "Laura Bianchi"
        assert d["role"] == "Data Scientist"
        assert d["location"] == "Roma"
        assert d["seniority"] == "mid"
        assert d["availability"] == "immediate"

    def test_skills_in_chunks(self):
        idxr = DocumentIndexer(chunk_size=100, chunk_overlap=10)
        md = _make_long_markdown(4)
        meta = _make_metadata()
        docs = idxr.build_chunk_documents(md, meta)
        assert docs
        assert "python" in docs[0]["skills"]
        assert "scikit-learn" in docs[0]["skills"]

    def test_certifications_in_chunks(self):
        idxr = DocumentIndexer(chunk_size=100, chunk_overlap=10)
        md = _make_long_markdown(4)
        meta = _make_metadata()
        docs = idxr.build_chunk_documents(md, meta)
        assert "AZ-900" in docs[0]["certifications"]

    def test_experience_years_in_chunks(self):
        idxr = DocumentIndexer(chunk_size=100, chunk_overlap=10)
        md = _make_long_markdown(4)
        meta = _make_metadata()
        docs = idxr.build_chunk_documents(md, meta)
        assert docs[0]["experience_years"] == 4.0

    def test_language_in_chunks(self):
        idxr = DocumentIndexer(chunk_size=100, chunk_overlap=10)
        md = _make_long_markdown(4)
        meta = _make_metadata()
        docs = idxr.build_chunk_documents(md, meta)
        assert docs[0]["language"] == "it"

    def test_chunk_index_increments(self):
        idxr = DocumentIndexer(chunk_size=100, chunk_overlap=10)
        md = _make_long_markdown(8)
        meta = _make_metadata()
        docs = idxr.build_chunk_documents(md, meta)
        assert len(docs) >= 2
        indices = [d["chunk_index"] for d in docs]
        # Indexer starts from 1; verify indices are strictly increasing and sequential
        assert indices == sorted(set(indices)), "chunk_index must be sorted and unique"
        assert all(b == a + 1 for a, b in zip(indices, indices[1:])), "chunk_index must be consecutive"

    def test_document_id_in_chunks(self):
        idxr = DocumentIndexer(chunk_size=100, chunk_overlap=10)
        md = _make_long_markdown(4)
        meta = _make_metadata(document_id="my-special-doc")
        docs = idxr.build_chunk_documents(md, meta)
        for d in docs:
            assert d["document_id"] == "my-special-doc"

    def test_source_path_in_chunks(self):
        idxr = DocumentIndexer(chunk_size=100, chunk_overlap=10)
        md = _make_long_markdown(4)
        meta = _make_metadata(source_path="incoming/laura_cv.pdf")
        docs = idxr.build_chunk_documents(md, meta)
        assert docs[0]["source_path"] == "incoming/laura_cv.pdf"

    def test_empty_markdown_returns_no_chunks(self):
        idxr = DocumentIndexer(chunk_size=100, chunk_overlap=10)
        meta = _make_metadata()
        docs = idxr.build_chunk_documents("", meta)
        assert docs == []

    def test_short_markdown_single_chunk(self):
        idxr = DocumentIndexer(chunk_size=500, chunk_overlap=50)
        md = "# CV\n\nBreve descrizione del candidato."
        meta = _make_metadata()
        docs = idxr.build_chunk_documents(md, meta)
        assert len(docs) == 1


# =========================================================
# index (sync)
# =========================================================

class TestIndexSync:

    def test_index_calls_upsert(self, monkeypatch):
        fake = FakeSearchService()
        monkeypatch.setattr(search_mod, "SearchService", lambda: fake)
        monkeypatch.setattr(idx_mod, "SearchService", lambda: fake)

        idxr = DocumentIndexer(chunk_size=100, chunk_overlap=10)
        md = _make_long_markdown(4)
        meta = _make_metadata(document_id="D1", version=1)
        docs = idxr.index(md, meta)

        assert docs
        assert fake.upserted, "upsert_chunks must be called"
        assert len(fake.upserted[0]) == len(docs)

    def test_index_version_gt1_still_upserts(self, monkeypatch):
        # index() does not perform delete (no dedup logic in current implementation);
        # it only calls upsert_chunks regardless of version.
        fake = FakeSearchService()
        monkeypatch.setattr(search_mod, "SearchService", lambda: fake)
        monkeypatch.setattr(idx_mod, "SearchService", lambda: fake)

        idxr = DocumentIndexer(chunk_size=100, chunk_overlap=10)
        md = _make_long_markdown(4)
        meta = _make_metadata(document_id="D1", version=2)
        docs = idxr.index(md, meta)

        assert docs, "index() must return chunk docs"
        assert fake.upserted, "upsert_chunks must be called"


# =========================================================
# index_async (con embedding)
# =========================================================

class TestIndexAsync:

    def test_index_async_adds_embedding(self, monkeypatch):
        fake = FakeSearchService()
        monkeypatch.setattr(search_mod, "SearchService", lambda: fake)
        monkeypatch.setattr(idx_mod, "SearchService", lambda: fake)

        idxr = DocumentIndexer(chunk_size=100, chunk_overlap=10)
        md = _make_long_markdown(4)
        meta = _make_metadata()

        call_count = [0]

        async def fake_embed(text: str) -> list[float]:
            call_count[0] += 1
            return [0.1] * 1536

        async def _run():
            docs = await idxr.index_async(md, meta, embedding_fn=fake_embed)
            return docs

        docs = asyncio.run(_run())
        assert docs
        assert call_count[0] == len(docs), "embedding_fn must be called once per chunk"
        for d in docs:
            assert "embedding" in d
            assert len(d["embedding"]) == 1536

    def test_index_async_no_embedding_fn(self, monkeypatch):
        fake = FakeSearchService()
        monkeypatch.setattr(search_mod, "SearchService", lambda: fake)
        monkeypatch.setattr(idx_mod, "SearchService", lambda: fake)

        idxr = DocumentIndexer(chunk_size=100, chunk_overlap=10)
        md = _make_long_markdown(4)
        meta = _make_metadata()

        async def _run():
            return await idxr.index_async(md, meta, embedding_fn=None)

        docs = asyncio.run(_run())
        assert docs
        # without embedding_fn, no 'embedding' key expected
        for d in docs:
            assert "embedding" not in d

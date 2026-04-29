import hashlib
import asyncio
from datetime import datetime, timezone

import infra.search_service as search_mod
from services.document_indexer import DocumentIndexer
from core.schema import NormalizedCVMetadata, CandidateInfo, SkillEntry


def _make_metadata(document_id: str = "my-doc", version: int = 1, source_path: str = "incoming/x") -> NormalizedCVMetadata:
    return NormalizedCVMetadata(
        document_id=document_id,
        source_paths=[source_path],
        version=version,
        hash="abc123",
        processed_at=datetime.now(timezone.utc).isoformat(),
        language="it",
        candidate=CandidateInfo(full_name="Mario Rossi", role="Dev", seniority="senior"),
        skills=[SkillEntry(name="python"), SkillEntry(name="azure")],
        certifications=["AZ-900"],
        education_titles=[],
        experience_years=5.0,
    )


class FakeSearchService:
    def __init__(self):
        self.upserted = []

    async def upsert_chunks(self, chunk_docs):
        self.upserted.append(list(chunk_docs))


def test_chunk_markdown_and_build_docs(monkeypatch):
    idxr = DocumentIndexer(chunk_size=80, chunk_overlap=20)
    paras = [f"Paragraph {i} " + ("lorem " * 20) for i in range(6)]
    md = "\n\n".join(paras)

    chunks = idxr.chunk_markdown(md)
    assert len(chunks) >= 2

    meta = _make_metadata(document_id="My Doc", version=1, source_path="incoming/x")
    docs = idxr.build_chunk_documents(md, meta)
    assert docs
    for d in docs:
        assert "-v1-" in d["id"]
        h = hashlib.sha256(d["content"].encode("utf-8")).hexdigest()
        assert h == d["content_hash"]
        # candidate fields present
        assert d["full_name"] == "Mario Rossi"
        assert "python" in d["skills"]


def test_index_calls_search_upsert(monkeypatch):
    fake = FakeSearchService()
    monkeypatch.setattr(search_mod, "SearchService", lambda: fake)
    import services.document_indexer as idx_mod
    monkeypatch.setattr(idx_mod, "SearchService", lambda: fake)

    idxr = DocumentIndexer(chunk_size=60, chunk_overlap=10)
    paras = ["p " + ("x" * 100) for _ in range(4)]
    md = "\n\n".join(paras)

    meta = _make_metadata(document_id="D1", version=2, source_path="incoming/x")
    docs = idxr.index(md, meta)
    assert docs
    assert fake.upserted, "SearchService.upsert_chunks not called"
    assert len(fake.upserted[0]) == len(docs)

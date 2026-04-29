from __future__ import annotations

import os
import re
import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, List, Dict, Any, Optional

from core.config import settings
from core.schema import NormalizedCVMetadata
from infra.search_service import SearchService


logger = logging.getLogger(__name__)


class DocumentIndexer:
    """Chunk markdown documents and upsert chunk documents into search.

    Behavior:
    - Strips YAML front matter
    - Tries LangChain `MarkdownHeaderTextSplitter` + `RecursiveCharacterTextSplitter` if available
    - Falls back to paragraph-aggregation splitter
    - Chunk defaults: `AZURE_SEARCH_CHUNK_SIZE` (env) or 2000, overlap `AZURE_SEARCH_CHUNK_OVERLAP` or 200
    - Builds chunk docs with id `{sanitized_document_id}-v{version}-{idx:05d}` and computes per-chunk hash
    """

    def __init__(self, chunk_size: int | None = None, chunk_overlap: int | None = None):
        # prefer explicit args, then settings, then env defaults
        cs = chunk_size if chunk_size is not None else getattr(settings, "azure_search_chunk_size", None)
        co = chunk_overlap if chunk_overlap is not None else getattr(settings, "azure_search_chunk_overlap", None)
        self.chunk_size = int(cs or os.environ.get("AZURE_SEARCH_CHUNK_SIZE") or 2000)
        self.chunk_overlap = int(co or os.environ.get("AZURE_SEARCH_CHUNK_OVERLAP") or 200)

        # try import of langchain splitters if available
        try:
            from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

            self._header_splitter_cls = MarkdownHeaderTextSplitter
            self._recursive_splitter_cls = RecursiveCharacterTextSplitter
        except Exception:
            self._header_splitter_cls = None
            self._recursive_splitter_cls = None

    def _strip_front_matter(self, markdown_text: str) -> str:
        if not markdown_text:
            return ""
        if markdown_text.startswith("---\n"):
            end = markdown_text.find("\n---\n", 4)
            if end >= 0:
                return markdown_text[end + 5 :]
        return markdown_text

    def _char_fallback_split(self, text: str) -> List[Dict[str, Any]]:
        paras = [p.strip() for p in text.split("\n\n") if p.strip()]
        chunks: List[Dict[str, Any]] = []
        current = ""
        idx = 0
        for p in paras:
            if not current:
                current = p
            else:
                current = current + "\n\n" + p

            if len(current) >= self.chunk_size:
                idx += 1
                chunks.append({"chunk_index": idx, "section": "", "content": current})
                current = current[-self.chunk_overlap:]

        if current.strip():
            idx += 1
            chunks.append({"chunk_index": idx, "section": "", "content": current})

        return chunks

    def chunk_markdown(self, markdown_text: str) -> List[Dict[str, Any]]:
        text = self._strip_front_matter(markdown_text or "")

        # prefer langchain splitters if available
        if self._header_splitter_cls and self._recursive_splitter_cls:
            try:
                header_splitter = self._header_splitter_cls(headers_to_split_on=[("#", "h1"), ("##", "h2"), ("###", "h3")], strip_headers=False)
                header_docs = header_splitter.split_text(text)
                recursive_splitter = self._recursive_splitter_cls(chunk_size=self.chunk_size, chunk_overlap=self.chunk_overlap)
                split_docs = recursive_splitter.split_documents(header_docs)
                chunks: List[Dict[str, Any]] = []
                for idx, doc in enumerate(split_docs, start=1):
                    meta = getattr(doc, "metadata", {}) or {}
                    section = meta.get("h3") or meta.get("h2") or meta.get("h1") or ""
                    chunks.append({"chunk_index": idx, "section": str(section), "content": doc.page_content.strip()})
                return [c for c in chunks if c["content"]]
            except Exception:
                logger.exception("LangChain splitting failed, falling back to simple splitter")

        return self._char_fallback_split(text)

    def _sanitize_id(self, value: str) -> str:
        return "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in (value or "")).strip("-") or "doc"

    def build_chunk_documents(
        self,
        markdown_text: str,
        metadata: NormalizedCVMetadata,
    ) -> List[Dict[str, Any]]:
        processed_at = metadata.processed_at or datetime.now(timezone.utc).isoformat()
        chunks = self.chunk_markdown(markdown_text)
        sanitized = self._sanitize_id(metadata.document_id)
        version = metadata.version
        source_path = metadata.source_paths[0] if metadata.source_paths else ""
        candidate = metadata.candidate or {}

        # flatten candidate fields
        full_name = getattr(candidate, "full_name", None) or (candidate.get("full_name") if isinstance(candidate, dict) else None)
        role = getattr(candidate, "role", None) or (candidate.get("role") if isinstance(candidate, dict) else None)
        location = getattr(candidate, "location", None) or (candidate.get("location") if isinstance(candidate, dict) else None)
        seniority = getattr(candidate, "seniority", None) or (candidate.get("seniority") if isinstance(candidate, dict) else None)
        availability = getattr(candidate, "availability", None) or (candidate.get("availability") if isinstance(candidate, dict) else None)

        docs: List[Dict[str, Any]] = []
        for c in chunks:
            idx = int(c.get("chunk_index") or 0)
            content = (c.get("content") or "").strip()
            if not content:
                continue
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            doc_id = f"{sanitized}-v{int(version)}-{idx:05d}"
            docs.append({
                "id": doc_id,
                "document_id": metadata.document_id,
                "version": int(version),
                "chunk_index": idx,
                "section": c.get("section") or "",
                "content": content,
                "content_hash": content_hash,
                "source_path": source_path,
                "processed_at": processed_at,
                # candidate metadata
                "full_name": full_name,
                "role": role,
                "location": location,
                "seniority": seniority,
                "availability": availability,
                "skills": [s.name if hasattr(s, "name") else str(s) for s in (metadata.skills or [])],
                "certifications": list(metadata.certifications or []),
                "experience_years": metadata.experience_years,
                "language": metadata.language,
            })

        return docs

    def upsert_chunks(self, chunk_docs: List[Dict[str, Any]]) -> None:
        if not chunk_docs:
            logger.debug("No chunks to upsert")
            return
        try:
            search = SearchService()
            asyncio.run(search.upsert_chunks(chunk_docs))
        except Exception:
            logger.exception("Failed to upsert chunks into search")

    def index(self, markdown_text: str, metadata: NormalizedCVMetadata) -> List[Dict[str, Any]]:
        docs = self.build_chunk_documents(markdown_text, metadata)
        if not docs:
            logger.info("Indexing skipped for document_id=%s (too short or no chunks)", metadata.document_id)
            return []
        self.upsert_chunks(docs)
        return docs

    async def index_async(
        self,
        markdown_text: str,
        metadata: NormalizedCVMetadata,
        embedding_fn: Optional[Callable[[str], Awaitable[list[float]]]] = None,
    ) -> List[Dict[str, Any]]:
        """Build chunk docs, optionally inject embeddings, then upsert asynchronously."""
        docs = self.build_chunk_documents(markdown_text, metadata)
        if not docs:
            logger.info("Indexing skipped (async) for document_id=%s", metadata.document_id)
            return []

        if embedding_fn is not None:
            for doc in docs:
                try:
                    doc["embedding"] = await embedding_fn(doc["content"])
                except Exception:
                    logger.exception("Embedding failed for chunk id=%s", doc.get("id"))

        search = SearchService()
        await search.upsert_chunks(docs)
        return docs

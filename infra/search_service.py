"""
Azure AI Search wrapper.

Responsabilità:
- upsert documenti
- creazione/aggiornamento indice (setup)
- ricerca ibrida (lexical + vector)
- zero logica business
"""

import logging
import re
from typing import Any
from azure.identity.aio import DefaultAzureCredential
from azure.search.documents.aio import SearchClient
from azure.search.documents.models import VectorizedQuery
from azure.search.documents.indexes.aio import SearchIndexClient
from azure.search.documents.indexes.models import (
    SearchIndex,
    SearchField,
    SearchFieldDataType,
    SimpleField,
    SearchableField,
    VectorSearch,
    HnswAlgorithmConfiguration,
    VectorSearchProfile,
    SemanticConfiguration,
    SemanticSearch,
    SemanticPrioritizedFields,
    SemanticField,
)
from azure.core.credentials import AzureKeyCredential

from core.config import settings


logger = logging.getLogger(__name__)

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = _HTML_TAG_RE.sub(" ", str(value))
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _build_semantic_evidence(
    *,
    caption: str | None,
    highlights: dict[str, list[str]] | None,
) -> str | None:
    parts: list[str] = []

    clean_caption = _clean_text(caption)
    if clean_caption:
        parts.append(clean_caption)

    if isinstance(highlights, dict):
        for snippets in highlights.values():
            if not isinstance(snippets, list):
                continue
            for snippet in snippets:
                clean_snippet = _clean_text(snippet)
                if clean_snippet:
                    parts.append(clean_snippet)
                if len(parts) >= 6:
                    break
            if len(parts) >= 6:
                break

    deduped: list[str] = []
    seen: set[str] = set()
    for item in parts:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    if not deduped:
        return None

    return " | ".join(deduped)[:1000]


def _extract_caption_text(captions: list[Any] | None) -> str | None:
    if not captions:
        return None
    first = captions[0]
    if isinstance(first, dict):
        text = first.get("text")
        return str(text).strip() if text else None
    text = getattr(first, "text", None)
    if text:
        return str(text).strip()
    return None


class SearchService:

    def __init__(self):
        # Prefer API key when provided, otherwise use DefaultAzureCredential
        if settings.azure_search_api_key:
            credential = AzureKeyCredential(settings.azure_search_api_key)
        else:
            credential = DefaultAzureCredential()

        # primary client for candidate documents
        self._credential = credential
        self.client = SearchClient(
            endpoint=settings.search_endpoint,
            index_name=settings.search_index_name,
            credential=credential,
        )

        # helper: chunk index name (may differ from candidate index)
        self.chunk_index_name = getattr(settings, "document_search_index_name", settings.search_index_name)

    async def upsert_candidate(self, match_key: str, cv):
        """
        Upsert document nel search index.
        """
        doc = {
            "id": match_key,  # chiave univoca
            "full_name": cv.full_name,
            "role": cv.role,
            "location": cv.location,
            "skills": [s.name if hasattr(s, "name") else str(s) for s in (cv.skills or [])],
            "seniority": cv.seniority,
            "experience_years": cv.experience_years,
            "text": " ".join(s.name if hasattr(s, "name") else str(s) for s in (cv.skills or [])),  # searchable fallback
        }

        await self.client.upload_documents([doc])

    async def upsert_chunks(self, chunk_docs: list[dict]):
        """
        Upsert chunk documents into the configured chunks index.
        Each dict in `chunk_docs` should contain the fields expected by the index,
        including `id` as the document key.
        """
        client = SearchClient(
            endpoint=settings.search_endpoint,
            index_name=self.chunk_index_name,
            credential=self._credential,
        )
        try:
            # upload_documents will create or replace documents
            await client.upload_documents(chunk_docs)
        finally:
            await client.close()

    async def delete_chunks(self, document_id: str):
        """
        Delete all chunks for a given document_id from the chunks index.
        """
        client = SearchClient(
            endpoint=settings.search_endpoint,
            index_name=self.chunk_index_name,
            credential=self._credential,
        )
        try:
            # find all documents with document_id
            query = f"document_id eq '{document_id}'"
            ids = []
            results = client.search(search_text="*", filter=query, top=1000)
            async for r in results:
                if hasattr(r, 'id'):
                    ids.append(r.id)
                else:
                    # try retrieving 'id' from dict-like
                    try:
                        ids.append(r['id'])
                    except Exception:
                        pass

            if ids:
                # delete by id
                docs = [{"id": i} for i in ids]
                await client.delete_documents(docs)
        finally:
            await client.close()

    async def close(self):
        await self.client.close()

    async def create_or_update_index(self, index_name: str | None = None) -> None:
        """
        Crea o aggiorna il mapping dell'indice chunk su Azure Search.
        Da eseguire in deploy/setup, non ad ogni upsert.
        """
        target_index = index_name or self.chunk_index_name

        fields = [
            SimpleField(name="id", type=SearchFieldDataType.String, key=True, filterable=True),
            SimpleField(name="document_id", type=SearchFieldDataType.String, filterable=True, retrievable=True),
            SearchableField(name="full_name", type=SearchFieldDataType.String, retrievable=True),
            SearchableField(name="role", type=SearchFieldDataType.String, filterable=True, facetable=True, retrievable=True),
            SimpleField(name="location", type=SearchFieldDataType.String, filterable=True, facetable=True, retrievable=True),
            SearchField(
                name="skills",
                type=SearchFieldDataType.Collection(SearchFieldDataType.String),
                searchable=True,
                filterable=True,
                facetable=True,
                retrievable=True,
            ),
            SearchField(
                name="certifications",
                type=SearchFieldDataType.Collection(SearchFieldDataType.String),
                filterable=True,
                facetable=True,
                retrievable=True,
            ),
            SimpleField(name="seniority", type=SearchFieldDataType.String, filterable=True, facetable=True, retrievable=True),
            SimpleField(name="experience_years", type=SearchFieldDataType.Double, filterable=True, sortable=True, retrievable=True),
            SimpleField(name="language", type=SearchFieldDataType.String, filterable=True, retrievable=True),
            SimpleField(name="availability", type=SearchFieldDataType.String, filterable=True, retrievable=True),
            SimpleField(name="version", type=SearchFieldDataType.Int32, filterable=True, sortable=True, retrievable=True),
            SimpleField(name="chunk_index", type=SearchFieldDataType.Int32, retrievable=True),
            SimpleField(name="section", type=SearchFieldDataType.String, retrievable=True),
            SearchableField(name="content", type=SearchFieldDataType.String, retrievable=True),
            SimpleField(name="source_path", type=SearchFieldDataType.String, retrievable=True),
            SimpleField(name="processed_at", type=SearchFieldDataType.DateTimeOffset, filterable=True, sortable=True, retrievable=True),
            SimpleField(name="content_hash", type=SearchFieldDataType.String, retrievable=True),
            SearchField(
                name="embedding",
                type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                searchable=True,
                retrievable=False,
                vector_search_dimensions=settings.search_vector_dimensions,
                vector_search_profile_name="hnsw-profile",
            ),
        ]

        vector_search = VectorSearch(
            algorithms=[HnswAlgorithmConfiguration(name="hnsw-algo")],
            profiles=[VectorSearchProfile(name="hnsw-profile", algorithm_configuration_name="hnsw-algo")],
        )

        semantic_config = SemanticConfiguration(
            name="cv-semantic",
            prioritized_fields=SemanticPrioritizedFields(
                content_fields=[SemanticField(field_name="content")],
                keywords_fields=[SemanticField(field_name="skills")],
            ),
        )

        index = SearchIndex(
            name=target_index,
            fields=fields,
            vector_search=vector_search,
            semantic_search=SemanticSearch(configurations=[semantic_config]),
        )

        index_client = SearchIndexClient(
            endpoint=settings.search_endpoint,
            credential=self._credential,
        )
        try:
            await index_client.create_or_update_index(index)
            logger.info("Index created/updated: %s", target_index)
        finally:
            await index_client.close()

    async def search_chunks(
        self,
        query: str,
        odata_filter: str | None = None,
        embedding: list[float] | None = None,
        top: int = 10,
        highlight_fields: str = "content",
        index_name: str | None = None,
    ) -> list[dict]:
        """
        Ricerca ibrida (keyword -> semantic sui candidati keyword + vector) su chunk index.
        Ritorna lista normalizzata con: document_id, full_name, role,
        location, skills, seniority, experience_years, score, highlights, source_path, version.
        """
        target_index = index_name or self.chunk_index_name
        client = SearchClient(
            endpoint=settings.search_endpoint,
            index_name=target_index,
            credential=self._credential,
        )

        select_fields = [
            "id", "document_id", "full_name", "role", "location",
            "skills", "certifications", "seniority", "experience_years",
            "language", "availability", "version", "chunk_index",
            "section", "content", "source_path", "processed_at",
        ]

        # Chunk-level merge: key by chunk id, keep document_id as metadata.
        merged: dict[str, dict] = {}

        try:
            if query:
                # --- step 1: keyword search pura ---
                keyword_results = await client.search(
                    search_text=query,
                    filter=odata_filter,
                    top=top * 5,
                    highlight_fields=highlight_fields,
                    select=select_fields,
                    query_type="simple",
                )

                candidate_chunk_ids: list[str] = []
                keyword_chunk_scores: dict[str, float] = {}
                keyword_chunk_entries: dict[str, dict] = {}
                async for hit in keyword_results:
                    chunk_id = hit.get("id") or hit.get("document_id", "")
                    if not chunk_id:
                        continue
                    score = hit.get("@search.score", 0.0)

                    if chunk_id not in keyword_chunk_scores:
                        candidate_chunk_ids.append(chunk_id)
                        keyword_chunk_scores[chunk_id] = score
                        keyword_chunk_entries[chunk_id] = dict(hit)
                    elif score > keyword_chunk_scores[chunk_id]:
                        keyword_chunk_scores[chunk_id] = score
                        keyword_chunk_entries[chunk_id] = dict(hit)

                MAX_SEMANTIC_CANDIDATES = 20
                candidate_chunk_ids = candidate_chunk_ids[:MAX_SEMANTIC_CANDIDATES]

                # --- step 2: semantic query solo sui candidati keyword ---
                candidate_filter = None
                if candidate_chunk_ids:
                    clauses = [
                        "id eq '{0}'".format(chunk_id.replace("'", "''"))
                        for chunk_id in candidate_chunk_ids
                    ]
                    ids_filter = " or ".join(clauses)
                    candidate_filter = f"({odata_filter}) and ({ids_filter})" if odata_filter else ids_filter

                if candidate_filter:
                    try:
                        semantic_results = await client.search(
                            search_text=query,
                            filter=candidate_filter,
                            top=top * 3,
                            select=select_fields,
                            highlight_fields=highlight_fields,
                            query_type="semantic",
                            semantic_configuration_name="cv-semantic",
                            query_caption="extractive",
                        )

                        async for hit in semantic_results:
                            chunk_id = hit.get("id") or hit.get("document_id", "")
                            if not chunk_id:
                                continue

                            captions = hit.get("@search.captions", [])
                            semantic_caption = _extract_caption_text(captions)
                            semantic_highlights = dict(hit.get("@search.highlights") or {})

                            semantic_evidence = _build_semantic_evidence(
                                caption=semantic_caption,
                                highlights=semantic_highlights,
                            )

                            entry = dict(hit)
                            entry["lex_score"] = keyword_chunk_scores.get(chunk_id, 0.0)
                            entry["semantic_score"] = hit.get("@search.reranker_score", 0.0)
                            entry["vec_score"] = 0.0
                            entry["semantic_evidence"] = semantic_evidence
                            entry["highlights"] = semantic_highlights
                            merged[chunk_id] = entry
                    except Exception as exc:
                        # Semantic configuration/filter issues should not fail the whole search.
                        logger.warning("Semantic pass failed; using keyword candidates only: %s", exc)

                # Fallback: mantieni candidati keyword anche senza risultato semantic.
                for chunk_id in candidate_chunk_ids:
                    if chunk_id in merged:
                        continue
                    base = keyword_chunk_entries.get(chunk_id, {})
                    entry = dict(base)
                    entry["lex_score"] = keyword_chunk_scores.get(chunk_id, 0.0)
                    entry["semantic_score"] = 0.0
                    entry["vec_score"] = 0.0
                    entry["semantic_evidence"] = None
                    entry["highlights"] = dict(base.get("@search.highlights") or {})
                    merged[chunk_id] = entry

            # --- step 3: vector pass ---
            if embedding:
                vec_query = VectorizedQuery(
                    vector=embedding,
                    k_nearest_neighbors=top * 3,
                    fields="embedding",
                )
                vec_results = await client.search(
                    search_text=None,
                    vector_queries=[vec_query],
                    filter=odata_filter,
                    top=top * 3,
                    select=select_fields,
                )
                async for hit in vec_results:
                    chunk_id = hit.get("id") or hit.get("document_id", "")
                    score = hit.get("@search.score", 0.0)
                    if chunk_id in merged:
                        merged[chunk_id]["vec_score"] = score
                    else:
                        entry = dict(hit)
                        entry["lex_score"] = 0.0
                        entry["semantic_score"] = 0.0
                        entry["vec_score"] = score
                        entry["semantic_evidence"] = None
                        entry["highlights"] = {}
                        merged[chunk_id] = entry

        finally:
            await client.close()

        # normalise output
        hits = []
        for entry in merged.values():
            semantic_score = entry.get("semantic_score", 0.0) or 0.0
            lex_score = entry.get("lex_score", 0.0) or 0.0
            vec_score = entry.get("vec_score", 0.0) or 0.0
            hits.append({
                "id": entry.get("id"),
                "document_id": entry.get("document_id"),
                "full_name": entry.get("full_name"),
                "role": entry.get("role"),
                "location": entry.get("location"),
                "skills": entry.get("skills") or [],
                "certifications": entry.get("certifications") or [],
                "seniority": entry.get("seniority"),
                "experience_years": entry.get("experience_years"),
                "language": entry.get("language"),
                "availability": entry.get("availability"),
                "version": entry.get("version"),
                "source_path": entry.get("source_path"),
                "chunk_index": entry.get("chunk_index"),
                "content": entry.get("content"),
                "highlights": entry.get("highlights") or {},
                "lex_score": entry.get("lex_score", 0.0),
                "semantic_score": entry.get("semantic_score", 0.0),
                "vec_score": entry.get("vec_score", 0.0),
                "semantic_evidence": entry.get("semantic_evidence"),
                "score": (
                    semantic_score
                    if semantic_score > 0
                    else max(lex_score, vec_score)
                ),
            })

        return hits

    async def load_chunks_for_candidates(
        self,
        document_ids: list[str],
        *,
        index_name: str | None = None,
        per_candidate_limit: int = 40,
    ) -> dict[str, list[dict]]:
        """
        Carica tutti (o quasi) i chunk per una lista di document_id.
        Usato per l'aggregazione evidence a livello candidato dopo il rerank chunk.
        """
        ids = [str(v).strip() for v in (document_ids or []) if str(v).strip()]
        if not ids:
            return {}

        target_index = index_name or self.chunk_index_name
        client = SearchClient(
            endpoint=settings.search_endpoint,
            index_name=target_index,
            credential=self._credential,
        )

        escaped = [f"document_id eq '{doc_id.replace(chr(39), chr(39) * 2)}'" for doc_id in ids]
        doc_filter = " or ".join(escaped)
        top = max(1, min(1000, len(ids) * max(1, int(per_candidate_limit))))

        select_fields = [
            "id", "document_id", "full_name", "role", "location",
            "skills", "certifications", "seniority", "experience_years",
            "language", "availability", "version", "chunk_index",
            "section", "content", "source_path", "processed_at",
        ]

        grouped: dict[str, list[dict]] = {doc_id: [] for doc_id in ids}
        try:
            results = await client.search(
                search_text="*",
                filter=doc_filter,
                top=top,
                select=select_fields,
                query_type="simple",
            )
            async for hit in results:
                doc_id = str(hit.get("document_id") or "").strip()
                if not doc_id:
                    continue
                grouped.setdefault(doc_id, []).append(dict(hit))
        finally:
            await client.close()

        return grouped

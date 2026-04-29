"""
Azure AI Search wrapper.

Responsabilità:
- upsert documenti
- creazione/aggiornamento indice (setup)
- ricerca ibrida (lexical + vector)
- zero logica business
"""

import logging
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
        Ricerca ibrida (lexical + vector) su chunk index.
        Merge per document_id tenendo lo score migliore per ogni risultato.
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

        merged: dict[str, dict] = {}

        try:
            # --- lexical pass ---
            if query:
                lex_results = await client.search(
                    search_text=query,
                    filter=odata_filter,
                    top=top * 3,
                    highlight_fields=highlight_fields,
                    select=select_fields,
                )
                async for hit in lex_results:
                    doc_id = hit.get("document_id") or hit.get("id", "")
                    score = hit.get("@search.score", 0.0)
                    entry = dict(hit)
                    entry["lex_score"] = score
                    entry["vec_score"] = 0.0
                    entry["highlights"] = dict(hit.get("@search.highlights") or {})
                    merged[doc_id] = entry

            # --- vector pass ---
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
                    doc_id = hit.get("document_id") or hit.get("id", "")
                    score = hit.get("@search.score", 0.0)
                    if doc_id in merged:
                        merged[doc_id]["vec_score"] = score
                    else:
                        entry = dict(hit)
                        entry["lex_score"] = 0.0
                        entry["vec_score"] = score
                        entry["highlights"] = {}
                        merged[doc_id] = entry

        finally:
            await client.close()

        # normalise output
        hits = []
        for entry in merged.values():
            hits.append({
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
                "vec_score": entry.get("vec_score", 0.0),
                # composite score computed by caller (reranker in Phase E)
                "score": max(entry.get("lex_score", 0.0), entry.get("vec_score", 0.0)),
            })

        return hits

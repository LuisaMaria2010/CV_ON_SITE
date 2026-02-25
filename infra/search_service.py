"""
Azure AI Search wrapper.

Responsabilità:
- upsert documenti
- zero logica business
"""

import logging
from azure.identity.aio import DefaultAzureCredential
from azure.search.documents.aio import SearchClient

from core.config import settings


logger = logging.getLogger(__name__)


class SearchService:

    def __init__(self):
        self.client = SearchClient(
            endpoint=settings.search_endpoint,
            index_name=settings.search_index_name,
            credential=DefaultAzureCredential(),
        )

    async def upsert_candidate(self, match_key: str, cv):
        """
        Upsert document nel search index.
        """
        doc = {
            "id": match_key,  # chiave univoca
            "full_name": cv.full_name,
            "role": cv.role,
            "location": cv.location,
            "skills": cv.skills,
            "seniority": cv.seniority,
            "experience_years": cv.experience_years,
            "text": " ".join(cv.skills or []),  # searchable fallback
        }

        await self.client.upload_documents([doc])

    async def close(self):
        await self.client.close()

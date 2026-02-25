"""
Layer di cache per testo e JSON estratti durante il parsing CV.

Struttura blob:
    container: raw-text-cache
        {hash}.txt
        {hash}.json
"""

from typing import Optional
import json

from azure.core.exceptions import ResourceNotFoundError

from infra.blob_storage import StorageService
from core.config import settings
from utils.observability import track_event
from utils.request_context import get_request_id


class TextCache:
    """
    Cache testo e JSON LLM basata su Azure Blob Storage.

    Usa un container dedicato configurato in:
        settings.storage_container_cache
    """

    def __init__(self, storage: StorageService):
        self.storage = storage
        self.container = settings.storage_container_cache

    # =========================================================
    # TEXT CACHE
    # =========================================================

    async def get(self, file_hash: str) -> Optional[str]:
        name = f"{file_hash}.txt"

        try:
            data = await self.storage.download_bytes(
                name,
                container=self.container,
            )

            track_event("cache_hit_text", request_id=get_request_id())

            return data.decode("utf-8")

        except ResourceNotFoundError:
            track_event("cache_miss_text", request_id=get_request_id())
            return None

    # ---------------------------------------------------------

    async def save(self, file_hash: str, text: str) -> None:
        name = f"{file_hash}.txt"

        await self.storage.upload_bytes(
            text.encode("utf-8"),
            name,
            container=self.container,
        )

    # =========================================================
    # JSON CACHE
    # =========================================================

    async def get_json(self, key: str) -> Optional[dict]:
        name = f"{key}.json"

        try:
            data = await self.storage.download_bytes(
                name,
                container=self.container,
            )

            track_event("cache_hit_json", request_id=get_request_id())

            return json.loads(data.decode("utf-8"))

        except ResourceNotFoundError:
            track_event("cache_miss_json", request_id=get_request_id())
            return None

    # ---------------------------------------------------------

    async def save_json(self, key: str, payload: dict) -> None:
        name = f"{key}.json"

        await self.storage.upload_bytes(
            json.dumps(payload).encode("utf-8"),
            name,
            container=self.container,
        )

"""
StorageService
==============

Wrapper minimale e async per Azure Blob Storage pensato per Azure Functions serverless.

Design principles:
- Solo operazioni bytes (NO filesystem locale)
- Async I/O per evitare blocchi del worker
- Connection string (per Function Apps) o Managed Identity
- Retry automatico con exponential backoff
- API minimale (upload/download/exists/delete)

Utilizzo tipico nel progetto FlashCV:
- incoming-cv/       -> file originali
- raw-text-cache/    -> testo estratto (hash.txt)
- json-cache/        -> JSON LLM opzionale

"""

import asyncio
import logging
import os
from typing import Optional, Callable, Awaitable, Any

from azure.storage.blob.aio import BlobServiceClient
from azure.core.exceptions import (
    ResourceNotFoundError,
    ResourceExistsError,
    ServiceRequestError,
    HttpResponseError,
)
from utils.observability import track_event
from utils.request_context import get_request_id
from core.config import settings

logger = logging.getLogger(__name__)


class StorageService:
    """
    Async wrapper per Azure Blob Storage.

    Tutte le operazioni sono retry-safe e usano Managed Identity.
    Espone API minimale per upload, download, exists, delete di blob.

    Attributi:
        credential: DefaultAzureCredential
        service: BlobServiceClient
        default_container: str
    """

    # -------------------------
    # Config retry (tunable)
    # -------------------------
    MAX_RETRIES = 1
    BASE_DELAY = 0.4  # seconds

    def __init__(self):
        # Try connection string first (Function Apps), then account URL
        connection_string = settings.storage_account_connection_string or settings.storage_connection_string

        if connection_string:
            self.service = BlobServiceClient.from_connection_string(connection_string)
        else:
            # Fallback to account URL (requires Managed Identity)
            try:
                from azure.identity.aio import DefaultAzureCredential
                self.service = BlobServiceClient(
                    account_url=settings.storage_account_url,
                    credential=DefaultAzureCredential(),
                )
            except ImportError:
                raise ValueError(
                    "No connection string found and DefaultAzureCredential not available. "
                    "Set AzureWebJobsStorage environment variable."
                )
        
        self.default_container = settings.storage_container_incoming

    # =========================================================
    # Internal helpers
    # =========================================================

    def _name(self, blob: str, folder: Optional[str]) -> str:
        """Costruisce il path blob con slash (cross-platform safe)."""
        if folder:
            return f"{folder.strip('/')}/{blob.lstrip('/')}"
        return blob.lstrip("/")


    async def _retry(self, fn: Callable[[], Awaitable[Any]]) -> Any:
        """
        Retry con exponential backoff per errori transitori Azure.

        Ritenta su:
        - network error
        - throttling (429)
        - service unavailable (503)

        Raises l'ultima eccezione se fallisce.
        """
        for attempt in range(self.MAX_RETRIES):
            try:
                return await fn()

            except ServiceRequestError as e:
                if attempt == self.MAX_RETRIES - 1:
                    raise
            except HttpResponseError as e:
                if e.status_code not in (429, 500, 502, 503, 504):
                    raise
                if attempt == self.MAX_RETRIES - 1:
                    raise

                delay = self.BASE_DELAY * (2**attempt)

                track_event(
                    "blob_retry",
                    request_id=get_request_id(),
                    properties={
                        "attempt": attempt + 1,
                        "max_retries": self.MAX_RETRIES,
                        "delay_sec": delay,
                        "error": type(e).__name__,
                    },
                )

                logger.warning(
                    "Blob retry %s/%s after error: %s (sleep %.2fs)",
                    attempt + 1,
                    self.MAX_RETRIES,
                    type(e).__name__,
                    delay,
                )

                await asyncio.sleep(delay)

    # =========================================================
    # Public API
    # =========================================================

    async def upload_bytes(
        self,
        data: bytes,
        blob_name: str,
        folder: Optional[str] = None,
        container: Optional[str] = None,
    ) -> None:
        """
        Carica bytes su Blob Storage (overwrite=True).

        Tipico uso:
            await upload_bytes(text.encode(), "hash.txt", "raw-text-cache")
        """
        container = container or self.default_container
        name = self._name(blob_name, folder)

        async def _ensure_container():
            container_client = self.service.get_container_client(container)
            try:
                await container_client.create_container()
            except ResourceExistsError:
                pass

        await self._retry(_ensure_container)

        async def _op():
            client = self.service.get_blob_client(container, name)
            await client.upload_blob(data, overwrite=True)

        await self._retry(_op)

    async def download_bytes(
        self,
        blob_name: str,
        folder: Optional[str] = None,
        container: Optional[str] = None,
    ) -> bytes:
        """
        Scarica contenuto blob come bytes.

        Raises:
            ResourceNotFoundError se non esiste.
        """
        container = container or self.default_container
        name = self._name(blob_name, folder)

        async def _op():
            client = self.service.get_blob_client(container, name)
            stream = await client.download_blob()
            return await stream.readall()
        try: 
            return await self._retry(_op)
        except Exception as e:
            logger.info("BLOB ERROR: %s", repr(e))
            raise

    async def exists(
        self,
        blob_name: str,
        folder: Optional[str] = None,
        container: Optional[str] = None,
    ) -> bool:
        """
        Verifica esistenza blob (cheap call).
        """
        container = container or self.default_container
        name = self._name(blob_name, folder)

        async def _op():
            client = self.service.get_blob_client(container, name)
            return await client.exists()

        try:
            return await self._retry(_op)
        except ResourceNotFoundError:
            return False

    async def delete(
        self,
        blob_name: str,
        folder: Optional[str] = None,
        container: Optional[str] = None,
    ) -> None:
        """
        Cancella blob se esiste.
        Safe: non lancia errore se non trovato.
        """
        container = container or self.default_container
        name = self._name(blob_name, folder)

        async def _op():
            client = self.service.get_blob_client(container, name)
            await client.delete_blob()

        try:
            await self._retry(_op)
        except ResourceNotFoundError:
            pass

    async def close(self):
        """Chiude client e credential async."""
        await self.service.close()
        await self.credential.close()
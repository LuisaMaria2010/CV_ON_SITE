"""
QueueService
============

Async wrapper per Azure Storage Queue.

Responsabilità:
- invio messaggi JSON
- retry automatico
- observability
- zero logica business

Design:
- separato da Blob (SRP)
- serverless friendly
"""

import asyncio
import json
import logging
from typing import Any, Callable, Awaitable

from azure.identity.aio import DefaultAzureCredential
from azure.storage.queue.aio import QueueServiceClient
from azure.core.exceptions import (
    ServiceRequestError,
    HttpResponseError,
)

from core.config import settings
from utils.observability import track_event
from utils.request_context import get_request_id


logger = logging.getLogger(__name__)


class QueueService:
    """
    Async wrapper per Azure Storage Queue.
    """

    MAX_RETRIES = 3
    BASE_DELAY = 0.4

    def __init__(self):
        self.credential = DefaultAzureCredential()

        self.service = QueueServiceClient(
            account_url=settings.storage_account_url.replace(
                "blob.", "queue."
            ),
            credential=self.credential,
        )

        self.queue_name = settings.storage_queue_name

        self.queue = self.service.get_queue_client(self.queue_name)

    # =========================================================
    # Retry
    # =========================================================

    async def _retry(self, fn: Callable[[], Awaitable[Any]]) -> Any:
        for attempt in range(self.MAX_RETRIES):
            try:
                return await fn()

            except (ServiceRequestError, HttpResponseError) as e:
                if attempt == self.MAX_RETRIES - 1:
                    raise

                delay = self.BASE_DELAY * (2**attempt)

                track_event(
                    "queue_retry",
                    request_id=get_request_id(),
                    properties={
                        "attempt": attempt + 1,
                        "delay": delay,
                        "error": type(e).__name__,
                    },
                )

                await asyncio.sleep(delay)

    # =========================================================
    # Public API
    # =========================================================

    async def send_json(self, payload: dict):
        """
        Invia messaggio JSON serializzato.
        """
        message = json.dumps(payload)

        async def _op():
            await self.queue.send_message(message)

        await self._retry(_op)

        track_event(
            "queue_message_sent",
            request_id=get_request_id(),
        )

    async def close(self):
        await self.service.close()
        await self.credential.close()

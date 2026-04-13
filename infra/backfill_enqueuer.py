from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob.aio import BlobServiceClient
from azure.storage.queue.aio import QueueClient


class BackfillEnqueuer:
    """
    One-shot helper for Phase 0.5.

    Lists existing blobs from incoming container and enqueues
    processing messages with the same contract used by live trigger flow.
    """

    def __init__(
        self,
        connection_string: str,
        container_name: str,
        queue_name: str,
    ):
        self._connection_string = connection_string
        self._container_name = container_name
        self._queue_name = queue_name

    async def enqueue_existing(
        self,
        *,
        prefix: str | None,
        max_items: int,
        dry_run: bool,
        only_pdf: bool,
    ) -> dict[str, Any]:
        blob_service = BlobServiceClient.from_connection_string(self._connection_string)
        container_client = blob_service.get_container_client(self._container_name)
        queue_client = QueueClient.from_connection_string(
            conn_str=self._connection_string,
            queue_name=self._queue_name,
        )

        scanned = 0
        selected = 0
        skipped_non_pdf = 0

        try:
            if not dry_run:
                try:
                    await queue_client.create_queue()
                except ResourceExistsError:
                    pass

            async for blob in container_client.list_blobs(name_starts_with=prefix or None):
                scanned += 1
                blob_name = blob.name
                filename = blob_name.split('/')[-1]

                if only_pdf and not filename.lower().endswith('.pdf'):
                    skipped_non_pdf += 1
                    continue

                correlation_id = (
                    f"backfill-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-"
                    f"{uuid4().hex[:8]}"
                )
                message = {
                    'blob': f'{self._container_name}/{blob_name}',
                    'filename': filename,
                    'source_path': f'/{self._container_name}/{blob_name}',
                    'last_modified': (
                        blob.last_modified.astimezone(timezone.utc).isoformat()
                        if getattr(blob, 'last_modified', None)
                        else datetime.now(timezone.utc).isoformat()
                    ),
                    'correlation_id': correlation_id,
                }

                if not dry_run:
                    await queue_client.send_message(json.dumps(message))

                selected += 1
                if selected >= max_items:
                    break

            return {
                'dry_run': dry_run,
                'container': self._container_name,
                'target_queue': self._queue_name,
                'prefix': prefix,
                'only_pdf': only_pdf,
                'max_items': max_items,
                'scanned': scanned,
                'selected': selected,
                'skipped_non_pdf': skipped_non_pdf,
            }
        finally:
            await queue_client.close()
            await blob_service.close()

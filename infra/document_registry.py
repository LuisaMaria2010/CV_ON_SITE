"""
Document registry for ingestion idempotency and versioning.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableClient, TableServiceClient

from core.config import settings


logger = logging.getLogger(__name__)


class DocumentRegistry:
    STATUS_PROCESSING = "processing"
    STATUS_PROCESSED = "processed"
    STATUS_FAILED = "failed"

    def __init__(self, connection_string: str, table_name: str):
        self.connection_string = connection_string
        self.table_name = table_name
        self.table_client = TableClient.from_connection_string(
            conn_str=connection_string,
            table_name=table_name,
        )
        self.ensure_table_exists()

    @classmethod
    def from_settings(cls) -> "DocumentRegistry":
        connection_name = settings.document_registry_connection_name
        connection_string = os.getenv(connection_name)
        if not connection_string:
            raise ValueError(f"Missing {connection_name} configuration")
        return cls(
            connection_string=connection_string,
            table_name=settings.document_registry_table_name,
        )

    def ensure_table_exists(self) -> None:
        try:
            service_client = TableServiceClient.from_connection_string(
                conn_str=self.connection_string,
            )
            service_client.create_table_if_not_exists(table_name=self.table_name)
        except ResourceExistsError:
            pass

    def _normalize_document_id(self, document_id: str) -> str:
        normalized = (document_id or "").strip().lower()
        return normalized.replace(" ", "-")

    def _partition_key_from_source_path(self, source_path: str) -> str:
        normalized = (source_path or "").strip().lower().replace("/", "_")
        return normalized[:128] or "default"

    def lookup(self, source_path: str) -> dict[str, Any] | None:
        escaped_source_path = source_path.replace("'", "''")
        query_filter = f"source_path eq '{escaped_source_path}'"
        entities = list(self.table_client.query_entities(query_filter=query_filter))
        if not entities:
            return None
        return dict(entities[0])

    def find_by_hash(self, content_hash: str) -> dict[str, Any] | None:
        """Return an entity that has the given hash (if any)."""
        if not content_hash:
            return None
        escaped = content_hash.replace("'", "''")
        query_filter = f"hash eq '{escaped}'"
        entities = list(self.table_client.query_entities(query_filter=query_filter))
        if not entities:
            return None
        return dict(entities[0])

    def find_by_document_id(self, document_id: str) -> dict[str, Any] | None:
        """Return an entity matching the normalized document_id (RowKey) if present."""
        if not document_id:
            return None
        normalized_document_id = self._normalize_document_id(document_id)
        # Query by RowKey equality across partitions
        escaped = normalized_document_id.replace("'", "''")
        query_filter = f"RowKey eq '{escaped}'"
        entities = list(self.table_client.query_entities(query_filter=query_filter))
        if not entities:
            return None
        return dict(entities[0])

    def register(self, document_id: str, source_path: str, content_hash: str) -> dict[str, Any]:
        normalized_document_id = self._normalize_document_id(document_id)
        partition_key = self._partition_key_from_source_path(source_path)
        now = datetime.now(timezone.utc).isoformat()

        try:
            entity = self.table_client.get_entity(
                partition_key=partition_key,
                row_key=normalized_document_id,
            )
        except ResourceNotFoundError:
            entity = None

        if entity:
            existing_hash = entity.get("hash")
            if existing_hash != content_hash:
                entity["version"] = entity.get("version", 1) + 1
                entity["hash"] = content_hash
            entity["status"] = self.STATUS_PROCESSING
            entity["processed_at"] = now
        else:
            entity = {
                "PartitionKey": partition_key,
                "RowKey": normalized_document_id,
                "document_id": normalized_document_id,
                "source_path": source_path,
                "hash": content_hash,
                "version": 1,
                "status": self.STATUS_PROCESSING,
                "processed_at": now,
            }

        self.table_client.upsert_entity(entity=entity, mode="merge")
        return dict(entity)

    def mark_status(self, document_id: str, source_path: str, status: str) -> bool:
        normalized_document_id = self._normalize_document_id(document_id)
        partition_key = self._partition_key_from_source_path(source_path)
        try:
            entity = self.table_client.get_entity(
                partition_key=partition_key,
                row_key=normalized_document_id,
            )
            entity["status"] = status
            entity["processed_at"] = datetime.now(timezone.utc).isoformat()
            self.table_client.upsert_entity(entity=entity, mode="merge")
            return True
        except Exception:
            logger.exception(
                "Unable to update document status document_id=%s source_path=%s",
                normalized_document_id,
                source_path,
            )
            return False

    def should_process(self, source_path: str, content_hash: str, document_id: str | None = None) -> tuple[bool, int | None]:
        # 1) If any record already has the same hash, reuse it (no-op)
        by_hash = self.find_by_hash(content_hash)
        if by_hash:
            # ensure the source_path is recorded as an alias
            try:
                srcs = set((by_hash.get("source_paths") or by_hash.get("source_path") or "").split(";"))
                srcs.add((source_path or "") )
                by_hash["source_paths"] = ";".join(x for x in srcs if x)
                # merge back the source_paths and processed_at
                by_hash["processed_at"] = datetime.now(timezone.utc).isoformat()
                self.table_client.upsert_entity(entity=by_hash, mode="merge")
            except Exception:
                logger.exception("Unable to add source_path alias for existing hash")
            existing_version = by_hash.get("version", 1)
            return False, existing_version

        # 2) If no hash match, check if we have an entity for this exact source_path
        existing = self.lookup(source_path)
        if existing:
            existing_hash = existing.get("hash")
            existing_version = existing.get("version", 1)
            if existing_hash == content_hash:
                return False, existing_version
            # hash changed for same source_path -> version up
            return True, existing_version + 1

        # 3) Not found by source_path: if caller provided a document_id, check RowKey
        if document_id:
            by_docid = self.find_by_document_id(document_id)
            if by_docid:
                existing_hash = by_docid.get("hash")
                existing_version = by_docid.get("version", 1)
                if existing_hash == content_hash:
                    # shouldn't happen since hash check ran earlier, but be safe
                    return False, existing_version
                return True, existing_version + 1

        # 4) Otherwise, treat as new
        return True, None
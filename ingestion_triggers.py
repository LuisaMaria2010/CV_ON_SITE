import base64
import json
import logging
import os
from uuid import uuid4

import azure.functions as func
from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobServiceClient

from core.config import settings
from core.errors import InvalidInputError
from infra.document_registry import DocumentRegistry
from services.document_processor import DocumentProcessor

bp = func.Blueprint()
logger = logging.getLogger(__name__)
document_registry = DocumentRegistry.from_settings()
document_processor = DocumentProcessor()


def _download_blob_sync(container_name: str, blob_name: str) -> bytes:
    conn = os.environ["AzureWebJobsStorage"]
    client = BlobServiceClient.from_connection_string(conn)
    blob_client = client.get_blob_client(container=container_name, blob=blob_name)
    return blob_client.download_blob().readall()


def _upload_markdown_sync(container_name: str, blob_name: str, markdown: str, metadata: dict[str, str]) -> None:
    conn = os.environ["AzureWebJobsStorage"]
    client = BlobServiceClient.from_connection_string(conn)
    container_client = client.get_container_client(container_name)
    try:
        container_client.create_container()
    except ResourceExistsError:
        pass

    blob_client = container_client.get_blob_client(blob_name)
    blob_client.upload_blob(
        markdown.encode("utf-8"),
        overwrite=True,
        content_type="text/markdown; charset=utf-8",
        metadata=metadata,
    )


def _split_blob_path(blob_path: str) -> tuple[str, str]:
    parts = [part for part in blob_path.split("/", 1) if part]
    if len(parts) != 2:
        raise InvalidInputError(f"Invalid blob path: {blob_path}")
    return parts[0], parts[1]


def _detect_mime_type(filename: str) -> str | None:
    lowered = filename.lower()
    if lowered.endswith(".pdf"):
        return "application/pdf"
    if lowered.endswith(".docx"):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if lowered.endswith(".txt"):
        return "text/plain"
    return None


def _markdown_blob_name(document_id: str, version: int) -> str:
    return f"{document_id}/v{version}.md"


@bp.queue_trigger(
    arg_name="msg",
    queue_name="%DOCUMENT_PROCESSING_QUEUE_NAME%",
    connection="AzureWebJobsStorage",
)
def process_incoming_cv(msg: func.QueueMessage):
    """
    Consume document-processing messages and update idempotency registry.
    """
    try:
        raw_body = msg.get_body() if hasattr(msg, "get_body") else msg
        if isinstance(raw_body, bytes):
            body_text = raw_body.decode("utf-8")
        elif isinstance(raw_body, str):
            body_text = raw_body
        else:
            body_text = str(raw_body)
        try:
            payload = json.loads(body_text)
        except Exception:
            decoded_text = base64.b64decode(body_text).decode("utf-8")
            payload = json.loads(decoded_text)
    except Exception as exc:
        logger.exception("Invalid document-processing message")
        raise InvalidInputError("Invalid queue message") from exc

    blob_path = payload.get("blob")
    filename = payload.get("filename")
    source_path = payload.get("source_path")
    correlation_id = payload.get("correlation_id") or f"queue-{uuid4().hex[:8]}"

    if not blob_path or not filename or not source_path:
        raise InvalidInputError("Queue message missing required fields")

    try:
        container_name, blob_name = _split_blob_path(blob_path)
        mime_type = _detect_mime_type(filename)

        logger.info(
            "Document processing started blob=%s correlation_id=%s",
            blob_path,
            correlation_id,
        )

        file_bytes = _download_blob_sync(container_name, blob_name)
        processing_result = document_processor.process(file_bytes, mime_type=mime_type)
        extracted_text = processing_result["extracted_text"]
        markdown = processing_result["markdown"]
        content_hash = processing_result["content_hash"]

        should_process, next_version = document_registry.should_process(source_path, content_hash)
        if not should_process:
            logger.info(
                "Document skipped source_path=%s version=%s correlation_id=%s",
                source_path,
                next_version,
                correlation_id,
            )
            return

        registry_record = document_registry.register(filename, source_path, content_hash)
        markdown_blob_name = _markdown_blob_name(
            registry_record.get("document_id") or filename,
            int(registry_record.get("version") or 1),
        )
        _upload_markdown_sync(
            settings.storage_container_normalized_markdown,
            markdown_blob_name,
            markdown,
            metadata={
                "document_id": str(registry_record.get("document_id") or ""),
                "source_path": source_path,
                "version": str(registry_record.get("version") or 1),
                "hash": content_hash,
            },
        )

        logger.info(
            "Document registered document_id=%s version=%s text_chars=%s markdown_blob=%s correlation_id=%s",
            registry_record.get("document_id"),
            registry_record.get("version"),
            len(extracted_text),
            markdown_blob_name,
            correlation_id,
        )

        document_registry.mark_status(
            filename,
            source_path,
            DocumentRegistry.STATUS_PROCESSED,
        )

        logger.info(
            "Document processing completed source_path=%s version=%s correlation_id=%s",
            source_path,
            registry_record.get("version"),
            correlation_id,
        )
    except Exception:
        document_registry.mark_status(
            filename,
            source_path,
            DocumentRegistry.STATUS_FAILED,
        )
        logger.exception(
            "Document processing failed source_path=%s correlation_id=%s",
            source_path,
            correlation_id,
        )
        raise

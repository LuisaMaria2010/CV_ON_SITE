import base64
import json
import logging
from uuid import uuid4

import azure.functions as func
from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobServiceClient

from core.config import settings
from core.errors import InvalidInputError
from infra.document_registry import DocumentRegistry
from services.document_processor import DocumentProcessor
from domain.normalizer import TextNormalizer
from infra.search_service import SearchService
import asyncio
from services.document_indexer import DocumentIndexer

bp = func.Blueprint()
logger = logging.getLogger(__name__)
document_registry: DocumentRegistry | None = None
document_processor = DocumentProcessor()


def _get_document_registry() -> DocumentRegistry:
    global document_registry
    if document_registry is None:
        document_registry = DocumentRegistry.from_settings()
    return document_registry


def _download_blob_sync(container_name: str, blob_name: str) -> bytes:
    conn = settings.storage_account_connection_string or settings.storage_connection_string
    if not conn:
        raise InvalidInputError("Missing storage connection string (AzureWebJobsStorage)")
    client = BlobServiceClient.from_connection_string(conn)
    blob_client = client.get_blob_client(container=container_name, blob=blob_name)
    return blob_client.download_blob().readall()


def _upload_markdown_sync(container_name: str, blob_name: str, markdown: str, metadata: dict[str, str]) -> None:
    conn = settings.storage_account_connection_string or settings.storage_connection_string
    if not conn:
        raise InvalidInputError("Missing storage connection string (AzureWebJobsStorage)")
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
        registry = _get_document_registry()
        container_name, blob_name = _split_blob_path(blob_path)
        mime_type = _detect_mime_type(filename)

        logger.info(
            "Document processing started blob=%s correlation_id=%s",
            blob_path,
            correlation_id,
        )

        file_bytes = _download_blob_sync(container_name, blob_name)
        processing_result = document_processor.process(
            file_bytes,
            mime_type=mime_type,
            filename=filename,
            source_path=source_path,
        )
        extracted_text = processing_result["extracted_text"]
        markdown = processing_result["markdown"]
        content_hash = processing_result["content_hash"]
        enriched_meta: dict = processing_result.get("metadata") or {}

        # compute normalized document id from filename
        document_id = TextNormalizer.normalize_document_id(filename)

        # LLM enrichment: populate skills, certifications, experience, etc. in front matter
        try:
            from core.llm_chain import CVExtractionChain
            from db_data.mapper import to_domain
            from db_data.postprocess import enrich as _enrich_cv
            _chain = CVExtractionChain()
            _raw = asyncio.run(_chain.extract(extracted_text))
            _cv_extraction = _enrich_cv(to_domain(_raw))
            markdown, enriched_meta = document_processor.apply_cv_extraction(
                markdown, processing_result.get("metadata") or {}, _cv_extraction
            )
            logger.info("LLM enrichment applied document_id=%s", document_id)
        except Exception:
            logger.exception(
                "LLM enrichment failed, proceeding with partial metadata document_id=%s", document_id
            )
        should_process, next_version = registry.should_process(
            source_path, content_hash, document_id=document_id
        )
        if not should_process:
            logger.info(
                "Document skipped source_path=%s version=%s correlation_id=%s",
                source_path,
                next_version,
                correlation_id,
            )
            return
        # Register using normalized document id
        registry_record = registry.register(document_id, source_path, content_hash)

        # If we are incrementing version for an existing document, delete old chunks
        try:
            if next_version is not None:
                try:
                    search = SearchService()
                    asyncio.run(search.delete_chunks(registry_record.get("document_id") or document_id))
                except Exception:
                    logger.exception("Failed to delete old chunks for document_id=%s", registry_record.get("document_id") or document_id)
        except Exception:
            logger.exception("Error while attempting pre-index cleanup")

        # Ensure metadata includes registry version and use processor metadata where available
        proc_meta = processing_result.get("metadata") or {}
        proc_meta["version"] = int(registry_record.get("version") or 1)

        markdown_blob_name = _markdown_blob_name(
            registry_record.get("document_id") or document_id or filename,
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

        # Index chunks with hybrid embeddings before marking as processed
        try:
            from core.schema import NormalizedCVMetadata
            from infra.llm_client import get_embedding_client
            indexer = DocumentIndexer()
            version = int(registry_record.get("version") or 1)
            try:
                chunk_meta = NormalizedCVMetadata(
                    **{
                        **enriched_meta,
                        "document_id": registry_record.get("document_id") or document_id,
                        "version": version,
                        "source_paths": [source_path],
                        "hash": content_hash,
                        "processed_at": enriched_meta.get("processed_at") or proc_meta.get("processed_at"),
                    }
                )

                # Build async embedding function using Azure OpenAI embeddings client
                _embed_client = get_embedding_client()

                async def _embedding_fn(text: str) -> list[float]:
                    return await _embed_client.aembed_query(text)

                docs = asyncio.run(indexer.index_async(markdown, chunk_meta, embedding_fn=_embedding_fn))
                logger.info(
                    "Indexed %s chunks (with embeddings) for document_id=%s version=%s",
                    len(docs), registry_record.get("document_id"), version,
                )
            except Exception:
                logger.exception("Indexing failed for document_id=%s version=%s", registry_record.get("document_id"), version)
        except Exception:
            logger.exception("Failed to initialize DocumentIndexer")

        registry.mark_status(
            registry_record.get("document_id") or document_id or filename,
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
        registry = _get_document_registry()
        registry.mark_status(
            registry_record.get("document_id") if 'registry_record' in locals() else filename,
            source_path,
            DocumentRegistry.STATUS_FAILED,
        )
        logger.exception(
            "Document processing failed source_path=%s correlation_id=%s",
            source_path,
            correlation_id,
        )
        raise

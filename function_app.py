import azure.functions as func
import logging
import os
import json
import re
from datetime import datetime, timezone
from uuid import uuid4

from core.config import settings
from core.errors import InvalidInputError, FileTooLargeError

from infra.blob_storage import StorageService
from infra.backfill_enqueuer import BackfillEnqueuer
from extraction.cache import TextCache
from db_data.pipeline import CVPipeline
from ingestion_triggers import bp as ingestion_bp

from utils.http_errors import http_error_handler

# Creazione dell'oggetto app principale
app = func.FunctionApp(
    http_auth_level=func.AuthLevel.FUNCTION
)
app.register_functions(ingestion_bp)

logger = logging.getLogger(__name__)

# Cold start dependency wiring
storage = StorageService()
cache = TextCache(storage)
pipeline = CVPipeline(cache)


def _build_processing_message(*, blob_name: str, last_modified: str | None = None) -> dict:
    filename = blob_name.split("/")[-1]
    return {
        "blob": f"{settings.storage_container_incoming}/{blob_name}",
        "filename": filename,
        "source_path": f"/{settings.storage_container_incoming}/{blob_name}",
        "last_modified": last_modified or datetime.now(timezone.utc).isoformat(),
        "correlation_id": f"blob-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8]}",
    }


def _parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    raise InvalidInputError(f"Invalid boolean value: {raw}")


def _parse_int(raw: str | None, default: int) -> int:
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise InvalidInputError(f"Invalid integer value: {raw}") from exc

    if value <= 0:
        raise InvalidInputError("max_items must be > 0")
    return value


def _body_params(req: func.HttpRequest) -> dict:
    body = req.get_body()
    if not body:
        return {}

    try:
        payload = json.loads(body.decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


@app.blob_trigger(
    arg_name="inputblob",
    path="incoming-cv/{name}",
    connection="AzureWebJobsStorage",
)
@app.queue_output(
    arg_name="queueoutput",
    queue_name="%DOCUMENT_PROCESSING_QUEUE_NAME%",
    connection="AzureWebJobsStorage",
)
def enqueue_incoming_cv_blob(inputblob: func.InputStream, queueoutput: func.Out[str]):
    """
    Trigger automatico per nuovi blob caricati in incoming-cv.

    Responsabilita':
    - ascolta upload sul container incoming-cv
    - costruisce il messaggio standard di processing
    - accoda su document-processing

    Non esegue parsing o estrazione CV.
    """
    blob_name = inputblob.name.split("/", 1)[-1]
    properties = getattr(inputblob, "properties", None)
    last_modified = None
    if properties is not None:
        last_modified_value = getattr(properties, "last_modified", None)
        if last_modified_value is not None:
            last_modified = last_modified_value.astimezone(timezone.utc).isoformat()

    message = _build_processing_message(
        blob_name=blob_name,
        last_modified=last_modified,
    )
    queueoutput.set(json.dumps(message))

    logger.info(
        "Incoming CV blob enqueued blob=%s queue=%s correlation_id=%s",
        message["blob"],
        settings.document_processing_queue_name,
        message["correlation_id"],
    )

# =========================================================
# HTTP Function: Extract CV
# =========================================================

@app.route(route="extract", methods=["POST"])
@http_error_handler
async def extract(req: func.HttpRequest):
    """
    POST /api/extract

    Input supportati:
    - raw bytes (PDF / DOCX / TXT)
    - multipart/form-data con campo "file"

    Output:
    - dict dominio CVExtraction
    """
    
    # Recupero body (raw o multipart)
    file_bytes = None
    upload_filename = None
    content_type = req.headers.get("content-type", "").lower()
    
    if "multipart/form-data" in content_type:
        
        files = req.files
        if not files or "file" not in files:
            raise InvalidInputError("Missing 'file' field in multipart request")
        
        uploaded_file = files["file"]
        upload_filename = uploaded_file.filename
        file_bytes = uploaded_file.read()
    else:
        # Raw bytes
        file_bytes = req.get_body()

    if not file_bytes:
        raise InvalidInputError("Empty file")

    if not upload_filename:
        upload_filename = f"upload-{uuid4().hex}.bin"

    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", upload_filename)
    blob_name = f"{datetime.now(timezone.utc).strftime('%Y/%m/%d')}/{uuid4().hex}_{safe_name}"

    await storage.upload_bytes(
        data=file_bytes,
        blob_name=blob_name,
        container=settings.storage_container_original_uploads,
    )

    logger.info(
        "Original CV upload stored container=%s blob=%s",
        settings.storage_container_original_uploads,
        blob_name,
    )
    
    # Validazione dimensione
    max_size_bytes = settings.max_file_size_mb * 1024 * 1024
    if len(file_bytes) > max_size_bytes:
        raise FileTooLargeError(
            f"File too large: {len(file_bytes)} bytes. Max: {max_size_bytes}"
        )
    
    # Pipeline dominio (parsing + LLM)
    extraction = await pipeline.process(file_bytes)

    # Rimuoviamo temporaneamente i dati sensibili dall'output pubblico
    response_payload = extraction.model_dump()
    for field in ("email", "phone", "age"):
        response_payload.pop(field, None)
    
    # Ritorniamo dict puro (decoratore gestisce envelope)
    return response_payload


@app.route(route="backfill/incoming-cv", methods=["POST"])
@http_error_handler
async def backfill_incoming_cv(req: func.HttpRequest):
    """
    POST /api/backfill/incoming-cv

    Enqueue dei blob gia' presenti in incoming-cv verso document-processing.
    Parametri (query o JSON body):
    - dry_run: true/false (default true)
    - prefix: prefisso opzionale blob
    - max_items: limite enqueue (default 100)
    - only_pdf: true/false (default true)
    """

    payload = _body_params(req)

    def get_value(key: str):
        query_value = req.params.get(key)
        return query_value if query_value is not None else payload.get(key)

    dry_run = _parse_bool(get_value("dry_run"), default=True)
    only_pdf = _parse_bool(get_value("only_pdf"), default=True)
    max_items = _parse_int(get_value("max_items"), default=100)
    prefix = get_value("prefix")
    if isinstance(prefix, str):
        prefix = prefix.strip() or None
    else:
        prefix = None

    connection_string = os.getenv("AzureWebJobsStorage")
    if not connection_string:
        raise InvalidInputError("Missing AzureWebJobsStorage configuration")

    enqueuer = BackfillEnqueuer(
        connection_string=connection_string,
        container_name=settings.storage_container_incoming,
        queue_name=settings.document_processing_queue_name,
    )

    result = await enqueuer.enqueue_existing(
        prefix=prefix,
        max_items=max_items,
        dry_run=dry_run,
        only_pdf=only_pdf,
    )

    logger.info(
        "Backfill completed dry_run=%s selected=%s scanned=%s queue=%s",
        result["dry_run"],
        result["selected"],
        result["scanned"],
        result["target_queue"],
    )

    return result



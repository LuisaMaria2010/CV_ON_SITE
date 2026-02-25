"""
Azure Function HTTP Entrypoint - FlashCV

Responsabilità:
- validazione minima della request
- invocazione pipeline di dominio
- nessuna logica business
- nessuna gestione errori diretta

Error handling, envelope JSON, request_id, logging e metriche
sono gestiti interamente dal decorator @http_error_handler.
"""

from __future__ import annotations

import logging
import azure.functions as func

from core.config import settings
from core.errors import InvalidInputError, FileTooLargeError

from infra.blob_storage import StorageService
from extraction.cache import TextCache
from db_data.pipeline import CVPipeline

from utils.http_errors import http_error_handler
from app_instance import app

logger = logging.getLogger(__name__)

# =========================================================
# Cold start dependency wiring
# (eseguito una sola volta per worker)
# =========================================================

storage = StorageService()
cache = TextCache(storage)
pipeline = CVPipeline(cache)

# =========================================================
# Endpoint
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
      (verrà automaticamente wrappato in envelope JSON)
    """

    # -------------------------------------------------
    # Recupero body (raw o multipart)
    # -------------------------------------------------

    content_type = req.headers.get("content-type", "")

    if "multipart/form-data" in content_type:
        file = req.files.get("file")
        if not file:
            raise InvalidInputError("Missing file field in multipart request")
        file_bytes = file.stream.read()
    else:
        file_bytes = req.get_body()

    # -------------------------------------------------
    # Validazioni minime
    # -------------------------------------------------

    if not file_bytes:
        raise InvalidInputError("Empty request body")

    if len(file_bytes) > settings.max_file_size_mb * 1024 * 1024:
        raise FileTooLargeError(
            f"File too large (max {settings.max_file_size_mb} MB)"
        )

    mime = req.headers.get("content-type")

    # -------------------------------------------------
    # Business pipeline
    # -------------------------------------------------

    result = await pipeline.process(
        file_bytes=file_bytes,
        mime=mime,
    )

    # IMPORTANTISSIMO:
    # ritorniamo SOLO il modello/dict
    # il decorator si occupa di:
    # - envelope JSON
    # - status code
    # - request_id
    # - error mapping
    return (
        result.model_dump()
        if hasattr(result, "model_dump")
        else result
    )
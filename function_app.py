import azure.functions as func
import logging

from core.config import settings
from core.errors import InvalidInputError, FileTooLargeError

from infra.blob_storage import StorageService
from extraction.cache import TextCache
from db_data.pipeline import CVPipeline

from utils.http_errors import http_error_handler

# Creazione dell'oggetto app principale
app = func.FunctionApp(
    http_auth_level=func.AuthLevel.FUNCTION
)

logger = logging.getLogger(__name__)

# Cold start dependency wiring
storage = StorageService()
cache = TextCache(storage)
pipeline = CVPipeline(cache)

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
    content_type = req.headers.get("content-type", "").lower()
    
    if "multipart/form-data" in content_type:
        
        files = req.files
        if not files or "file" not in files:
            raise InvalidInputError("Missing 'file' field in multipart request")
        
        uploaded_file = files["file"]
        file_bytes = uploaded_file.read()
    else:
        # Raw bytes
        file_bytes = req.get_body()
    
    if not file_bytes:
        raise InvalidInputError("Empty file")
    
    # Validazione dimensione
    max_size_bytes = settings.max_file_size_mb * 1024 * 1024
    if len(file_bytes) > max_size_bytes:
        raise FileTooLargeError(
            f"File too large: {len(file_bytes)} bytes. Max: {max_size_bytes}"
        )
    
    # Pipeline dominio (parsing + LLM)
    extraction = await pipeline.process(file_bytes)
    
    # Ritorniamo dict puro (decoratore gestisce envelope)
    return extraction.model_dump()



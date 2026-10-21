"""Estrazione sincrona di un CV: POST /api/extract.

Estratto da function_app.py senza modifiche ai corpi delle funzioni.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from uuid import uuid4

import azure.functions as func

from core.config import settings
from core.dependencies import pipeline, storage
from core.errors import FileTooLargeError, InvalidInputError
from db_data.pipeline import PROFILE_DEFAULT, PROFILE_MCFLASH, PROFILES
from domain.interview_sheet import merge_sheet, parse_sheet
from utils.http_errors import http_error_handler
from utils.http_params import body_params

logger = logging.getLogger(__name__)

bp = func.Blueprint()

# Dati personali: fuori dalla risposta, salvo profilo mcflash o include_contacts=true.
_CONTACT_FIELDS = ("email", "phone", "age")
# Campi che esistono solo nel profilo mcflash: nel profilo base la risposta resta quella di prima.
_MCFLASH_ONLY_FIELDS = (
    "first_name", "last_name", "catalog_role", "experiences", "education", "trainings", "languages",
    "cv_skills", "trittico",
)


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "si", "sì")

@bp.route(route="extract", methods=["POST"])
@http_error_handler
async def extract(req: func.HttpRequest):
    """
    POST /api/extract

    Input supportati:
    - raw bytes (PDF / DOCX / TXT)
    - multipart/form-data con campo "file"

    Query:
    - profile=default|mcflash (default "default"). "mcflash" aggiunge nome e
      cognome separati, trittico, esperienze complete, formazione con anno e
      istituto, lingue con livello, e include email/telefono/eta'.
    - include_contacts=true: email/telefono/eta' anche nel profilo base.

    Output:
    - dict dominio CVExtraction
    """
    profile = (req.params.get("profile") or PROFILE_DEFAULT).strip().lower()
    if profile not in PROFILES:
        raise InvalidInputError(f"Unknown profile '{profile}'. Allowed: {', '.join(PROFILES)}")
    include_contacts = profile == PROFILE_MCFLASH or _flag(req.params.get("include_contacts"))

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
    extraction = await pipeline.process(file_bytes, profile=profile)

    response_payload = extraction.model_dump()
    if not include_contacts:
        for field in _CONTACT_FIELDS:
            response_payload.pop(field, None)
    if profile != PROFILE_MCFLASH:
        for field in _MCFLASH_ONLY_FIELDS:
            response_payload.pop(field, None)
    else:
        # La dashboard lega la bozza al file originale: serve per rivederlo e,
        # alla conferma, per registrarne il percorso.
        response_payload["original_blob"] = blob_name
        response_payload["original_filename"] = upload_filename

    # Ritorniamo dict puro (decoratore gestisce envelope)
    return response_payload


_MAX_SHEET_CHARS = 60_000


@bp.route(route="extract/scheda", methods=["POST"])
@http_error_handler
async def extract_sheet(req: func.HttpRequest):
    """
    POST /api/extract/scheda

    Body JSON: {testo, base?}. `testo` e' la scheda colloquio del recruiter
    (domain/interview_sheet.py); `base` i dati gia' estratti dal CV, se c'e'.
    La scheda passa anche dall'LLM del profilo mcflash quando manca il CV,
    quando il CV non ha esperienze ne' formazione, o quando la scheda descrive
    esperienze o titoli di studio: il form e' un misto delle due fonti
    (domain/interview_sheet.merge_sheet), qualunque sia la piu' ricca.
    Nulla viene salvato: ne' file nello storage, ne' indice.

    Output: i dati del form (come /api/extract?profile=mcflash) con in piu'
    mcflash (tariffa, seniority, disponibilita') e colloquio (note del colloquio).
    """
    body = body_params(req)
    text = str(body.get("testo") or "").strip()
    if not text:
        raise InvalidInputError("'testo' (la scheda colloquio) e' obbligatorio")
    if len(text) > _MAX_SHEET_CHARS:
        raise InvalidInputError(f"Scheda troppo lunga: massimo {_MAX_SHEET_CHARS} caratteri")
    base = body.get("base") if isinstance(body.get("base"), dict) else None
    sheet = parse_sheet(text)
    from_text = None
    narrative = any((sheet.get("colloquio") or {}).get(k) for k in ("esperienza", "titolo_studio"))
    if base is None or narrative or not (base.get("experiences") or base.get("education")):
        extraction = await pipeline.process(text.encode("utf-8"), mime="text/plain", profile=PROFILE_MCFLASH)
        from_text = extraction.model_dump()
    return merge_sheet(base, sheet, from_text)

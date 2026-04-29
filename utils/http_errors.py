# =============================================================================
# http_errors.py
# =============================================================================
"""
Gestione centralizzata degli errori HTTP per Azure Functions.

Questo modulo fornisce decoratori e helper per:
- intercettare eccezioni di dominio (core.errors)
- mapparle in risposte HTTP coerenti (envelope JSON standard)
- propagare e generare request_id/correlation id
- inviare metriche/tracce ad Application Insights

Formato risposta standard:
{
    "data": ...,
    "error": ...,
    "request_id": ...
}

Nota:
    Questo modulo NON deve essere importato dal core, ma solo dal layer HTTP/adapters.
"""

from __future__ import annotations

import logging
import uuid
from functools import wraps

import azure.functions as func

from core.errors import (
    CVError,
    InvalidInputError,
    FileTooLargeError,
    TextExtractionError,
    LLMProcessingError,
)

from utils.http_response import json_response
from utils.observability import track_event, track_duration
from utils.request_context import set_request_id


logger = logging.getLogger(__name__)


# =========================================================
# Helpers
# =========================================================

def _get_request_id(req: func.HttpRequest) -> str:
    """
    Estrae un identificativo di richiesta dagli header HTTP oppure ne genera uno nuovo.

    Cerca negli header 'x-request-id' o 'x-correlation-id'.
    Se non presenti, genera un nuovo UUID.

    Args:
        req (func.HttpRequest): La richiesta HTTP in ingresso.

    Returns:
        str: L'identificativo della richiesta.
    """
    return (
        req.headers.get("x-request-id")
        or req.headers.get("x-correlation-id")
        or str(uuid.uuid4())
    )


# =========================================================
# ⭐ FUNZIONE UNICA DI MAPPING ERRORI
# =========================================================

def map_exception_to_response(
    exc: Exception,
    request_id: str,
) -> func.HttpResponse:
    """
    Converte qualsiasi eccezione in una risposta HTTP envelope standard.

    Punto unico di verità per il mapping errori → HTTP.
    Mappa errori noti su codici di stato e messaggi specifici, altrimenti restituisce errore generico 500.
    
    Args:
        exc (Exception): L'eccezione sollevata.
        request_id (str): Identificativo della richiesta.

    Returns:
        func.HttpResponse: Risposta HTTP envelope con errore.
    """
    # -----------------------------------------------------
    # Errori di dominio noti
    # -----------------------------------------------------

    if isinstance(exc, FileTooLargeError):
        logger.warning("[%s] %s", request_id, exc)
        return json_response(
            data=None,
            error=str(exc),
            request_id=request_id,
            status_code=413,
        )

    if isinstance(exc, InvalidInputError):
        logger.warning("[%s] %s", request_id, exc)
        return json_response(
            data=None,
            error=str(exc),
            request_id=request_id,
            status_code=400,
        )

    if isinstance(exc, TextExtractionError):
        return json_response(
            data=None,
            error="Cannot extract text from CV",
            request_id=request_id,
            status_code=422,
        )

    if isinstance(exc, LLMProcessingError):
        return json_response(
            data=None,
            error="LLM processing failed",
            request_id=request_id,
            status_code=502,
        )

    logger.exception("[%s] Unexpected error", request_id)

    # During local debugging return the exception message to aid diagnosis.
    # NOTE: this may expose internal details; revert before production.
    return json_response(
        data=None,
        error=str(exc),
        request_id=request_id,
        status_code=500,
    )



# =========================================================
# Decorator principale
# =========================================================

def http_error_handler(fn):
    """
    Decoratore principale per handler HTTP Azure Function.

    Automatizza:
    - generazione/propagazione request_id
    - tracciamento eventi e metriche (Application Insights)
    - misurazione durata request
    - envelope JSON per successi ed errori
    - mapping centralizzato delle eccezioni

    Args:
        fn (Callable): Handler asincrono della richiesta HTTP.

    Returns:
        Callable: Wrapper che gestisce errori, metriche e risposta envelope.
    """
    @wraps(fn)
    async def wrapper(req: func.HttpRequest, *args, **kwargs):
        """
        Wrapper che gestisce la richiesta HTTP:
        - Estrae e propaga il request_id
        - Traccia eventi di inizio, successo, errore
        - Misura la durata della richiesta
        - Applica envelope JSON standard
        - Mappa eccezioni in risposte coerenti

        Args:
            req (func.HttpRequest): La richiesta HTTP in ingresso.
            *args, **kwargs: Argomenti aggiuntivi per l'handler.

        Returns:
            func.HttpResponse: Risposta HTTP envelope.
        """
        request_id = _get_request_id(req)
        set_request_id(request_id)
        track_event(
            "http_request_start",
            request_id=request_id,
            properties={
                "path": req.url,
                "method": req.method,
            },
        )
        try:
            with track_duration("http_request_ms", request_id=request_id):
                result = await fn(req, *args, **kwargs)
            # -------------------------------------------------
            # SUCCESS
            # -------------------------------------------------
            track_event(
                "http_request_success",
                request_id=request_id,
            )
            if isinstance(result, func.HttpResponse):
                result.headers["x-request-id"] = request_id
                return result
            response = json_response(
                data=result,
                error=None,
                request_id=request_id,
                status_code=200,
            )
            response.headers["x-request-id"] = request_id
            return response
        # -------------------------------------------------
        # ERROR
        # -------------------------------------------------
        except Exception as exc:
            response = map_exception_to_response(exc, request_id)
            track_event(
                "http_request_error",
                request_id=request_id,
                properties={
                    "error_type": type(exc).__name__,
                    "status_code": response.status_code,
                },
            )
            response.headers["x-request-id"] = request_id
            return response
    return wrapper
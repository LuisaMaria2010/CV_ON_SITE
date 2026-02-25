"""
Utility per la creazione di risposte HTTP envelope JSON standard per API Azure Functions.

Fornisce:
- Funzione json_response per serializzare dati, errori e request_id in formato coerente.
- Gestione automatica header Content-Type.
"""
import json
from typing import Any, Optional, Dict

import azure.functions as func


# =========================================================
# JSON envelope response
# =========================================================

def json_response(
    *,
    data: Optional[Any],
    error: Optional[str],
    request_id: str,
    status_code: int,
    headers: Optional[Dict[str, str]] = None,
) -> func.HttpResponse:
    """
    Costruisce una risposta HTTP envelope JSON standard per API.

    Args:
        data (Any | None): Payload dati serializzabile.
        error (str | None): Messaggio di errore, se presente.
        request_id (str): Identificativo univoco della richiesta.
        status_code (int): Codice di stato HTTP.
        headers (dict | None): Header aggiuntivi opzionali.

    Returns:
        func.HttpResponse: Risposta HTTP serializzata in JSON.

    Note:
        - NON gestisce l'header request-id (compito del decorator)
        - Serializzazione sicura (default=str per oggetti non serializzabili)
    """

    body = {
        "success": error is None,
        "data": data,
        "error": error,
        "request_id": request_id,
    }

    response_headers = {
        "Content-Type": "application/json; charset=utf-8",
    }

    if headers:
        response_headers.update(headers)

    return func.HttpResponse(
        body=json.dumps(body, ensure_ascii=False, default=str),
        status_code=status_code,
        headers=response_headers,
    )

"""
Helper per osservabilità e telemetria (Application Insights via logging).

Responsabilità:
- tracciare eventi custom
- tracciare metriche numeriche
- misurare durate (ms)
- correlare tutto tramite request_id

Nota:
Questo modulo NON deve mai sollevare eccezioni.
"""

import logging
import time
from contextlib import contextmanager
from typing import Optional, Dict, Any


logger = logging.getLogger(__name__)


# =========================================================
# Events
# =========================================================

def track_event(
    name: str,
    *,
    request_id: str,
    properties: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Traccia un evento custom per Application Insights o logging strutturato.

    Args:
        name (str): Nome dell'evento.
        request_id (str): Identificativo di correlazione della richiesta.
        properties (dict, opzionale): Proprietà aggiuntive da allegare all'evento.

    Note:
        Non solleva mai eccezioni.
    """
    try:
        props = dict(properties) if properties else {}
        props["request_id"] = request_id

        logger.info(
            "EVENT::%s | %s",
            name,
            props,
        )
    except Exception:
        # observability non deve mai rompere il flusso
        pass


# =========================================================
# Metrics
# =========================================================

def track_metric(
    name: str,
    value: float,
    *,
    request_id: str,
    properties: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Traccia una metrica numerica custom.

    Args:
        name (str): Nome della metrica.
        value (float): Valore numerico della metrica.
        request_id (str): Identificativo di correlazione della richiesta.
        properties (dict, opzionale): Proprietà aggiuntive.

    Note:
        Non solleva mai eccezioni.
    """
    try:
        props = dict(properties) if properties else {}
        props["request_id"] = request_id

        logger.info(
            "METRIC::%s=%s | %s",
            name,
            value,
            props,
        )
    except Exception:
        pass


# =========================================================
# Duration
# =========================================================

@contextmanager
def track_duration(
    name: str,
    *,
    request_id: str,
    properties: Optional[Dict[str, Any]] = None,
):
    """
    Context manager per misurare e tracciare la durata (in ms) di un blocco di codice.

    Args:
        name (str): Nome della metrica temporale.
        request_id (str): Identificativo di correlazione della richiesta.
        properties (dict, opzionale): Proprietà aggiuntive.

    Esempio:
        with track_duration("llm_processing_ms", request_id=req_id):
            ...
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        track_metric(
            name,
            duration_ms,
            request_id=request_id,
            properties=properties,
        )

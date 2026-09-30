"""Tabella CV -> MCFlash: document_id dell'indice CV -> id del record MCFlash.

E' l'unico collegamento tra un CV e una persona MCFlash usato dal pipeline di
ricerca: il codice anonimizzato nel CV (es. "DCA") non e' univoco e non basta.
Un CV che non e' in tabella e' fuori perimetro e non viene mai restituito.

La tabella vive su Azure Table storage (stesso account della Function):
- tabella: CV_MCFLASH_MAP_TABLE_NAME (default "CvMcflashMap")
- PartitionKey "cv", RowKey = document_id
- campi: id_mcflash, file, codice, criterio, updated_at

Si popola con scripts/upload_cv_mcflash_map.py. Viene letta per intero e
tenuta in memoria per CV_MCFLASH_MAP_TTL_SECONDS (default 300): una modifica
alla tabella ha effetto entro quel tempo, senza deploy.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from core.config import settings
from utils.app_settings import settings_value

try:
    from azure.data.tables import TableClient
except ImportError:  # pragma: no cover - dipende dall'ambiente di deploy
    TableClient = None

logger = logging.getLogger(__name__)

PARTITION_KEY = "cv"

_lock = threading.Lock()
_cache: dict[str, str] | None = None
_loaded_at = 0.0


def cv_mcflash_map_table_name() -> str:
    return settings_value("CV_MCFLASH_MAP_TABLE_NAME", default="CvMcflashMap")


def _connection_string() -> str:
    return settings_value(
        "AzureWebJobsStorage",
        "STORAGE_ACCOUNT_CONNECTION_STRING",
        "STORAGE_CONNECTION_STRING",
        default=settings.storage_connection_string or "",
    )


def _ttl_seconds() -> float:
    try:
        return float(settings_value("CV_MCFLASH_MAP_TTL_SECONDS", default="300"))
    except ValueError:
        return 300.0


def get_table_client() -> Any:
    if TableClient is None:
        raise RuntimeError("CV->MCFlash map unavailable: install azure-data-tables")
    connection_string = _connection_string()
    if not connection_string:
        raise RuntimeError("CV->MCFlash map unavailable: missing storage connection string")
    # Timeout e retry espliciti: con i default dell'SDK una connessione appesa
    # tiene fermo il lock (e quindi ogni ricerca) per minuti.
    return TableClient.from_connection_string(
        connection_string,
        table_name=cv_mcflash_map_table_name(),
        connection_timeout=10,
        read_timeout=30,
        retry_total=2,
    )


def _read_table() -> dict[str, str]:
    client = get_table_client()
    mapping: dict[str, str] = {}
    for entity in client.query_entities(
        f"PartitionKey eq '{PARTITION_KEY}'", select=["RowKey", "id_mcflash"]
    ):
        doc_id, mc_id = entity.get("RowKey"), entity.get("id_mcflash")
        if doc_id and mc_id not in (None, ""):
            mapping[str(doc_id)] = str(mc_id)
    return mapping


def get_cv_mcflash_map() -> dict[str, str]:
    """document_id -> id MCFlash (come stringa), dalla tabella su storage.

    Se la lettura fallisce si usa l'ultima copia valida; senza copia valida
    l'errore viene propagato: meglio una ricerca in errore che una ricerca che
    restituisce zero candidati senza dirlo.
    """
    global _cache, _loaded_at
    with _lock:
        if _cache is not None and time.monotonic() - _loaded_at < _ttl_seconds():
            return _cache
        try:
            t0 = time.monotonic()
            _cache = _read_table()
            _loaded_at = time.monotonic()
            logger.info("CV->MCFlash map loaded rows=%d ms=%.0f", len(_cache), (_loaded_at - t0) * 1000)
            if not _cache:
                logger.error("CV->MCFlash map table %s is empty: every CV is out of perimeter",
                             cv_mcflash_map_table_name())
        except Exception:
            if _cache is None:
                raise
            logger.exception("CV->MCFlash map refresh failed; using the last loaded copy")
        return _cache


def clear_cache() -> None:
    global _cache, _loaded_at
    with _lock:
        _cache, _loaded_at = None, 0.0

"""
Queue consumer worker.

Responsabilità:
- riceve JSON dalla queue
- converte in CVExtraction
- salva su DB via repository
- gestisce pool/connessioni
- repository resta puro SQL

ZERO Azure logic qui.
Solo dominio e persistenza.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from core.schema import CVExtraction
from utils.observability import track_event
from utils.request_context import get_request_id

from .repository import CandidateRepository
from .db import acquire_conn


logger = logging.getLogger(__name__)


# =========================================================
# Public entrypoint
# =========================================================

async def handle_message(
    body: bytes,
    *,
    repo: Optional[CandidateRepository] = None,
) -> None:
    """
    Processa un singolo messaggio queue.

    Payload atteso:
    {
        "file_hash": "...",
        "data": { CVExtraction fields... }
    }

    repo:
        opzionale, solo per test/mocking.
        In produzione viene creato automaticamente.
    """

    request_id = get_request_id()

    payload = json.loads(body)

    file_hash: str = payload["file_hash"]
    data: dict = payload["data"]

    cv = CVExtraction(**data)

    track_event(
        "persist_start",
        request_id=request_id,
        properties={"file_hash": file_hash},
    )

    # -----------------------------------------------------
    # TEST MODE (repo iniettato)
    # -----------------------------------------------------

    if repo:
        await repo.upsert(file_hash, cv)

    # -----------------------------------------------------
    # PROD MODE (conn dal pool)
    # -----------------------------------------------------

    else:
        async with acquire_conn() as conn:
            repo = CandidateRepository(conn)
            await repo.upsert(file_hash, cv)

    track_event(
        "persist_success",
        request_id=request_id,
        properties={"file_hash": file_hash},
    )

    logger.debug("Persisted candidate %s", file_hash)

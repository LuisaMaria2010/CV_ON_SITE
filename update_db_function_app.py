"""
Azure Function: Persist Candidate (Queue Trigger)

Semantica:
- 1 riga = 1 persona
- match_key UNIQUE
- se persona esiste → UPDATE
- se nuova → INSERT

Responsabilità:
- leggere messaggio queue
- validare payload
- costruire CVExtraction
- chiamare repository (upsert)
- tracciare metriche

ZERO:
- logica business
- parsing
- LLM
"""

from __future__ import annotations

import json
import logging
import azure.functions as func

from core.schema import CVExtraction
from core.errors import InvalidInputError

from utils.observability import track_event, track_duration
from utils.request_context import set_request_id

# ✅ repository puro SQL
from persist.repository import CandidateRepository

# ✅ infrastruttura DB (pool)
from persist.db import acquire_conn

from infra.queue_service import QueueService

from app_instance import app

index_queue = QueueService()

logger = logging.getLogger(__name__)


# =========================================================
# Queue Trigger
# =========================================================

@app.queue_trigger(
    arg_name="msg",
    queue_name="cv-persist",
    connection="AzureWebJobsStorage",
)
async def persist_candidate(msg: func.QueueMessage):
    """
    Trigger:
        Azure Storage Queue (cv-persist)

    Message format:
    {
        "hash": "<sha256>",
        "data": {... CVExtraction ...},
        "request_id": optional
    }

    NOTE ARCHITETTURA:
    - questa function orchestration ONLY
    - il repository fa SOLO SQL
    - la connessione viene gestita qui (pool.acquire)
    """

    # -----------------------------------------------------
    # Parse JSON
    # -----------------------------------------------------

    try:
        payload = json.loads(msg.get_body().decode("utf-8"))
    except Exception as e:
        logger.exception("Invalid queue message JSON")
        raise InvalidInputError("Invalid queue message") from e

    file_hash = payload.get("hash")
    data = payload.get("data")

    if not file_hash or not data:
        raise InvalidInputError("Queue message missing 'hash' or 'data'")

    # -----------------------------------------------------
    # Correlation / tracing
    # -----------------------------------------------------

    request_id = payload.get("request_id", file_hash)

    set_request_id(request_id)

    track_event(
        "persist_start",
        request_id=request_id,
        properties={"hash": file_hash},
    )

    # -----------------------------------------------------
    # Persist candidate
    # -----------------------------------------------------

    try:
        # costruzione modello dominio
        cv = CVExtraction(**data)

        # -------------------------------------------------
        # ACQUIRE CONN DAL POOL (release automatico)
        # -------------------------------------------------
        # IMPORTANTISSIMO:
        # niente acquire manuale → evita connection leak
        # -------------------------------------------------

        async with acquire_conn() as conn:
            repo = CandidateRepository(conn)

            with track_duration("db_upsert_ms", request_id=request_id):
                match_key = await repo.upsert(file_hash, cv)
            
                await index_queue.send_json({
                    "match_key": match_key,
                    "data": cv.model_dump(),
                    "request_id": request_id,
                })

        track_event(
            "persist_success",
            request_id=request_id,
        )

    # -----------------------------------------------------
    # Error handling
    # -----------------------------------------------------

    except Exception:
        track_event(
            "persist_error",
            request_id=request_id,
        )
        logger.exception("Persist candidate failed")
        raise

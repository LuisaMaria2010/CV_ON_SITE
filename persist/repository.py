"""
Repository single-table candidate based.

Semantica:
- 1 riga = 1 persona
- match_key UNIQUE
- insert or update (upsert)

Design:
- SOLO SQL
- NESSUNA logica business
- NESSUN pool
- riceve connessione già aperta (dependency injection)
- facilissimo da mockare nei test
"""

from __future__ import annotations

import json
import logging
from typing import Any

from core.schema import CVExtraction


logger = logging.getLogger(__name__)


# =========================================================
# Query
# =========================================================

UPSERT_QUERY = """
INSERT INTO candidates (
    match_key,
    full_name,
    role,
    location,
    email,
    phone,
    language,
    age,
    experience_years,
    seniority,
    payload_json,
    updated_at
)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
ON DUPLICATE KEY UPDATE
    full_name=VALUES(full_name),
    role=VALUES(role),
    location=VALUES(location),
    email=VALUES(email),
    phone=VALUES(phone),
    language=VALUES(language),
    age=VALUES(age),
    experience_years=VALUES(experience_years),
    seniority=VALUES(seniority),
    payload_json=VALUES(payload_json),
    updated_at=NOW()
"""


# =========================================================
# Repository
# =========================================================

class CandidateRepository:
    """
    Repository MySQL per candidati.

    NOTE:
    Riceve UNA connessione già aperta.
    Il pool viene gestito dal layer superiore (worker/service).
    """

    def __init__(self, conn: Any):
        self.conn = conn

    # -----------------------------------------------------

    async def upsert(self, file_hash: str, cv: CVExtraction) -> None:
        """
        Upsert candidato idempotente.

        match_key:
            email se presente
            altrimenti file_hash
        """

        match_key = (cv.email or file_hash).lower()

        payload_json = json.dumps(cv.model_dump())

        params = (
            match_key,
            cv.full_name,
            cv.role,
            cv.location,
            cv.email,
            cv.phone,
            cv.language,
            cv.age,
            cv.experience_years,
            cv.seniority,
            payload_json,
        )

        cur = await self.conn.cursor()
        await cur.execute(UPSERT_QUERY, params)
        await self.conn.commit()

        logger.debug("Upserted candidate %s", match_key)
        return match_key

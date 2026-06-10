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

SELECT_COLUMNS = """
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

    def _serialise_row(self, row: dict[str, Any], include_payload: bool = False) -> dict[str, Any]:
        payload_raw = row.get("payload_json")
        payload: Any = None
        if isinstance(payload_raw, str) and payload_raw.strip():
            try:
                payload = json.loads(payload_raw)
            except json.JSONDecodeError:
                payload = None

        candidate = {
            "match_key": row.get("match_key"),
            "full_name": row.get("full_name"),
            "role": row.get("role"),
            "location": row.get("location"),
            "email": row.get("email"),
            "phone": row.get("phone"),
            "language": row.get("language"),
            "age": row.get("age"),
            "experience_years": row.get("experience_years"),
            "seniority": row.get("seniority"),
            "updated_at": row.get("updated_at").isoformat() if row.get("updated_at") else None,
        }
        if include_payload:
            candidate["payload"] = payload
        return candidate

    async def get_candidate_by_match_key(self, match_key: str, include_payload: bool = True) -> dict[str, Any] | None:
        query = f"""
            SELECT
                {SELECT_COLUMNS}
            FROM candidates
            WHERE match_key = %s
            LIMIT 1
        """
        cur = await self.conn.cursor()
        await cur.execute(query, (match_key.lower(),))
        columns = [desc[0] for desc in cur.description]
        row = await cur.fetchone()
        if not row:
            return None

        row_dict = dict(zip(columns, row))
        return self._serialise_row(row_dict, include_payload=include_payload)

    async def search_candidates(
        self,
        q: str | None,
        limit: int = 10,
        role: str | None = None,
        location: str | None = None,
        seniority: str | None = None,
        language: str | None = None,
        min_experience_years: float | None = None,
        max_experience_years: float | None = None,
    ) -> list[dict[str, Any]]:
        where_clauses: list[str] = []
        params: list[Any] = []

        if q:
            like_q = f"%{q.strip()}%"
            where_clauses.append(
                "(full_name LIKE %s OR role LIKE %s OR location LIKE %s OR payload_json LIKE %s)"
            )
            params.extend([like_q, like_q, like_q, like_q])

        if role:
            where_clauses.append("role LIKE %s")
            params.append(f"%{role.strip()}%")

        if location:
            where_clauses.append("location LIKE %s")
            params.append(f"%{location.strip()}%")

        if seniority:
            where_clauses.append("seniority = %s")
            params.append(seniority.strip().lower())

        if language:
            where_clauses.append("language LIKE %s")
            params.append(f"%{language.strip()}%")

        if min_experience_years is not None:
            where_clauses.append("experience_years >= %s")
            params.append(float(min_experience_years))

        if max_experience_years is not None:
            where_clauses.append("experience_years <= %s")
            params.append(float(max_experience_years))

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        safe_limit = max(1, min(int(limit), 50))

        query = f"""
            SELECT
                {SELECT_COLUMNS}
            FROM candidates
            {where_sql}
            ORDER BY updated_at DESC
            LIMIT %s
        """
        params.append(safe_limit)

        cur = await self.conn.cursor()
        await cur.execute(query, tuple(params))

        columns = [desc[0] for desc in cur.description]
        rows = await cur.fetchall()
        return [
            self._serialise_row(dict(zip(columns, row)), include_payload=False)
            for row in rows
        ]

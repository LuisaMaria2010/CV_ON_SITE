"""
Logica di ricerca candidati: OData builder, reranker, fallback.

Tutte le funzioni sono pure e senza side-effect: facili da testare isolatamente.
L'handler HTTP in function_app.py le orchestra.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from core.config import settings


# =========================================================
# OData filter builder
# =========================================================

def build_odata_filter(
    skills: list[str] | None = None,
    seniority: str | None = None,
    min_experience_years: float | None = None,
    max_experience_years: float | None = None,
    language: str | None = None,
    availability_required: bool = False,
) -> str | None:
    """
    Costruisce un filtro OData combinando i constraint passati.
    Ritorna None se nessun filtro è richiesto.
    """
    clauses: list[str] = []

    if skills:
        for skill in skills:
            safe = skill.replace("'", "''")
            clauses.append(f"skills/any(s: s eq '{safe}')")

    if seniority:
        safe = seniority.replace("'", "''")
        clauses.append(f"seniority eq '{safe}'")

    if min_experience_years is not None:
        clauses.append(f"experience_years ge {min_experience_years}")

    if max_experience_years is not None:
        clauses.append(f"experience_years le {max_experience_years}")

    if language:
        safe = language.replace("'", "''")
        clauses.append(f"language eq '{safe}'")

    if availability_required:
        clauses.append("availability ne null")

    return " and ".join(clauses) if clauses else None


def build_odata_filter_relaxed(
    seniority: str | None = None,
    min_experience_years: float | None = None,
    max_experience_years: float | None = None,
    language: str | None = None,
) -> str | None:
    """Versione rilassata: rimuove il filtro skills, mantiene gli altri."""
    return build_odata_filter(
        skills=None,
        seniority=seniority,
        min_experience_years=min_experience_years,
        max_experience_years=max_experience_years,
        language=language,
    )


# =========================================================
# Reranker
# =========================================================

def _months_ago(iso_date: str | None, now: datetime) -> float | None:
    if not iso_date:
        return None
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = now - dt
        return delta.days / 30.0
    except Exception:
        return None


def rerank(
    hits: list[dict],
    query_skills: list[str] | None = None,
    query_role: str | None = None,
    query_location: str | None = None,
    top: int = 10,
) -> list[dict]:
    """
    Applica scoring composito e ritorna i top N risultati ordinati per score DESC.

    Formula:
        score = lex_score  * lex_weight
              + vec_score  * vec_weight
              + Σ(skill_boost per ogni skill richiesta che il candidato ha)
              + role_boost  se query_role è substring del campo role
              + location_boost se query_location è substring del campo location
              + recency_boost se processed_at < 6 mesi
    """
    now = datetime.now(timezone.utc)
    lw = settings.search_reranker_lex_weight
    vw = settings.search_reranker_vec_weight
    sb = settings.search_reranker_skill_boost
    rb = settings.search_reranker_role_boost
    lb = settings.search_reranker_location_boost
    recb = settings.search_reranker_recency_boost

    norm_skills = {s.lower().strip() for s in (query_skills or [])}
    norm_role = (query_role or "").lower().strip()
    norm_location = (query_location or "").lower().strip()

    scored: list[dict] = []
    for hit in hits:
        s = hit.get("lex_score", 0.0) * lw + hit.get("vec_score", 0.0) * vw

        # skill boost
        candidate_skills = {sk.lower().strip() for sk in (hit.get("skills") or [])}
        matching = norm_skills & candidate_skills
        s += len(matching) * sb

        # role boost
        if norm_role and norm_role in (hit.get("role") or "").lower():
            s += rb

        # location boost
        if norm_location and norm_location in (hit.get("location") or "").lower():
            s += lb

        # recency boost (< 6 months)
        months = _months_ago(hit.get("processed_at"), now)
        if months is not None and months <= 6:
            s += recb

        entry = dict(hit)
        entry["score"] = round(s, 6)
        scored.append(entry)

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top]


# =========================================================
# Request normaliser
# =========================================================

def normalise_search_request(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Normalizza e valida i parametri di ricerca dal body della richiesta.
    Ritorna il dict normalizzato pronto per l'uso nell'handler.
    """
    raw_skills = payload.get("skills") or []
    skills = sorted({str(s).lower().strip() for s in raw_skills if s and str(s).strip()})

    query = str(payload.get("query") or "").strip()
    role = str(payload.get("role") or "").strip() or None
    location = str(payload.get("location") or "").strip() or None
    seniority = str(payload.get("seniority") or "").strip() or None
    language = str(payload.get("language") or "").strip() or None
    subco = str(payload.get("subco") or "").strip().lower() or None

    try:
        top = int(payload.get("top") or 10)
        top = max(1, min(top, 100))
    except (ValueError, TypeError):
        top = 10

    try:
        min_exp = float(payload["min_experience_years"]) if payload.get("min_experience_years") is not None else None
    except (ValueError, TypeError):
        min_exp = None

    try:
        max_exp = float(payload["max_experience_years"]) if payload.get("max_experience_years") is not None else None
    except (ValueError, TypeError):
        max_exp = None

    hybrid = bool(payload.get("hybrid", True))
    availability_required = bool(payload.get("availability_required", False))

    return {
        "query": query,
        "skills": skills,
        "role": role,
        "location": location,
        "seniority": seniority,
        "language": language,
        "subco": subco,
        "top": top,
        "min_experience_years": min_exp,
        "max_experience_years": max_exp,
        "hybrid": hybrid,
        "availability_required": availability_required,
    }


# =========================================================
# Index routing
# =========================================================

def resolve_index(subco: str | None) -> str:
    return settings.document_search_index_name

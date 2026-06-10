"""
Logica di ricerca candidati: OData builder, reranker, fallback.

Tutte le funzioni sono pure e senza side-effect: facili da testare isolatamente.
L'handler HTTP in function_app.py le orchestra.
"""
from __future__ import annotations

import re
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


def _norm_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def _norm_list(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, list):
        return [_norm_text(v) for v in values if _norm_text(v)]
    if isinstance(values, str):
        return [_norm_text(v) for v in values.split(",") if _norm_text(v)]
    return []


def _seniority_from_years(years: float | None) -> str | None:
    if years is None:
        return None
    if years < 2:
        return "junior"
    if years < 5:
        return "mid"
    if years < 10:
        return "senior"
    if years < 15:
        return "lead"
    return "principal"


def _derive_request_seniority(
    explicit_seniority: str | None,
    *,
    min_experience_years: float | None,
    max_experience_years: float | None,
) -> tuple[str | None, bool]:
    normalized_explicit = _norm_text(explicit_seniority) or None
    if normalized_explicit:
        return normalized_explicit, False

    # Derive only from the lower bound. Using max years would create an
    # arbitrary hard label and make broad requests overly brittle.
    if min_experience_years is not None:
        return _seniority_from_years(min_experience_years), True

    return None, False


def _parse_experience_years_from_query(query: str) -> tuple[float | None, float | None]:
    text = _norm_text(query)
    if not text:
        return None, None

    min_years: float | None = None
    max_years: float | None = None

    patterns_min = [
        r"\balmeno\s+(\d+(?:[.,]\d+)?)\s*(?:anni?|years?)\b",
        r"\bda\s+(\d+(?:[.,]\d+)?)\s*(?:anni?|years?)\b",
        r"\b(\d+(?:[.,]\d+)?)\s*\+\s*(?:anni?|years?)\b",
        r"\b(\d+(?:[.,]\d+)?)\s*(?:anni?|years?)\s*(?:di )?esperienz",
    ]
    patterns_max = [
        r"\bmassimo\s+(\d+(?:[.,]\d+)?)\s*(?:anni?|years?)\b",
        r"\bentro\s+(\d+(?:[.,]\d+)?)\s*(?:anni?|years?)\b",
        r"\bfino a\s+(\d+(?:[.,]\d+)?)\s*(?:anni?|years?)\b",
    ]
    range_patterns = [
        r"\btra\s+(\d+(?:[.,]\d+)?)\s+e\s+(\d+(?:[.,]\d+)?)\s*(?:anni?|years?)\b",
        r"\b(\d+(?:[.,]\d+)?)\s*[-–]\s*(\d+(?:[.,]\d+)?)\s*(?:anni?|years?)\b",
    ]

    for pattern in range_patterns:
        match = re.search(pattern, text)
        if match:
            try:
                min_years = float(match.group(1).replace(",", "."))
                max_years = float(match.group(2).replace(",", "."))
                return min_years, max_years
            except ValueError:
                pass

    for pattern in patterns_min:
        match = re.search(pattern, text)
        if match:
            try:
                min_years = float(match.group(1).replace(",", "."))
                break
            except ValueError:
                pass

    for pattern in patterns_max:
        match = re.search(pattern, text)
        if match:
            try:
                max_years = float(match.group(1).replace(",", "."))
                break
            except ValueError:
                pass

    return min_years, max_years


def _role_match_score(requested_role: str, candidate_role: str) -> float:
    if not requested_role or not candidate_role:
        return 0.0
    if requested_role == candidate_role:
        return 1.0
    if requested_role in candidate_role or candidate_role in requested_role:
        return 0.75
    req_tokens = set(requested_role.split())
    cand_tokens = set(candidate_role.split())
    if not req_tokens or not cand_tokens:
        return 0.0
    overlap = len(req_tokens & cand_tokens)
    return round(overlap / len(req_tokens), 4)


def _location_match_label(requested_location: str, candidate_location: str, work_mode: str) -> str:
    if not requested_location:
        return "unknown"
    if work_mode == "remote":
        return "not_applicable"
    if not candidate_location:
        return "none"
    if requested_location == candidate_location:
        return "exact"
    if requested_location in candidate_location or candidate_location in requested_location:
        return "soft"
    return "none"


def build_match_features(
    candidate: dict[str, Any],
    *,
    query_skills: list[str] | None,
    query_role: str | None,
    query_location: str | None,
    query_language: str | None,
    work_mode: str,
    relaxed_criteria: list[str],
    is_relaxed_result: bool,
) -> dict[str, Any]:
    requested_skills = _norm_list(query_skills)
    candidate_skills = _norm_list(candidate.get("skills"))

    matched_skills: list[str] = []
    semantic_matches: list[str] = []
    missing_skills: list[str] = []
    for req_skill in requested_skills:
        if req_skill in candidate_skills:
            matched_skills.append(req_skill)
            continue
        semantic_candidate = next(
            (
                cs
                for cs in candidate_skills
                if req_skill in cs or cs in req_skill
            ),
            None,
        )
        if semantic_candidate:
            semantic_matches.append(semantic_candidate)
        else:
            missing_skills.append(req_skill)

    requested_role = _norm_text(query_role)
    candidate_role = _norm_text(candidate.get("role"))
    role_score = _role_match_score(requested_role, candidate_role)

    requested_location = _norm_text(query_location)
    candidate_location = _norm_text(candidate.get("location"))
    location_match = _location_match_label(requested_location, candidate_location, _norm_text(work_mode) or "unknown")

    requested_language = _norm_text(query_language)
    candidate_language = _norm_text(candidate.get("language"))
    if requested_language:
        language_match: bool | str = requested_language in candidate_language if candidate_language else False
    else:
        language_match = "unknown"

    matched_on: list[str] = []
    if matched_skills or semantic_matches:
        matched_on.append("skills")
    if role_score >= 0.5:
        matched_on.append("role")
    if location_match in {"exact", "soft"}:
        matched_on.append("location")
    if language_match is True:
        matched_on.append("language")

    return {
        "skills": {
            "requested": requested_skills,
            "matched": matched_skills,
            "semantic_matches": semantic_matches,
            "missing": missing_skills,
        },
        "role": {
            "requested": requested_role or None,
            "candidate": candidate_role or None,
            "score": round(role_score, 4),
        },
        "location": {
            "requested": requested_location or None,
            "candidate": candidate_location or None,
            "match": location_match,
        },
        "language": {
            "requested": requested_language or None,
            "candidate": candidate_language or None,
            "match": language_match,
        },
        "relaxed_criteria": list(dict.fromkeys(relaxed_criteria if is_relaxed_result else [])),
        "is_relaxed_result": bool(is_relaxed_result),
        "matched_on": matched_on,
    }


def enrich_hits_with_match_features(
    hits: list[dict[str, Any]],
    *,
    query_skills: list[str] | None,
    query_role: str | None,
    query_location: str | None,
    query_language: str | None,
    work_mode: str,
    relaxed_criteria: list[str],
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for hit in hits:
        entry = dict(hit)
        entry["match_features"] = build_match_features(
            entry,
            query_skills=query_skills,
            query_role=query_role,
            query_location=query_location,
            query_language=query_language,
            work_mode=work_mode,
            relaxed_criteria=relaxed_criteria,
            is_relaxed_result=bool(entry.get("is_relaxed_result", False)),
        )
        enriched.append(entry)
    return enriched


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
    explicit_seniority = str(payload.get("seniority") or "").strip() or None
    language = str(payload.get("language") or "").strip() or None
    subco = str(payload.get("subco") or "").strip().lower() or None
    work_mode = str(payload.get("work_mode") or "").strip().lower() or "unknown"
    if work_mode not in {"remote", "hybrid", "onsite", "unknown"}:
        work_mode = "unknown"

    raw_relaxed = payload.get("relaxed_criteria") or []
    relaxed_criteria = sorted({str(v).strip().lower() for v in raw_relaxed if str(v).strip()})

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

    inferred_min_exp, inferred_max_exp = _parse_experience_years_from_query(query)
    if min_exp is None:
        min_exp = inferred_min_exp
    if max_exp is None:
        max_exp = inferred_max_exp

    seniority, seniority_inferred = _derive_request_seniority(
        explicit_seniority,
        min_experience_years=min_exp,
        max_experience_years=max_exp,
    )

    hybrid = bool(payload.get("hybrid", True))

    # Support classifier outputs where availability can arrive as free text
    # (e.g. "entro lunedi") instead of an explicit boolean flag.
    availability = payload.get("availability")
    availability_required = bool(payload.get("availability_required", False))
    if isinstance(availability, bool):
        availability_required = availability_required or availability
    elif isinstance(availability, (int, float)):
        availability_required = availability_required or bool(availability)
    elif isinstance(availability, str):
        availability_required = availability_required or bool(availability.strip())

    strict = bool(payload.get("strict", True))

    return {
        "query": query,
        "skills": skills,
        "role": role,
        "location": location,
        "work_mode": work_mode,
        "seniority": seniority,
        "seniority_explicit": _norm_text(explicit_seniority) or None,
        "seniority_inferred": seniority_inferred,
        "language": language,
        "subco": subco,
        "top": top,
        "strict": strict,
        "relaxed_criteria": relaxed_criteria,
        "min_experience_years": min_exp,
        "max_experience_years": max_exp,
        "hybrid": hybrid,
        "availability": availability,
        "availability_required": availability_required,
    }


# =========================================================
# Index routing
# =========================================================

def resolve_index(subco: str | None) -> str:
    return settings.document_search_index_name

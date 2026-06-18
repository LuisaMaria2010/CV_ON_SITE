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


ROLE_NOISE = {
    "junior",
    "mid",
    "middle",
    "senior",
    "lead",
    "principal",
    "staff",
}


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
    Applica ranking puramente retrieval-oriented e ritorna i top N per score DESC.

    Formula:
        score = semantic_score_norm * 0.70
              + vec_score_norm      * 0.20
              + lex_score_norm      * 0.10
              + recency_boost (opzionale, < 6 mesi)

    Nota: query_skills/query_role/query_location sono mantenuti per backward-compatibility,
    ma non influiscono piu' sul ranking.
    """
    now = datetime.now(timezone.utc)
    recb = settings.search_reranker_recency_boost

    _ = (query_skills, query_role, query_location)

    def _norm_semantic(value: Any) -> float:
        # Azure semantic reranker score is commonly in [0,4].
        try:
            return max(0.0, min(1.0, float(value) / 4.0))
        except Exception:
            return 0.0

    def _norm_retrieval(value: Any) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except Exception:
            return 0.0

    scored: list[dict] = []
    for hit in hits:
        semantic_norm = _norm_semantic(hit.get("semantic_score", 0.0))
        vec_norm = _norm_retrieval(hit.get("vec_score", 0.0))
        lex_norm = _norm_retrieval(hit.get("lex_score", 0.0))

        s = semantic_norm * 0.70 + vec_norm * 0.20 + lex_norm * 0.10

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


def _clamp_01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _score_value(value: Any, *, applicable: bool) -> float | None:
    if not applicable:
        return None
    try:
        return round(_clamp_01(float(value)), 4)
    except Exception:
        return 0.0


def _skills_match_features(requested_skills: list[str], candidate_skills: list[str]) -> dict[str, Any]:
    matched: list[str] = []
    semantic_matches: list[str] = []

    for req_skill in requested_skills:
        if req_skill in candidate_skills:
            matched.append(req_skill)
            continue

        semantic_hit = next(
            (
                cs
                for cs in candidate_skills
                if req_skill in cs or cs in req_skill
            ),
            None,
        )
        if semantic_hit:
            semantic_matches.append(req_skill)

    if not requested_skills:
        score = 0.0
    else:
        score = (len(set(matched)) + 0.7 * len(set(semantic_matches))) / len(requested_skills)

    return {
        "score": round(_clamp_01(score), 4),
        "matched": sorted(set(matched)),
        "semantic_matches": sorted(set(semantic_matches)),
    }


def _role_match_features(requested_role: str, candidate_role: str) -> dict[str, Any]:
    if not requested_role:
        return {"applicable": False, "score": None, "match": "not_requested"}
    if requested_role == candidate_role:
        return {"applicable": True, "score": 1.0, "match": "exact"}

    if requested_role in candidate_role or candidate_role in requested_role:
        return {"applicable": True, "score": 0.8, "match": "semantic"}

    req_tokens = set(requested_role.split())
    cand_tokens = set(candidate_role.split())

    # Seniority is evaluated in a dedicated dimension, so exclude it from role similarity.
    req_tokens -= ROLE_NOISE
    cand_tokens -= ROLE_NOISE

    # Keep acronym expansion in Search Layer only.
    if "qa" in req_tokens:
        req_tokens.update({"quality", "assurance"})
    if "qa" in cand_tokens:
        cand_tokens.update({"quality", "assurance"})

    overlap = len(req_tokens & cand_tokens) if req_tokens and cand_tokens else 0
    if overlap <= 0:
        return {"applicable": True, "score": 0.0, "match": "none"}

    score = overlap / max(1, len(req_tokens))
    if score >= 0.4:
        return {"applicable": True, "score": round(_clamp_01(score), 4), "match": "semantic"}
    return {"applicable": True, "score": round(_clamp_01(score), 4), "match": "partial"}


def _location_match_features(requested_location: str, candidate_location: str, work_mode: str) -> dict[str, Any]:
    if not requested_location:
        return {"applicable": False, "score": None, "match": "not_requested"}
    if work_mode == "remote":
        return {"applicable": True, "score": 1.0, "match": "not_applicable"}
    if not candidate_location:
        return {"applicable": True, "score": 0.0, "match": "none"}
    if requested_location == candidate_location:
        return {"applicable": True, "score": 1.0, "match": "exact"}
    if requested_location in candidate_location or candidate_location in requested_location:
        return {"applicable": True, "score": 0.6, "match": "soft"}

    req_tokens = set(requested_location.split())
    cand_tokens = set(candidate_location.split())
    overlap = len(req_tokens & cand_tokens) if req_tokens and cand_tokens else 0
    if overlap > 0:
        return {"applicable": True, "score": 0.35, "match": "weak"}
    return {"applicable": True, "score": 0.0, "match": "none"}


def _language_match_features(requested_language: str, candidate_language: str) -> dict[str, Any]:
    if not requested_language:
        return {"applicable": False, "score": None, "match": "not_requested"}
    if not candidate_language:
        return {"applicable": True, "score": 0.0, "match": False}
    is_match = requested_language in candidate_language or candidate_language in requested_language
    return {"applicable": True, "score": 1.0 if is_match else 0.0, "match": bool(is_match)}


def _seniority_match_features(requested_seniority: str, candidate_seniority: str) -> dict[str, Any]:
    if not requested_seniority:
        return {"applicable": False, "score": None, "match": "not_requested"}
    if not candidate_seniority:
        return {"applicable": True, "score": 0.0, "match": "none"}
    if requested_seniority == candidate_seniority:
        return {"applicable": True, "score": 1.0, "match": "exact"}
    return {"applicable": True, "score": 0.0, "match": "none"}


def _availability_match_features(required: bool, candidate_availability_days: int | None) -> dict[str, Any]:
    if not required:
        return {"applicable": False, "score": None, "match": "not_requested"}
    if candidate_availability_days is None:
        return {"applicable": True, "score": 0.0, "match": "none"}
    if candidate_availability_days <= 30:
        return {"applicable": True, "score": 1.0, "match": "exact"}
    if candidate_availability_days <= 60:
        return {"applicable": True, "score": 0.6, "match": "partial"}
    return {"applicable": True, "score": 0.2, "match": "weak"}


def _canonicalize_match_features_contract(match_features: dict[str, Any]) -> dict[str, Any]:
    skills = match_features.get("skills") if isinstance(match_features.get("skills"), dict) else {}
    role = match_features.get("role") if isinstance(match_features.get("role"), dict) else {}
    location = match_features.get("location") if isinstance(match_features.get("location"), dict) else {}
    language = match_features.get("language") if isinstance(match_features.get("language"), dict) else {}
    seniority = match_features.get("seniority") if isinstance(match_features.get("seniority"), dict) else {}
    availability = match_features.get("availability") if isinstance(match_features.get("availability"), dict) else {}

    role_match = _norm_text(role.get("match"))
    if role_match not in {"exact", "semantic", "partial", "none", "not_requested"}:
        role_match = "none"

    location_match = _norm_text(location.get("match"))
    if location_match not in {"exact", "soft", "weak", "none", "not_applicable", "not_requested"}:
        location_match = "none"

    language_match_raw = language.get("match")
    if isinstance(language_match_raw, bool):
        language_match: bool | str = language_match_raw
    else:
        language_match = "not_requested" if _norm_text(language_match_raw) == "not_requested" else False

    seniority_match = _norm_text(seniority.get("match"))
    if seniority_match not in {"exact", "none", "not_requested"}:
        seniority_match = "none"

    availability_match = _norm_text(availability.get("match"))
    if availability_match not in {"exact", "partial", "weak", "none", "not_requested"}:
        availability_match = "none"

    role_applicable = bool(role.get("applicable", role_match != "not_requested"))
    location_applicable = bool(location.get("applicable", location_match != "not_requested"))
    language_applicable = bool(language.get("applicable", language_match != "not_requested"))
    seniority_applicable = bool(seniority.get("applicable", seniority_match != "not_requested"))
    availability_applicable = bool(availability.get("applicable", availability_match != "not_requested"))

    return {
        "skills": {
            "score": round(_clamp_01(skills.get("score", 0.0)), 4),
            "matched": _norm_list(skills.get("matched")),
            "semantic_matches": _norm_list(skills.get("semantic_matches")),
        },
        "role": {
            "applicable": role_applicable,
            "score": _score_value(role.get("score"), applicable=role_applicable),
            "match": role_match,
        },
        "location": {
            "applicable": location_applicable,
            "score": _score_value(location.get("score"), applicable=location_applicable),
            "match": location_match,
        },
        "language": {
            "applicable": language_applicable,
            "score": _score_value(language.get("score"), applicable=language_applicable),
            "match": language_match,
        },
        "seniority": {
            "applicable": seniority_applicable,
            "score": _score_value(seniority.get("score"), applicable=seniority_applicable),
            "match": seniority_match,
        },
        "availability": {
            "applicable": availability_applicable,
            "score": _score_value(availability.get("score"), applicable=availability_applicable),
            "match": availability_match,
        },
    }


def build_match_features(
    candidate: dict[str, Any],
    *,
    query_skills: list[str] | None,
    query_role: str | None,
    query_location: str | None,
    query_language: str | None,
    query_seniority: str | None,
    query_availability_required: bool,
    work_mode: str,
    relaxed_criteria: list[str],
    is_relaxed_result: bool,
) -> dict[str, Any]:
    requested_skills = _norm_list(query_skills)
    candidate_skills = _norm_list(candidate.get("skills"))
    requested_role = _norm_text(query_role)
    candidate_role = _norm_text(candidate.get("role"))
    requested_location = _norm_text(query_location)
    candidate_location = _norm_text(candidate.get("location"))
    requested_language = _norm_text(query_language)
    candidate_language = _norm_text(candidate.get("language"))
    requested_seniority = _norm_text(query_seniority)
    candidate_seniority = _norm_text(candidate.get("seniority"))

    candidate_availability_days: int | None = None
    availability_raw = candidate.get("availability_days")
    if isinstance(availability_raw, (int, float)):
        candidate_availability_days = int(availability_raw)
    elif isinstance(candidate.get("availability"), (int, float)):
        candidate_availability_days = int(candidate.get("availability"))
    elif isinstance(candidate.get("availability"), str):
        m = re.search(r"(\d+)", candidate.get("availability") or "")
        if m:
            candidate_availability_days = int(m.group(1))

    skills_features = _skills_match_features(requested_skills, candidate_skills)
    role_features = _role_match_features(requested_role, candidate_role)
    location_features = _location_match_features(
        requested_location,
        candidate_location,
        _norm_text(work_mode) or "unknown",
    )
    language_features = _language_match_features(requested_language, candidate_language)
    seniority_features = _seniority_match_features(requested_seniority, candidate_seniority)
    availability_features = _availability_match_features(bool(query_availability_required), candidate_availability_days)

    matched_on: list[str] = []
    if skills_features["score"] > 0:
        matched_on.append("skills")
    if float(role_features.get("score") or 0.0) > 0:
        matched_on.append("role")
    if float(location_features.get("score") or 0.0) > 0:
        matched_on.append("location")
    if language_features["match"] is True:
        matched_on.append("language")
    if seniority_features.get("score") and float(seniority_features.get("score") or 0.0) > 0:
        matched_on.append("seniority")
    if availability_features.get("score") and float(availability_features.get("score") or 0.0) > 0:
        matched_on.append("availability")

    contract = _canonicalize_match_features_contract(
        {
            "skills": skills_features,
            "role": role_features,
            "location": location_features,
            "language": language_features,
            "seniority": seniority_features,
            "availability": availability_features,
        }
    )

    contract["relaxed_criteria"] = list(dict.fromkeys(relaxed_criteria if is_relaxed_result else []))
    contract["is_relaxed_result"] = bool(is_relaxed_result)
    contract["matched_on"] = matched_on
    return contract


def enrich_hits_with_match_features(
    hits: list[dict[str, Any]],
    *,
    query_skills: list[str] | None,
    query_role: str | None,
    query_location: str | None,
    query_language: str | None,
    query_seniority: str | None,
    query_availability_required: bool,
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
            query_seniority=query_seniority,
            query_availability_required=query_availability_required,
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
    availability_date_raw = payload.get("availability_date")
    availability_date = (
        str(availability_date_raw).strip() if availability_date_raw is not None else ""
    ) or None

    availability_days_raw = payload.get("availability_days")
    try:
        availability_days = (
            int(availability_days_raw)
            if availability_days_raw is not None and str(availability_days_raw).strip() != ""
            else None
        )
    except (ValueError, TypeError):
        availability_days = None

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
        "availability_date": availability_date,
        "availability_days": availability_days,
        "availability_required": availability_required,
    }


# =========================================================
# Index routing
# =========================================================

def resolve_index(subco: str | None) -> str:
    return settings.document_search_index_name

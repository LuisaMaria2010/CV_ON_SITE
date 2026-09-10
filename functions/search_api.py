"""Ricerca candidati: POST /api/search e POST /api/searcher-wrapper.

Estratto da function_app.py senza modifiche ai corpi delle funzioni.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any

import azure.functions as func

from core.config import settings
from core.dependencies import get_mcflash_candidates_client as _get_mcflash_candidates_client
from core.errors import InvalidInputError
from services.coherence_evaluator import run_candidate_coherence_evaluator as _run_candidate_coherence_evaluator
from services.search_pipeline import run_search_pipeline
from utils.http_errors import http_error_handler
from utils.http_params import body_params as _body_params, payload_from_query as _payload_from_query
from utils.values import first_non_empty as _first_non_empty, lower_list as _lower_list, safe_str as _safe_str

logger = logging.getLogger(__name__)

bp = func.Blueprint()

def _extract_availability_days(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        n = int(value)
        return n if n >= 0 else None
    if isinstance(value, str):
        m = re.search(r"(\d+)", value)
        if m:
            n = int(m.group(1))
            return n if n >= 0 else None
    return None

def _aggregate_semantic_evidence(
    top_chunks: list[dict[str, Any]],
    all_chunks: list[dict[str, Any]],
    aggregated_skills: list[str],
) -> str | None:
    parts: list[str] = []

    for chunk in top_chunks[:4]:
        evidence = chunk.get("semantic_evidence")
        if isinstance(evidence, str) and evidence.strip():
            parts.extend([p.strip() for p in evidence.split("|") if p.strip()])

    if aggregated_skills:
        parts.extend(aggregated_skills[:6])

    for chunk in all_chunks[:8]:
        content = _safe_str(chunk.get("content"))
        if content:
            parts.append(content[:140])

    deduped: list[str] = []
    seen: set[str] = set()
    for item in parts:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= 8:
            break

    if not deduped:
        return None
    return " | ".join(deduped)

def _requested_availability_days(raw_date: Any, raw_days: Any) -> int | None:
    explicit_days = _extract_availability_days(raw_days)
    if explicit_days is not None:
        return explicit_days

    if not isinstance(raw_date, str) or not raw_date.strip():
        return None

    text = raw_date.strip()
    dt: datetime | None = None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        try:
            dt = datetime.fromisoformat(f"{text}T00:00:00+00:00")
        except Exception:
            return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    delta_days = (dt.date() - datetime.now(timezone.utc).date()).days
    return max(0, int(delta_days))

def _apply_optional_constraints(
    hits: list[dict[str, Any]],
    *,
    query_location: str | None,
    work_mode: str | None,
    availability_date: str | None,
    availability_days: int | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    filtered = list(hits)
    ignored: list[str] = []

    normalized_mode = _safe_str(work_mode).lower() or "unknown"
    normalized_location = _safe_str(query_location).lower()

    if filtered and normalized_location and normalized_mode in {"onsite", "hybrid", "unknown"}:
        mode_filtered = [
            hit for hit in filtered
            if float((hit.get("match_features") or {}).get("location", {}).get("score") or 0.0) > 0.0
        ]
        if mode_filtered:
            filtered = mode_filtered
        else:
            ignored.append("work_mode")

    max_availability_days = _requested_availability_days(availability_date, availability_days)
    if filtered and max_availability_days is not None:
        availability_filtered = []
        for hit in filtered:
            candidate_days = _extract_availability_days(
                _first_non_empty(hit.get("availability_days"), hit.get("availability"))
            )
            if candidate_days is None:
                continue
            if candidate_days <= max_availability_days:
                availability_filtered.append(hit)

        if availability_filtered:
            filtered = availability_filtered
        else:
            ignored.append("availability_date")

    return filtered, ignored

async def _run_search_pipeline(payload: dict) -> dict:
    return await run_search_pipeline(
        payload,
        get_mcflash_candidates_client=_get_mcflash_candidates_client,
        logger=logger,
    )

def _mcflash_value(candidate: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in candidate and candidate.get(key) is not None:
            return candidate.get(key)
    return None

def _build_minimal_search_hit(hit: dict[str, Any], requested_skills: list[str]) -> dict[str, Any]:
    """Fixed contract, always present regardless of which search_request fields
    were used: id_mcflash, nome, ruolo, eta, seniority, location, skills,
    matched_skills, missing_skills, match_type, workmode, lingue, disponibilita,
    budget, semantic_snippet."""
    mcflash_profile = hit.get("mcflash_profile") if isinstance(hit.get("mcflash_profile"), dict) else {}
    compact_skills = _compact_skills_for_wrapper(hit.get("skills"), requested_skills)
    matched_skills, missing_skills = _skill_coverage_for_wrapper(hit, requested_skills)

    return {
        # Real MCFlash record id only: null when no MCFlash profile was resolved
        # for this candidate (do not fall back to the CV/index id — that would
        # silently mix two different identity spaces under the same field).
        "id_mcflash": _mcflash_value(mcflash_profile, "id", "Id"),
        "nome": _first_non_empty(
            _mcflash_value(mcflash_profile, "nome", "Nome", "nomi", "Nomi", "full_name", "name"),
            hit.get("full_name"),
            hit.get("name"),
        ),
        "ruolo": _first_non_empty(_mcflash_value(mcflash_profile, "ruolo", "role"), hit.get("role")),
        "eta": _mcflash_value(mcflash_profile, "eta", "age"),
        "seniority": _first_non_empty(_mcflash_value(mcflash_profile, "seniority"), hit.get("seniority")),
        "location": _first_non_empty(_mcflash_value(mcflash_profile, "sede", "location"), hit.get("location")),
        "workmode": _mcflash_value(mcflash_profile, "work_mode", "workmode"),
        "lingue": _first_non_empty(_mcflash_value(mcflash_profile, "lingue", "language"), hit.get("language")),
        "disponibilita": _first_non_empty(
            _mcflash_value(mcflash_profile, "disponibilita", "availability"),
            hit.get("availability"),
        ),
        "budget": _mcflash_value(mcflash_profile, "budget", "max_budget", "budget_max"),
        "skills": compact_skills,
        "matched_skills": matched_skills,
        "missing_skills": missing_skills,
        "match_type": _match_type_for_wrapper(hit),
        "semantic_snippet": hit.get("semantic_evidence"),
    }

def _skill_present_in_candidate(skill: str, candidate: list[str], candidate_set: set[str]) -> bool:
    """Exact match, or the candidate has a more specific variant of the
    requested skill (requested "spring" covered by candidate "spring boot").
    NOT the reverse: candidate "java" does not cover requested "java 17".
    Same rule as search_handler._skills_match_features."""
    if skill in candidate_set:
        return True
    return any(skill in cs for cs in candidate)

def _compact_skills_for_wrapper(
    candidate_skills: Any,
    requested_skills: list[str],
    *,
    max_skills: int = 6,
) -> list[str]:
    """Skills shown for a candidate = the candidate's OWN skills, deduped and
    capped. Already ordered by relevance to the request upstream
    (_order_skills_by_relevance). The requested strings are never injected here:
    doing so made a candidate with plain "java" look like it had "java 17".
    Coverage vs the request is carried separately by matched_skills /
    missing_skills. `requested_skills` is kept for signature stability."""
    _ = requested_skills
    candidate = [s for s in _lower_list(candidate_skills) if s]

    selected: list[str] = []
    seen: set[str] = set()
    for skill in candidate:
        if skill in seen:
            continue
        selected.append(skill)
        seen.add(skill)
        if len(selected) >= max_skills:
            break

    return selected

def _skill_coverage_for_wrapper(
    hit: dict[str, Any], requested_skills: list[str]
) -> tuple[list[str], list[str]]:
    """(matched, missing) requested skills for a candidate. Prefers the honest
    figures already computed by search_handler.build_match_features
    (`hit["match_features"]["skills"]`); falls back to a local check when the
    pipeline did not enrich the hit."""
    requested = [s for s in _lower_list(requested_skills) if s]
    if not requested:
        return [], []

    matched: list[str] = []
    mf = hit.get("match_features")
    if isinstance(mf, dict) and isinstance(mf.get("skills"), dict):
        sk = mf["skills"]
        matched = [s for s in _lower_list(sk.get("matched")) + _lower_list(sk.get("semantic_matches")) if s]
    else:
        candidate = [s for s in _lower_list(hit.get("skills")) if s]
        candidate_set = set(candidate)
        matched = [s for s in requested if _skill_present_in_candidate(s, candidate, candidate_set)]

    matched = list(dict.fromkeys(matched))
    matched_set = set(matched)
    missing = [s for s in requested if s not in matched_set]
    return matched, missing

def _match_type_for_wrapper(hit: dict[str, Any]) -> str:
    """How this candidate entered the result set — so the agent can flag weak
    matches instead of presenting all 6 as solid."""
    if hit.get("second_pass_skills_relaxed"):
        return "hybrid_no_skill_filter"
    if hit.get("second_pass_extension"):
        return "hybrid_extension"
    if hit.get("is_relaxed_result"):
        return "relaxed_skills"
    return "strict"


@bp.route(route="search", methods=["POST"])
@http_error_handler
async def search_candidates(req: func.HttpRequest):
    """
    POST /api/search

    Ricerca ibrida (lexical + vector) su candidati indicizzati.
    Applica reranker custom e fallback relaxation se i risultati sono insufficienti.
    """
    payload = _body_params(req)
    if not payload:
        raise InvalidInputError("Missing or invalid JSON body")

    return await _run_search_pipeline(payload)


@bp.route(route="searcher-wrapper", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
@http_error_handler
async def searcher_wrapper(req: func.HttpRequest):
    """
    POST /api/searcher-wrapper

    Wrapper API per Foundry classifier:
    - accetta un payload del classifier con campo `search_request`
    - esegue la ricerca tramite la stessa pipeline di /api/search
    - ritorna classificazione + risposta search
    """
    payload = _body_params(req)
    if not payload:
        payload = _payload_from_query(req)
    if not payload:
        raise InvalidInputError("Missing or invalid JSON body")

    search_request = payload.get("search_request")
    if search_request is None:
        # backward-compatible: accept direct /api/search-like payload
        search_request = payload

    if not isinstance(search_request, dict):
        raise InvalidInputError("'search_request' must be an object")

    # Availability and language-level constraints are handled outside index search.
    # Keep language filter enabled, but do not pass language level until indexed support exists.
    search_request_for_index = {
        k: v for k, v in search_request.items()
        if k not in {
            "language_level",
            "languageLevel",
            "english_level",
        }
    }

    has_index_search_criteria = bool(
        str(search_request_for_index.get("query") or "").strip()
        or (isinstance(search_request_for_index.get("skills"), list) and len(search_request_for_index.get("skills")) > 0)
        or str(search_request_for_index.get("role") or "").strip()
    )

    if has_index_search_criteria:
        search_response = await _run_search_pipeline(search_request_for_index)
    else:
        fallback_top = 10
        try:
            fallback_top = max(1, min(int(search_request.get("top") or 10), 100))
        except Exception:
            fallback_top = 10

        search_response = {
            "hits": [],
            "meta": {
                "total": 0,
                "top": fallback_top,
                "relaxed": False,
                "relaxed_criteria": [],
                "hybrid": bool(search_request.get("hybrid", True)),
                "work_mode": str(search_request.get("work_mode") or "unknown"),
                "index": settings.document_search_index_name,
                "skipped": "availability_only",
            },
            "suggestions": [],
        }

    if isinstance(search_response.get("hits"), list) and search_response["hits"]:
        logger.info(
            "search hit keys=%s",
            list(search_response["hits"][0].keys()),
        )

    requested_skills = _lower_list(search_request.get("skills")) if isinstance(search_request, dict) else []
    requested_role = _safe_str(search_request.get("role")) if isinstance(search_request, dict) else ""
    raw_hits = search_response.get("hits") if isinstance(search_response.get("hits"), list) else []

    pipeline_meta = search_response.get("meta") if isinstance(search_response.get("meta"), dict) else {}
    hard_select_meta = pipeline_meta.get("hard_select") if isinstance(pipeline_meta.get("hard_select"), dict) else {}
    hard_select_widened = bool(hard_select_meta.get("widened"))

    # When MCFlash's business filters matched nobody and we widened to the
    # whole archive with only a role (no skills) to go on, the index has no
    # hard constraint left to discriminate on — role/seniority keywords alone
    # can surface completely unrelated CVs (e.g. "junior QA" -> a Front End
    # Developer, "consulente" -> a COBOL developer). Trim to candidates whose
    # role actually matches at least a little; if none do, show none rather
    # than padding the answer with 6 unrelated profiles.
    if hard_select_widened and not requested_skills and requested_role:
        def _has_role_signal(hit: Any) -> bool:
            mf = hit.get("match_features") if isinstance(hit, dict) else None
            role_mf = mf.get("role") if isinstance(mf, dict) else None
            score = role_mf.get("score") if isinstance(role_mf, dict) else None
            try:
                return score is not None and float(score) > 0
            except (TypeError, ValueError):
                return False

        raw_hits = [h for h in raw_hits if isinstance(h, dict) and _has_role_signal(h)]

    minimal_hits: list[dict[str, Any]] = []
    for hit in raw_hits:
        if not isinstance(hit, dict):
            continue
        minimal_hits.append(_build_minimal_search_hit(hit, requested_skills))

    original_request = _safe_str(
        _first_non_empty(payload.get("original_request"), payload.get("query"), search_request.get("query"))
    )

    # Evaluate coherence server-side so the orchestrator gets ranked candidates
    # + clarifying questions in this same call, instead of needing a second
    # round trip to invoke_match_evaluator. See _run_candidate_coherence_evaluator
    # for the short-circuit/fallback conditions (always safe: worst case, the
    # original retrieval order is returned unchanged).
    evaluation = await asyncio.to_thread(
        _run_candidate_coherence_evaluator, original_request, minimal_hits
    )

    # Surface retrieval diagnostics so the agent knows when the search was
    # widened (skills filter dropped, hybrid extension) and does not present
    # relaxed / second-pass candidates as strict matches.
    pipeline_suggestions = search_response.get("suggestions") if isinstance(search_response.get("suggestions"), list) else []
    if hard_select_widened and not requested_skills:
        # Nothing was actually hard-filtered (business filters matched nobody,
        # no skills to filter the index on) — "strict" would overstate how
        # solid these matches are, even for the ones that survived the role
        # trim above.
        strict_count = 0
    else:
        strict_count = sum(1 for h in minimal_hits if h.get("match_type") == "strict")

    return {
        "original_request": original_request,
        "interpreted_request": search_request,
        "search_response": {
            "hits": evaluation["candidates"],
        },
        "search_meta": {
            "relaxed": bool(pipeline_meta.get("relaxed")),
            "relaxed_criteria": pipeline_meta.get("relaxed_criteria") or [],
            "total_candidates": pipeline_meta.get("total", len(minimal_hits)),
            "strict_candidates": strict_count,
            "hard_select_matched": hard_select_meta.get("matched_candidates"),
            "suggestions": pipeline_suggestions,
        },
        "verdict": evaluation["verdict"],
        "clarifying_questions": evaluation["clarifying_questions"],
    }

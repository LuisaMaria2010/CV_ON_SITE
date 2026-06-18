import asyncio
import azure.functions as func
import logging
import json
import re
import math
import os
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

try:
    from openai import AzureOpenAI as _AzureOpenAI
    from openai import OpenAI as _OpenAI
    _openai_available = True
except ImportError:
    _OpenAI = None
    _openai_available = False

from core.config import settings
from core.errors import InvalidInputError, FileTooLargeError

from infra.blob_storage import StorageService
from infra.backfill_enqueuer import BackfillEnqueuer
from infra.search_service import SearchService
from extraction.cache import TextCache
from db_data.pipeline import CVPipeline
from ingestion_triggers import bp as ingestion_bp
from services.search_handler import (
    build_odata_filter,
    build_odata_filter_relaxed,
    enrich_hits_with_match_features,
    rerank,
    normalise_search_request,
    resolve_index,
)
from persist.db import acquire_conn
from persist.repository import CandidateRepository

from utils.http_errors import http_error_handler

# Creazione dell'oggetto app principale
app = func.FunctionApp(
    http_auth_level=func.AuthLevel.FUNCTION
)
app.register_functions(ingestion_bp)

logger = logging.getLogger(__name__)

# Cold start dependency wiring
storage = StorageService()
cache = TextCache(storage)
pipeline = CVPipeline(cache)

# Singleton judge client — created once per Function App instance
_judge_client: Any = None
_mc_matcher_client: Any = None
JUDGE_DEFAULT_TIMEOUT = getattr(settings, "judge_timeout_seconds", 10)


def _build_processing_message(*, blob_name: str, last_modified: str | None = None) -> dict:
    filename = blob_name.split("/")[-1]
    return {
        "blob": f"{settings.storage_container_incoming}/{blob_name}",
        "filename": filename,
        "source_path": f"/{settings.storage_container_incoming}/{blob_name}",
        "last_modified": last_modified or datetime.now(timezone.utc).isoformat(),
        "correlation_id": f"blob-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8]}",
    }


def _parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    # Accept native booleans
    if isinstance(raw, bool):
        return raw
    # Accept numeric truthy/falsy values
    if isinstance(raw, (int, float)):
        return bool(raw)
    # Fallback to string parsing for form/query values
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    raise InvalidInputError(f"Invalid boolean value: {raw}")


def _parse_int(raw: str | None, default: int) -> int:
    if raw is None:
        return default
    if isinstance(raw, str) and raw.strip() == "":
        return default
    try:
        if isinstance(raw, (int, float)):
            value = int(raw)
        else:
            value = int(str(raw).strip())
    except Exception as exc:
        raise InvalidInputError(f"Invalid integer value: {raw}") from exc

    if value <= 0:
        raise InvalidInputError("max_items must be > 0")
    return value


def _parse_float(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, str) and raw.strip() == "":
        return None
    try:
        return float(raw)
    except Exception as exc:
        raise InvalidInputError(f"Invalid number value: {raw}") from exc


def _body_params(req: func.HttpRequest) -> dict:
    body = req.get_body()
    if not body:
        return {}

    try:
        payload = json.loads(body.decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _payload_from_query(req: func.HttpRequest, param_name: str = "payload_json") -> dict:
    raw = req.params.get(param_name)
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


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


def _build_candidate_from_chunks(
    document_id: str,
    top_chunks: list[dict[str, Any]],
    all_chunks: list[dict[str, Any]],
) -> dict[str, Any]:
    primary = top_chunks[0] if top_chunks else (all_chunks[0] if all_chunks else {})

    all_skill_values: list[str] = []
    all_cert_values: list[str] = []
    for chunk in [*top_chunks, *all_chunks]:
        all_skill_values.extend([_safe_str(v).lower() for v in (chunk.get("skills") or []) if _safe_str(v)])
        all_cert_values.extend([_safe_str(v) for v in (chunk.get("certifications") or []) if _safe_str(v)])

    aggregated_skills = sorted(set(v for v in all_skill_values if v))
    aggregated_certs = sorted(set(v for v in all_cert_values if v))

    semantic_score = max((_to_float(c.get("semantic_score"), 0.0) for c in top_chunks), default=0.0)
    vec_score = max((_to_float(c.get("vec_score"), 0.0) for c in top_chunks), default=0.0)
    lex_score = max((_to_float(c.get("lex_score"), 0.0) for c in top_chunks), default=0.0)
    candidate_score = max((_to_float(c.get("score"), 0.0) for c in top_chunks), default=0.0)

    availability_days = _extract_availability_days(
        _first_non_empty(
            primary.get("availability_days"),
            primary.get("availability"),
        )
    )

    evidence = _aggregate_semantic_evidence(top_chunks, all_chunks, aggregated_skills)

    return {
        "id": primary.get("id"),
        "candidate_id": _safe_str(_first_non_empty(primary.get("candidate_id"), document_id)),
        "document_id": document_id,
        "full_name": _first_non_empty(primary.get("full_name"), primary.get("name")),
        "name": _first_non_empty(primary.get("name"), primary.get("full_name")),
        "role": primary.get("role"),
        "location": primary.get("location"),
        "skills": aggregated_skills,
        "certifications": aggregated_certs,
        "seniority": primary.get("seniority"),
        "experience_years": primary.get("experience_years"),
        "language": primary.get("language"),
        "availability": primary.get("availability"),
        "availability_days": availability_days,
        "version": primary.get("version"),
        "source_path": primary.get("source_path"),
        "semantic_score": semantic_score,
        "vec_score": vec_score,
        "lex_score": lex_score,
        "score": round(candidate_score, 6),
        "semantic_evidence": evidence,
        "highlights": primary.get("highlights") or {},
    }


async def _aggregate_top_candidates(
    *,
    search: SearchService,
    reranked_chunks: list[dict[str, Any]],
    index_name: str,
    candidate_top_k: int,
) -> list[dict[str, Any]]:
    chunks_by_doc: dict[str, list[dict[str, Any]]] = {}
    for chunk in reranked_chunks:
        doc_id = _safe_str(_first_non_empty(chunk.get("document_id"), chunk.get("id")))
        if not doc_id:
            continue
        chunks_by_doc.setdefault(doc_id, []).append(chunk)

    if not chunks_by_doc:
        return []

    for doc_chunks in chunks_by_doc.values():
        doc_chunks.sort(key=lambda c: _to_float(c.get("score"), 0.0), reverse=True)

    ranked_docs = sorted(
        chunks_by_doc.items(),
        key=lambda it: _to_float(it[1][0].get("score"), 0.0),
        reverse=True,
    )

    top_doc_ids = [doc_id for doc_id, _ in ranked_docs[:candidate_top_k]]
    all_chunks_by_doc = await search.load_chunks_for_candidates(
        top_doc_ids,
        index_name=index_name,
        per_candidate_limit=40,
    )

    aggregated: list[dict[str, Any]] = []
    for doc_id in top_doc_ids:
        top_chunks = chunks_by_doc.get(doc_id, [])
        all_chunks = all_chunks_by_doc.get(doc_id, [])
        aggregated.append(_build_candidate_from_chunks(doc_id, top_chunks, all_chunks))

    return aggregated

async def _run_search_pipeline(payload: dict) -> dict:
    p = normalise_search_request(payload)

    if not p["query"] and not p["skills"] and not p["role"]:
        raise InvalidInputError("At least one of 'query', 'skills' or 'role' is required")

    index_name = resolve_index(p["subco"])

    odata_filter = build_odata_filter(
        skills=p["skills"],
        seniority=p["seniority_explicit"],
        min_experience_years=p["min_experience_years"],
        max_experience_years=p["max_experience_years"],
        language=p["language"],
        availability_required=False,
    )

    lexical_query = " ".join(filter(None, [
        p["role"] or "",
        " ".join(p["skills"]),
    ])).strip()

    semantic_query = (p["query"] or "").strip()

    embedding: list[float] | None = None
    if p["hybrid"] and semantic_query:
        try:
            from infra.llm_client import get_embedding_client
            emb_client = get_embedding_client()
            embedding = await emb_client.aembed_query(semantic_query)
        except Exception:
            logger.exception("Embedding generation failed, falling back to lexical-only search")

    search = SearchService()
    candidate_top_k = min(6, max(10, p["top"]))
    chunk_top_k = max(candidate_top_k * 10, p["top"] * 5)

    raw_hits = await search.search_chunks(
        query=lexical_query or semantic_query or "*",
        odata_filter=odata_filter,
        embedding=embedding,
        top=max(p["top"], chunk_top_k),
        index_name=index_name,
    )

    reranked_chunks = rerank(
        raw_hits,
        query_skills=p["skills"],
        query_role=p["role"],
        query_location=p["location"],
        top=chunk_top_k,
    )
    hits = await _aggregate_top_candidates(
        search=search,
        reranked_chunks=reranked_chunks,
        index_name=index_name,
        candidate_top_k=candidate_top_k,
    )

    relaxed = False
    relaxed_criteria = list(p.get("relaxed_criteria") or [])
    suggestions: list[str] = []
    fallback_min = math.ceil(p["top"] * settings.search_fallback_threshold)

    if len(hits) < fallback_min and p["skills"]:
        relaxed = True
        if "skills" not in relaxed_criteria:
            relaxed_criteria.append("skills")
        relaxed_lexical_query = (p["role"] or "").strip()
        relaxed_semantic_query = (p["query"] or "").strip()
        relaxed_query = relaxed_lexical_query or relaxed_semantic_query or "*"
        relaxed_filter = build_odata_filter_relaxed(
            seniority=p["seniority_explicit"],
            min_experience_years=p["min_experience_years"],
            max_experience_years=p["max_experience_years"],
            language=p["language"],
        )

        relaxed_embedding: list[float] | None = None
        if p["hybrid"] and relaxed_semantic_query:
            try:
                from infra.llm_client import get_embedding_client
                emb_client = get_embedding_client()
                relaxed_embedding = await emb_client.aembed_query(relaxed_semantic_query)
            except Exception:
                logger.exception("Relaxed embedding generation failed, falling back to lexical-only search")

        relaxed_hits = await search.search_chunks(
            query=relaxed_query,
            odata_filter=relaxed_filter,
            embedding=relaxed_embedding,
            top=max(chunk_top_k, fallback_min * 6),
            index_name=index_name,
        )
        relaxed_reranked_chunks = rerank(
            relaxed_hits,
            query_skills=p["skills"],
            query_role=p["role"],
            query_location=p["location"],
            top=chunk_top_k,
        )
        relaxed_candidates = await _aggregate_top_candidates(
            search=search,
            reranked_chunks=relaxed_reranked_chunks,
            index_name=index_name,
            candidate_top_k=candidate_top_k,
        )
        existing_ids = {_safe_str(h.get("document_id")) for h in hits}
        for h in relaxed_candidates:
            doc_id = _safe_str(h.get("document_id"))
            if doc_id and doc_id not in existing_ids:
                h["is_relaxed_result"] = True
                hits.append(h)
                existing_ids.add(doc_id)
        hits = hits[:candidate_top_k]
        suggestions = [
            f"{s} (not found as strict requirement, showing partial matches)"
            for s in p["skills"]
        ]

    hits = enrich_hits_with_match_features(
        hits,
        query_skills=p["skills"],
        query_role=p["role"],
        query_location=p["location"],
        query_language=p["language"],
        query_seniority=p["seniority"],
        query_availability_required=p["availability_required"],
        work_mode=p["work_mode"],
        relaxed_criteria=relaxed_criteria,
    )

    constrained_hits, ignored_constraints = _apply_optional_constraints(
        hits,
        query_location=p["location"],
        work_mode=p["work_mode"],
        availability_date=p.get("availability_date"),
        availability_days=p.get("availability_days"),
    )
    if ignored_constraints:
        relaxed = True
        for constraint in ignored_constraints:
            if constraint not in relaxed_criteria:
                relaxed_criteria.append(constraint)
            suggestions.append(
                f"{constraint} (no strict matches found, constraint ignored)"
            )
    hits = constrained_hits

    logger.info(
        "Search completed query=%r index=%s hits=%s relaxed=%s hybrid=%s",
        p["query"], index_name, len(hits), relaxed, p["hybrid"],
    )

    return {
        "hits": hits,
        "meta": {
            "total": len(hits),
            "top": p["top"],
            "relaxed": relaxed,
            "relaxed_criteria": relaxed_criteria,
            "hybrid": p["hybrid"],
            "work_mode": p["work_mode"],
            "ignored_constraints": ignored_constraints,
            "index": index_name,
        },
        "suggestions": suggestions,
    }

def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _lower_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip().lower() for v in value if str(v).strip()]
    if isinstance(value, str):
        parts = [p.strip().lower() for p in value.split(",")]
        return [p for p in parts if p]
    return []


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _candidate_fields_for_evaluator(candidate: dict[str, Any]) -> dict[str, Any]:
    """Keep only candidate fields consumed by deterministic evaluator logic."""
    allowed_keys = {
        "id",
        "candidate_id",
        "document_id",
        "full_name",
        "name",
        "role",
        "location",
        "skills",
        "seniority",
        "language",
        "availability_days",
        "semantic_score",
        "source_path",
        "match_features",
        "relaxed_criteria",
    }
    return {k: v for k, v in candidate.items() if k in allowed_keys}


def _extract_evaluator_candidates(payload: dict) -> list[dict[str, Any]]:
    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        return [
            _candidate_fields_for_evaluator(c)
            for c in candidates
            if isinstance(c, dict)
        ]

    search_response = payload.get("search_response")
    if isinstance(search_response, dict):
        if isinstance(search_response.get("hits"), list):
            return [
                _candidate_fields_for_evaluator(c)
                for c in search_response["hits"]
                if isinstance(c, dict)
            ]
        data = search_response.get("data")
        if isinstance(data, dict) and isinstance(data.get("hits"), list):
            return [
                _candidate_fields_for_evaluator(c)
                for c in data["hits"]
                if isinstance(c, dict)
            ]

    return []


def _evaluate_candidate(
    candidate: dict[str, Any],
    interpreted_request: dict[str, Any],
    fallback_relaxed_criteria: list[str],
) -> dict[str, Any]:
    req_skills = _lower_list(interpreted_request.get("skills"))
    req_role = _safe_str(interpreted_request.get("role")).lower()
    req_location = _safe_str(interpreted_request.get("location")).lower()
    req_seniority = _safe_str(interpreted_request.get("seniority")).lower()
    req_language = _safe_str(_first_non_empty(interpreted_request.get("language"), None)).lower()
    if not req_language:
        req_languages = _lower_list(interpreted_request.get("languages"))
        req_language = req_languages[0] if req_languages else ""
    req_availability = bool(interpreted_request.get("availability_required", False))
    work_mode = _safe_str(interpreted_request.get("work_mode")).lower() or "unknown"

    cand_skills = _lower_list(candidate.get("skills"))
    cand_availability = candidate.get("availability_days")
    semantic_score_raw = max(0.0, _to_float(candidate.get("semantic_score"), 0.0))
    semantic_retrieval_score = semantic_score_raw / (1.0 + semantic_score_raw) if semantic_score_raw > 0 else 0.0

    match_features = candidate.get("match_features") if isinstance(candidate.get("match_features"), dict) else None

    weights: dict[str, float] = {}
    component_scores: dict[str, float] = {}
    reasons: list[str] = []
    strengths: list[str] = []
    weaknesses: list[str] = []
    missing_requirements: list[str] = []
    diagnostic_warnings: list[str] = []

    base_weights = {
        "skills": 0.50,
        "role": 0.25,
        "location": 0.10,
        "seniority": 0.05,
        "language": 0.05,
        "availability": 0.05,
        "semantic_retrieval": 0.05,
    }

    candidate_id_for_logs = _safe_str(
        _first_non_empty(
            candidate.get("candidate_id"),
            candidate.get("document_id"),
            candidate.get("id"),
            candidate.get("name"),
            "unknown",
        )
    )

    def _score_from_match_features(dimension: str, required: bool) -> float:
        if not required:
            return 0.0
        if not match_features:
            warning = f"match_features missing: {dimension} score defaulted to 0"
            diagnostic_warnings.append(warning)
            logger.warning("%s candidate_id=%s", warning, candidate_id_for_logs)
            return 0.0
        block = match_features.get(dimension)
        if not isinstance(block, dict):
            warning = f"match_features.{dimension} missing: score defaulted to 0"
            diagnostic_warnings.append(warning)
            logger.warning("%s candidate_id=%s", warning, candidate_id_for_logs)
            return 0.0
        if block.get("applicable") is False:
            warning = f"match_features.{dimension}.applicable=false while required: score defaulted to 0"
            diagnostic_warnings.append(warning)
            logger.warning("%s candidate_id=%s", warning, candidate_id_for_logs)
            return 0.0
        raw_score = block.get("score")
        if not isinstance(raw_score, (int, float)):
            warning = f"match_features.{dimension}.score missing: defaulted to 0"
            diagnostic_warnings.append(warning)
            logger.warning("%s candidate_id=%s", warning, candidate_id_for_logs)
            return 0.0
        return max(0.0, min(1.0, float(raw_score)))

    if req_skills:
        skills_block = match_features.get("skills") if match_features and isinstance(match_features.get("skills"), dict) else {}
        exact = _lower_list(skills_block.get("matched"))
        semantic = _lower_list(skills_block.get("semantic_matches"))
        matched_count = len(set(exact) | set(semantic))
        skill_score = _score_from_match_features("skills", required=True)
        weights["skills"] = base_weights["skills"]
        component_scores["skills"] = skill_score
        if skill_score >= 0.7:
            reasons.append("competenze principali in linea con richiesta")
            if matched_count > 0:
                reasons.append(f"skill coperte da match_features: {matched_count}")
            strengths.append("copertura skill elevata")
        elif skill_score > 0:
            reasons.append("copertura skill parziale")
            if matched_count > 0:
                reasons.append(f"skill coperte da match_features: {matched_count}")
            weaknesses.append("non tutte le skill richieste sono presenti")
        else:
            weaknesses.append("assenza delle skill chiave richieste")
            missing_requirements.append("skill principali")

    if req_role:
        role_score = _score_from_match_features("role", required=True)

        weights["role"] = base_weights["role"]
        component_scores["role"] = role_score
        if role_score >= 0.7:
            reasons.append("ruolo compatibile")
        elif role_score > 0:
            reasons.append("ruolo parzialmente compatibile")
            weaknesses.append("ruolo solo parzialmente allineato")
        else:
            weaknesses.append("ruolo non allineato")
            missing_requirements.append("ruolo")

    if req_location and work_mode in {"onsite", "hybrid", "unknown"}:
        location_score = _score_from_match_features("location", required=True)
        weights["location"] = base_weights["location"]
        component_scores["location"] = location_score
        if location_score >= 1.0:
            reasons.append("location compatibile")
        else:
            weaknesses.append("location non compatibile")
            if location_score == 0:
                missing_requirements.append("location")

    if req_seniority:
        seniority_score = _score_from_match_features("seniority", required=True)
        weights["seniority"] = base_weights["seniority"]
        component_scores["seniority"] = seniority_score
        if seniority_score == 0:
            missing_requirements.append("seniority")

    if req_language:
        language_score = _score_from_match_features("language", required=True)
        weights["language"] = base_weights["language"]
        component_scores["language"] = language_score
        if language_score == 0:
            missing_requirements.append("lingua")

    if req_availability:
        availability_score = _score_from_match_features("availability", required=True)
        weights["availability"] = base_weights["availability"]
        component_scores["availability"] = availability_score
        if availability_score == 0:
            missing_requirements.append("disponibilita'")

    if semantic_retrieval_score > 0:
        # Small retrieval-based signal to account for semantic ranking quality without dominating match_features.
        weights["semantic_retrieval"] = base_weights["semantic_retrieval"]
        component_scores["semantic_retrieval"] = semantic_retrieval_score
        if semantic_retrieval_score >= 0.7:
            reasons.append("forte segnale di rilevanza semantica")

    total_weight = sum(weights.values())
    if total_weight <= 0:
        match_score = 0.0
    else:
        weighted_sum = sum(component_scores.get(k, 0.0) * w for k, w in weights.items())
        match_score = weighted_sum / total_weight

    if match_score >= 0.8:
        match_type = "strong"
    elif match_score >= 0.6:
        match_type = "good"
    else:
        match_type = "weak"

    candidate_name = _safe_str(_first_non_empty(candidate.get("name"), candidate.get("full_name"), "Unknown"))
    candidate_role = _safe_str(candidate.get("role")) or None
    candidate_location = _safe_str(candidate.get("location")) or None

    if not reasons:
        reasons.append("coerenza limitata sui criteri disponibili")
    if not strengths and match_score >= 0.7:
        strengths.append("profilo globalmente coerente")
    if not weaknesses and match_score < 0.7:
        weaknesses.append("allineamento parziale ai requisiti")

    return {
        "candidate_id": _safe_str(_first_non_empty(candidate.get("candidate_id"), candidate.get("document_id"), candidate.get("id"), candidate_name)),
        "name": candidate_name,
        "role": candidate_role,
        "location": candidate_location,
        "skills": cand_skills,
        "seniority": _safe_str(candidate.get("seniority")) or None,
        "availability_days": cand_availability,
        "language": _safe_str(candidate.get("language")) or None,
        "match_features": match_features or {},
        "source_path": candidate.get("source_path"),
        "match_score": round(float(match_score), 4),
        "match_type": match_type,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "missing_requirements": list(dict.fromkeys(missing_requirements)),
        "matched_on": _lower_list(match_features.get("matched_on")) if match_features else [],
        "diagnostic_warnings": diagnostic_warnings
    }
def _level_from_score(score: float | None) -> str:
    if score is None or score <= 0:
        return "unknown"
    if score >= 0.75:
        return "high"
    if score >= 0.45:
        return "medium"
    return "low"


def _aggregate_coverage(evaluated_candidates: list[dict[str, Any]]) -> dict[str, str]:
    if not evaluated_candidates:
        return {
            "skills": "unknown",
            "role": "unknown",
            "location": "unknown",
            "seniority": "unknown",
            "language": "unknown",
            "availability": "unknown",
        }

    top = evaluated_candidates[0]
    components = top.get("_component_scores", {})
    return {
        "skills": _level_from_score(components.get("skills")),
        "role": _level_from_score(components.get("role")),
        "location": _level_from_score(components.get("location")),
        "seniority": _level_from_score(components.get("seniority")),
        "language": _level_from_score(components.get("language")),
        "availability": _level_from_score(components.get("availability")),
    }


def _extract_json_safe(raw: str) -> dict[str, Any] | None:
    """Parsing JSON robusto — stesso approccio di run_evaluation_classifier._extract_json."""
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    s, e = raw.find("{"), raw.rfind("}")
    if s == -1 or e <= s:
        return None
    try:
        parsed = json.loads(raw[s : e + 1])
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _settings_value(*keys: str, default: str = "") -> str:
    """Read setting from env first, then local.settings.json fallback."""
    for key in keys:
        value = os.environ.get(key)
        if value is not None and str(value).strip() != "":
            return str(value).strip()

    try:
        cfg_path = "local.settings.json"
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            values = payload.get("Values") if isinstance(payload, dict) else {}
            if isinstance(values, dict):
                for key in keys:
                    value = values.get(key)
                    if value is not None and str(value).strip() != "":
                        return str(value).strip()
    except Exception:
        pass

    return default


def _foundry_project_endpoint() -> str:
    explicit = _settings_value(
        "AZURE_AI_PROJECT_ENDPOINT",
        "FOUNDRY_PROJECT_ENDPOINT",
        "AZURE_FOUNDRY_PROJECT_ENDPOINT",
        default="",
    )
    if explicit:
        return explicit.rstrip("/")

    endpoint = _settings_value("FOUNDRY_ENDPOINT", default="").rstrip("/")
    project = _settings_value(
        "FOUNDRY_PROJECT",
        "AZURE_AI_PROJECT_NAME",
        "AZURE_FOUNDRY_PROJECT_NAME",
        default="",
    ).strip()
    if endpoint and project:
        return f"{endpoint}/api/projects/{project}"
    if endpoint and "/api/projects/" in endpoint:
        return endpoint
    return ""


def _response_to_plain_dict(response: Any) -> dict[str, Any]:
    if response is None:
        return {}

    try:
        if hasattr(response, "model_dump"):
            dumped = response.model_dump()
            if isinstance(dumped, dict):
                return dumped
    except Exception:
        pass

    try:
        if hasattr(response, "model_dump_json"):
            dumped_json = response.model_dump_json()
            parsed = json.loads(dumped_json)
            if isinstance(parsed, dict):
                return parsed
    except Exception:
        pass

    try:
        if isinstance(response, dict):
            return response
        raw = json.loads(str(response))
        if isinstance(raw, dict):
            return raw
    except Exception:
        pass

    return {}


def _responses_api_version(version: str | None, minimum: str = "2025-03-01-preview") -> str:
    """Ensure api-version is compatible with Azure OpenAI Responses API."""
    candidate = (version or "").strip()
    if not candidate:
        return minimum

    # Expected format is YYYY-MM-DD[-preview]. Compare by date part only.
    date_part = candidate[:10]
    if len(date_part) != 10 or date_part[4] != "-" or date_part[7] != "-":
        return minimum

    return candidate if date_part >= minimum[:10] else minimum


_foundry_retry_max_attempts = max(
    1,
    int(_settings_value("FOUNDRY_RETRY_MAX_ATTEMPTS", default="3") or "3"),
)


def _wait_for_foundry_slot(agent_name: str) -> None:
    """Hook per eventuale throttling locale/concurrency control."""
    _ = agent_name


def _is_foundry_rate_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "429" in text
        or "rate limit" in text
        or "too_many_requests" in text
        or "rate_limited" in text
    )


def _foundry_backoff_seconds(exc: Exception, attempt: int) -> float:
    text = str(exc)
    retry_after = re.search(r"retry[-_ ]?after\D*(\d+)", text, re.IGNORECASE)
    if retry_after:
        try:
            return max(0.5, float(retry_after.group(1)))
        except Exception:
            pass
    return min(8.0, 0.75 * (2 ** (attempt - 1)))


def _run_foundry_agent(
    *,
    agent_name: str,
    message: str,
    model_name: str | None = None,
    previous_response_id: str | None = None,
) -> Any:
    """Invoke Foundry modern agent via Responses API with agent_reference."""
    openai = _get_mc_matcher_client()
    if openai is None:
        has_api_key = bool(_settings_value("AZURE_OPENAI_KEY", default=settings.azure_openai_key or ""))
        has_project_endpoint = bool(_settings_value("AZURE_AI_PROJECT_ENDPOINT", default="").strip())
        raise InvalidInputError(
            "Foundry mc-matcher client not initialized. "
            f"openai_available={_openai_available}, "
            f"has_api_key={has_api_key}, "
            f"has_project_endpoint={has_project_endpoint}. "
            "Required env vars: AZURE_AI_PROJECT_ENDPOINT and AZURE_OPENAI_KEY."
        )

    kwargs: dict[str, Any] = {
        "extra_body": {
            "agent_reference": {
                "name": agent_name,
                "type": "agent_reference",
            }
        },
        "input": message,
    }
    if model_name:
        kwargs["model"] = model_name
    if previous_response_id:
        kwargs["previous_response_id"] = previous_response_id

    for attempt in range(1, _foundry_retry_max_attempts + 1):
        _wait_for_foundry_slot(agent_name)
        try:
            return openai.responses.create(**kwargs)
        except Exception as exc:
            if not _is_foundry_rate_limit_error(exc) or attempt >= _foundry_retry_max_attempts:
                raise

            backoff = _foundry_backoff_seconds(exc, attempt)
            logger.warning(
                "foundry_rate_limited agent=%s attempt=%s/%s backoff=%.2fs",
                agent_name,
                attempt,
                _foundry_retry_max_attempts,
                backoff,
            )
            time.sleep(backoff)

    raise RuntimeError("Foundry invocation exhausted retries")


def _get_mc_matcher_client() -> Any:
    """Create and cache Foundry Responses API client for mc-matcher wrapper."""
    global _mc_matcher_client
    if _mc_matcher_client is not None:
        return _mc_matcher_client

    if not _openai_available:
        logger.warning("mc-matcher client not initialized: openai package import failed")
        return None

    if _OpenAI is None:
        logger.warning("mc-matcher client not initialized: OpenAI client class unavailable")
        return None

    api_key = _settings_value("AZURE_OPENAI_KEY", default=settings.azure_openai_key or "")
    project_endpoint = _settings_value("AZURE_AI_PROJECT_ENDPOINT", default="").rstrip("/")
    if not api_key:
        logger.warning("mc-matcher client not initialized: missing AZURE_OPENAI_KEY")
        return None

    if not project_endpoint:
        logger.warning("mc-matcher client not initialized: missing AZURE_AI_PROJECT_ENDPOINT")
        return None

    try:
        _mc_matcher_client = _OpenAI(
            base_url=f"{project_endpoint.rstrip('/')}/openai/v1/",
            api_key=api_key,
        )
        logger.info("mc-matcher client initialized project_endpoint=%s", project_endpoint)
        return _mc_matcher_client
    except Exception as exc:
        logger.warning("Cannot create mc-matcher client: %s", exc)
        return None


def _get_judge_client() -> Any:
    """Restituisce il client Azure OpenAI per il judge (singleton per istanza)."""
    global _judge_client
    if _judge_client is not None:
        return _judge_client
    if not _openai_available:
        return None
    key = settings.azure_openai_key or ""
    if not key:
        return None
    try:
        _judge_client = _AzureOpenAI(
            azure_endpoint=settings.azure_openai_endpoint.rstrip("/"),
            api_version=settings.azure_openai_api_version,
            api_key=key,
        )
        logger.info("Judge client initialized endpoint=%s", settings.azure_openai_endpoint)
        return _judge_client
    except Exception as exc:
        logger.warning("Cannot create judge client: %s", exc)
        return None


def _summarize_semantic_evidence_text(evidence_chunks: list[str]) -> str | None:
    """Summarize semantic evidence in a short form (target 100-200 tokens)."""
    cleaned = [str(v).strip() for v in (evidence_chunks or []) if isinstance(v, str) and str(v).strip()]
    if not cleaned:
        return None

    client = _get_judge_client()
    if client is None:
        return None

    evidence_blob = "\n- " + "\n- ".join(cleaned[:8])
    system_msg = (
        "Sei un assistente che sintetizza evidence semantiche di CV. "
        "Produci un riassunto chiaro, fattuale e non inventare dati."
    )
    user_msg = (
        "Riassumi queste semantic evidence in italiano in 100-200 token, "
        "con focus su ruolo, competenze, seniority e disponibilita' quando presenti."
        f"\n\nEVIDENCE:{evidence_blob}"
    )

    try:
        resp = client.chat.completions.create(
            model=settings.azure_openai_model,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=180,
            temperature=0,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text or None
    except Exception as exc:
        logger.warning("Semantic evidence summary LLM call failed: %s", exc)
        return None

# ---------------------------------------------------------------------------
# Response Judger — compatibilità risposta/richiesta (solo coerenza, no ranking)
# ---------------------------------------------------------------------------

_RESPONSE_JUDGER_SYSTEM = """\
Sei un verificatore di coerenza del sistema MC Flash.
Il tuo UNICO compito e' verificare se la risposta finale fornita all'utente e' compatibile
con la richiesta originale.

NON devi:
- Rifare la ricerca o il ranking dei candidati
- Valutare le skill o la location dei profili
- Assegnare punteggi di match
- Aggiungere candidati non presenti nella risposta

Devi solo controllare:
- La risposta affronta la domanda posta?
- La risposta e' utile e non fuorviante rispetto alla richiesta?
- Ci sono contraddizioni evidenti tra richiesta e risposta?

Restituisci SOLO JSON valido, senza markdown o commenti.
"""

_RESPONSE_JUDGER_PROMPT = """\
RICHIESTA ORIGINALE DELL'UTENTE:
_ORIGINAL_REQUEST_

RISPOSTA FINALE GENERATA:
_FINAL_ANSWER_

Verifica se la risposta e' compatibile con la richiesta.

Restituisci esattamente questo JSON (nessun markdown):
{
    "compatible": true,
    "confidence": 0.9,
    "verdict": "ok|partial|mismatch",
    "issues": [],
    "notes": "stringa breve"
}

- compatible: true se la risposta e' accettabile, false se e' chiaramente incompatibile
- confidence: 0.0-1.0 quanto sei sicuro del giudizio
- verdict: "ok" (risposta coerente), "partial" (parzialmente coerente), "mismatch" (incompatibile)
- issues: lista di problemi specifici identificati (vuota se nessuno)
- notes: breve spiegazione (max 2 frasi)
"""


def _judge_response_compatibility(original_request: str, final_answer: str) -> dict[str, Any]:
    """
    Judge leggero LLM: verifica solo la compatibilita' risposta/richiesta.
    Non rifà il ranking ne' classifica candidati.
    Fallback deterministico (compatible=True) se LLM non disponibile.
    """
    _default = {
        "compatible": True,
        "confidence": 0.5,
        "verdict": "ok",
        "issues": [],
        "notes": "LLM judge non disponibile, risposta accettata per default",
    }

    client = _get_judge_client()
    if client is None:
        return _default

    user_msg = (
        _RESPONSE_JUDGER_PROMPT
        .replace("_ORIGINAL_REQUEST_", original_request)
        .replace("_FINAL_ANSWER_", final_answer[:3000])
    )

    try:
        resp = client.chat.completions.create(
            model=settings.azure_openai_model,
            messages=[
                {"role": "system", "content": _RESPONSE_JUDGER_SYSTEM},
                {"role": "user",   "content": user_msg},
            ],
            max_tokens=300,
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw = (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.warning("Response judger LLM call failed: %s", exc)
        return {**_default, "notes": "LLM judge fallito, risposta accettata per default"}

    parsed = _extract_json_safe(raw)
    if not parsed:
        return {**_default, "notes": "LLM judge ha restituito JSON malformato"}

    return {
        "compatible": bool(parsed.get("compatible", True)),
        "confidence": float(parsed.get("confidence", 0.5)),
        "verdict": str(parsed.get("verdict") or "ok"),
        "issues": list(parsed.get("issues") or []),
        "notes": str(parsed.get("notes") or ""),
    }


def _compute_recovery_signals(
    verdict: str,
    failure_coverage: dict[str, str],
    critical_gaps: list[str],
    relaxed_criteria: list[str],
    interpreted_request: dict[str, Any],
    original_request: str | None,
) -> dict[str, Any]:
    """
    Determina i segnali di recovery da restituire al classifier.

    Valori possibili:
    - failure_type: no_matches | poor_skill_coverage | location_mismatch |
                    seniority_mismatch | ambiguous_query | invalid_input | none
    - recovery_strategy: RETURN_ANSWER | RELAX_AND_RETRY | REWRITE_QUERY |
                         ASK_USER_CLARIFICATION | RETURN_PARTIAL_ANSWER | SAFE_REFUSAL
    """
    failure_type = "none"
    recovery_strategy = "RETURN_ANSWER"
    needs_clarification = False
    clarifying_questions: list[str] = []
    improved_queries: list[str] = []
    missing_entities: list[str] = []

    if verdict == "invalid_input":
        return {
            "failure_type": "invalid_input",
            "recovery_strategy": "SAFE_REFUSAL",
            "needs_clarification": False,
            "clarifying_questions": [],
            "improved_queries": [],
            "missing_entities": [],
        }

    already_relaxed = bool(relaxed_criteria)

    if verdict in ("no_match", "weak_match"):
        # Determina quale dimensione ha fallito
        skills_cov = failure_coverage.get("skills", "unknown")
        loc_cov = failure_coverage.get("location", "unknown")
        sen_cov = failure_coverage.get("seniority", "unknown")

        if skills_cov == "low":
            failure_type = "poor_skill_coverage"
        elif loc_cov == "low":
            failure_type = "location_mismatch"
        elif sen_cov == "low":
            failure_type = "seniority_mismatch"
        else:
            failure_type = "no_matches"

        if verdict == "no_match":
            if already_relaxed:
                recovery_strategy = "ASK_USER_CLARIFICATION"
                needs_clarification = True
            else:
                recovery_strategy = "RELAX_AND_RETRY"
        else:  # weak_match
            recovery_strategy = "RETURN_PARTIAL_ANSWER" if already_relaxed else "RELAX_AND_RETRY"

    # Segnali query mancanti
    if not interpreted_request.get("skills"):
        missing_entities.append("skills")
    if not interpreted_request.get("role"):
        missing_entities.append("role")
    if not interpreted_request.get("location") and interpreted_request.get("work_mode") not in ("remote",):
        missing_entities.append("location")

    # Query migliorate per retry automatico
    if recovery_strategy in ("RELAX_AND_RETRY", "REWRITE_QUERY"):
        base = _safe_str(
            interpreted_request.get("query")
            or interpreted_request.get("role")
            or original_request
            or ""
        )
        skills = interpreted_request.get("skills") or []
        if base:
            improved_queries.append(base)
        if skills:
            improved_queries.append(" ".join(skills[:3]))

    # Suggerimento operativo quando conviene ritentare con criteri rilassati.
    if recovery_strategy == "RELAX_AND_RETRY":
        clarifying_questions.append(
            "Posso rilanciare la ricerca allargando i criteri (es. skill correlate o vincoli meno rigidi). Confermi?"
        )

    # Domande chiarificatrici
    if needs_clarification or recovery_strategy == "ASK_USER_CLARIFICATION":
        if failure_type == "poor_skill_coverage":
            clarifying_questions.append(
                "Puoi specificare le skill tecniche prioritarie che cerchi?"
            )
        elif failure_type == "location_mismatch":
            clarifying_questions.append(
                "Il profilo deve essere in sede o accetti anche modalita' remota?"
            )
        elif failure_type == "seniority_mismatch":
            clarifying_questions.append(
                "Puoi indicare gli anni di esperienza o la seniority che cerchi?"
            )
        else:
            clarifying_questions.append(
                "Puoi fornire maggiori dettagli sulla figura professionale che cerchi?"
            )

    return {
        "failure_type": failure_type,
        "recovery_strategy": recovery_strategy,
        "needs_clarification": needs_clarification,
        "clarifying_questions": clarifying_questions,
        "improved_queries": list(dict.fromkeys(q for q in improved_queries if q)),
        "missing_entities": missing_entities,
    }


def _build_match_judgement_payload(
    evaluated_candidates: list[dict[str, Any]],
    interpreted_request: dict[str, Any],
    relaxed_criteria: list[str],
    original_request: str,
) -> dict[str, Any]:
    if not evaluated_candidates:
        recovery = _compute_recovery_signals(
            verdict="invalid_input",
            failure_coverage={},
            critical_gaps=["missing_candidates"],
            relaxed_criteria=relaxed_criteria,
            interpreted_request=interpreted_request,
            original_request=original_request,
        )
        return {
            "verdict": "invalid_input",
            "confidence": 0.1,
            "summary": "Input non valido: nessun candidato valutabile.",
            "coverage": {
                "skills": "unknown",
                "role": "unknown",
                "location": "unknown",
                "seniority": "unknown",
                "language": "unknown",
                "availability": "unknown",
            },
            "critical_gaps": ["missing_candidates"],
            "failure_type": recovery["failure_type"],
            "recovery_strategy": recovery["recovery_strategy"],
            "needs_clarification": recovery["needs_clarification"],
            "clarifying_questions": recovery["clarifying_questions"],
        }

    top_score = float(evaluated_candidates[0].get("match_score") or 0.0)
    if top_score >= 0.8:
        verdict = "strong_match"
    elif top_score >= 0.6:
        verdict = "partial_match"
    elif top_score >= 0.4:
        verdict = "weak_match"
    else:
        verdict = "no_match"

    confidence = round(min(1.0, 0.35 + 0.5 * top_score + min(len(evaluated_candidates), 5) * 0.03), 4)

    critical_gaps: list[str] = []
    top_missing = evaluated_candidates[0].get("missing_requirements", [])
    if isinstance(top_missing, list):
        critical_gaps.extend(top_missing[:3])

    if "_component_scores" in evaluated_candidates[0]:
        coverage = _aggregate_coverage(evaluated_candidates)
    else:
        coverage = {
            "skills": "unknown",
            "role": "unknown",
            "location": "unknown",
            "seniority": "unknown",
            "language": "unknown",
            "availability": "unknown",
        }

    recovery = _compute_recovery_signals(
        verdict=verdict,
        failure_coverage=coverage,
        critical_gaps=list(dict.fromkeys(critical_gaps)),
        relaxed_criteria=relaxed_criteria,
        interpreted_request=interpreted_request,
        original_request=original_request,
    )

    return {
        "verdict": verdict,
        "confidence": confidence,
        "summary": f"Valutati {len(evaluated_candidates)} candidati. Miglior score: {round(top_score, 2)}.",
        "coverage": coverage,
        "critical_gaps": list(dict.fromkeys(critical_gaps)),
        "failure_type": recovery["failure_type"],
        "recovery_strategy": recovery["recovery_strategy"],
        "needs_clarification": recovery["needs_clarification"],
        "clarifying_questions": recovery["clarifying_questions"],
        "improved_queries": recovery["improved_queries"],
        "missing_entities": recovery["missing_entities"],
    }


def _evaluate_match_payload(payload: dict[str, Any]) -> dict[str, Any]:
    interpreted_request = payload.get("interpreted_request")
    if not isinstance(interpreted_request, dict):
        interpreted_request = payload.get("classification") if isinstance(payload.get("classification"), dict) else {}

    candidates = _extract_evaluator_candidates(payload)
    if not isinstance(candidates, list):
        candidates = []

    logger.info(
        "evaluator candidate keys=%s",
        list(candidates[0].keys()) if candidates and isinstance(candidates[0], dict) else [],
    )

    relaxed_criteria = _lower_list(payload.get("relaxed_criteria"))

    if not candidates:
        return {
            "best_candidates": [],
            "candidate_evaluations": [],
            "meta": {
                "total": 0,
                "source": "deterministic_match_evaluator",
            },
        }

    evaluated = [
        _evaluate_candidate(candidate, interpreted_request, relaxed_criteria)
        for candidate in candidates
        if isinstance(candidate, dict)
    ]
    evaluated.sort(key=lambda c: c.get("match_score", 0.0), reverse=True)

    evaluated_public = []
    for c in evaluated:
        public = dict(c)
        public.pop("_component_scores", None)
        evaluated_public.append(public)

    best_candidates = [
        _safe_str(_first_non_empty(c.get("name"), c.get("candidate_id"), "Unknown"))
        for c in evaluated_public[:3]
    ]

    return {
        "best_candidates": best_candidates,
        "candidate_evaluations": evaluated_public,
        "judgement": _build_match_judgement_payload(
            evaluated_candidates=evaluated,
            interpreted_request=interpreted_request,
            relaxed_criteria=relaxed_criteria,
            original_request=_safe_str(payload.get("original_request")),
        ),
        "meta": {
            "total": len(evaluated),
            "source": "deterministic_match_evaluator",
        },
    }


@app.blob_trigger(
    arg_name="inputblob",
    path="incoming-cv/{name}",
    connection="AzureWebJobsStorage",
)
@app.queue_output(
    arg_name="queueoutput",
    queue_name="%DOCUMENT_PROCESSING_QUEUE_NAME%",
    connection="AzureWebJobsStorage",
)
def enqueue_incoming_cv_blob(inputblob: func.InputStream, queueoutput: func.Out[str]):
    """
    Trigger automatico per nuovi blob caricati in incoming-cv.

    Responsabilita':
    - ascolta upload sul container incoming-cv
    - costruisce il messaggio standard di processing
    - accoda su document-processing

    Non esegue parsing o estrazione CV.
    """
    blob_name = inputblob.name.split("/", 1)[-1]
    properties = getattr(inputblob, "properties", None)
    last_modified = None
    if properties is not None:
        last_modified_value = getattr(properties, "last_modified", None)
        if last_modified_value is not None:
            last_modified = last_modified_value.astimezone(timezone.utc).isoformat()

    message = _build_processing_message(
        blob_name=blob_name,
        last_modified=last_modified,
    )
    queueoutput.set(json.dumps(message))

    logger.info(
        "Incoming CV blob enqueued blob=%s queue=%s correlation_id=%s",
        message["blob"],
        settings.document_processing_queue_name,
        message["correlation_id"],
    )

# =========================================================
# HTTP Function: Extract CV
# =========================================================

@app.route(route="extract", methods=["POST"])
@http_error_handler
async def extract(req: func.HttpRequest):
    """
    POST /api/extract

    Input supportati:
    - raw bytes (PDF / DOCX / TXT)
    - multipart/form-data con campo "file"

    Output:
    - dict dominio CVExtraction
    """
    
    # Recupero body (raw o multipart)
    file_bytes = None
    upload_filename = None
    content_type = req.headers.get("content-type", "").lower()
    
    if "multipart/form-data" in content_type:
        
        files = req.files
        if not files or "file" not in files:
            raise InvalidInputError("Missing 'file' field in multipart request")
        
        uploaded_file = files["file"]
        upload_filename = uploaded_file.filename
        file_bytes = uploaded_file.read()
    else:
        # Raw bytes
        file_bytes = req.get_body()

    if not file_bytes:
        raise InvalidInputError("Empty file")

    if not upload_filename:
        upload_filename = f"upload-{uuid4().hex}.bin"

    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", upload_filename)
    blob_name = f"{datetime.now(timezone.utc).strftime('%Y/%m/%d')}/{uuid4().hex}_{safe_name}"

    await storage.upload_bytes(
        data=file_bytes,
        blob_name=blob_name,
        container=settings.storage_container_original_uploads,
    )

    logger.info(
        "Original CV upload stored container=%s blob=%s",
        settings.storage_container_original_uploads,
        blob_name,
    )
    
    # Validazione dimensione
    max_size_bytes = settings.max_file_size_mb * 1024 * 1024
    if len(file_bytes) > max_size_bytes:
        raise FileTooLargeError(
            f"File too large: {len(file_bytes)} bytes. Max: {max_size_bytes}"
        )
    
    # Pipeline dominio (parsing + LLM)
    extraction = await pipeline.process(file_bytes)

    # Rimuoviamo temporaneamente i dati sensibili dall'output pubblico
    response_payload = extraction.model_dump()
    for field in ("email", "phone", "age"):
        response_payload.pop(field, None)
    
    # Ritorniamo dict puro (decoratore gestisce envelope)
    return response_payload


@app.route(route="search", methods=["POST"])
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


@app.route(route="searcher-wrapper", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
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
                "index": resolve_index(str(search_request.get("subco") or "").strip().lower() or None),
                "skipped": "availability_only",
            },
            "suggestions": [],
        }

    classification_fields = {
        k: v
        for k, v in payload.items()
        if k != "search_request"
    }

    if isinstance(search_response.get("hits"), list) and search_response["hits"]:
        logger.info(
            "search hit keys=%s",
            list(search_response["hits"][0].keys()),
        )

        evidence_chunks = [
            _safe_str(hit.get("semantic_evidence"))
            for hit in search_response["hits"][:5]
            if _safe_str(hit.get("semantic_evidence"))
        ]
        if evidence_chunks:
            summary = await asyncio.to_thread(_summarize_semantic_evidence_text, evidence_chunks)
            if summary:
                for hit in search_response["hits"]:
                    if isinstance(hit, dict):
                        hit["semantic_evidence"] = summary

    return {
        "classification": classification_fields,
        "search_request": search_request,
        "search_response": search_response,
    }


@app.route(route="match-evaluator-wrapper", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
@http_error_handler
async def match_evaluator_wrapper(req: func.HttpRequest):
    """
    POST /api/match-evaluator-wrapper

    Wrapper deterministico per Match Evaluator:
    - accetta payload con original_request + interpreted_request + candidates
    - fallback compatibile: candidates da search_response.hits o search_response.data.hits
    - valutazione completamente deterministica (no LLM, no latenza aggiuntiva)
    """
    payload = _body_params(req)
    if not payload:
        payload = _payload_from_query(req)
    if not payload:
        raise InvalidInputError("Missing or invalid JSON body")

    return _evaluate_match_payload(payload)


@app.route(route="mc-matcher-wrapper", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
@http_error_handler
async def mc_matcher_wrapper(req: func.HttpRequest):
    """
    POST /api/mc-matcher-wrapper

    Wrapper riusabile per chiamare l'agente Foundry `mc-matcher` da sistemi esterni.
    Pensato per essere consumato come tool OpenAPI da altri agenti.
    """
    payload = _body_params(req)
    if not payload:
        payload = _payload_from_query(req)
    if not payload:
        raise InvalidInputError("Missing or invalid JSON body")

    user_request = _safe_str(payload.get("user_request"))
    if not user_request:
        user_request = _safe_str(_first_non_empty(payload.get("query"), payload.get("original_request")))
    if not user_request:
        raise InvalidInputError("'user_request' is required")

    context = payload.get("context")
    if context is None and isinstance(payload.get("search_request"), dict):
        context = {"search_request": payload.get("search_request")}
    if context is not None and not isinstance(context, dict):
        raise InvalidInputError("'context' must be an object when provided")

    model_name = _safe_str(payload.get("model")) or _settings_value(
        "FOUNDRY_MODEL",
        default="",
    )
    agent_name = _safe_str(payload.get("agent_name")) or _settings_value(
        "MC_MATCHER_AGENT_NAME",
        "MATCHER_AGENT_NAME",
        default="mc-matcher",
    )

    agent_input = user_request
    if context:
        agent_input = f"{user_request}\n\nCONTEXT_JSON:\n{json.dumps(context, ensure_ascii=False)}"

    previous_response_id = _safe_str(payload.get("previous_response_id")) or None
    response = await asyncio.to_thread(
        lambda: _run_foundry_agent(
            agent_name=agent_name,
            message=agent_input,
            model_name=model_name,
            previous_response_id=previous_response_id,
        )
    )

    raw_response = _response_to_plain_dict(response)
    output_text = _safe_str(getattr(response, "output_text", ""))
    parsed_output = _extract_json_safe(output_text) if output_text else None

    result_payload: dict[str, Any]
    if isinstance(parsed_output, dict):
        result_payload = parsed_output
    else:
        result_payload = {"raw_text": output_text}

    return {
        "agent": {
            "name": agent_name,
            "model": model_name or None,
            "response_id": _safe_str(raw_response.get("id")),
            "previous_response_id": previous_response_id,
        },
        "result": result_payload,
        "raw_response": raw_response,
    }


@app.route(route="response-judger", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
@http_error_handler
async def response_judger(req: func.HttpRequest):
    """
    POST /api/response-judger

    Modalita' supportate:
    1) response_compatibility (LLM): verifica coerenza tra richiesta e risposta finale
    2) match_judgement (deterministica): produce solo metadati di judgement match
       (verdict/confidence/summary/failure/recovery/clarifications)

    Body JSON:
      mode opzionale: "response_compatibility" | "match_judgement"
    """
    payload = _body_params(req)
    if not payload:
        payload = _payload_from_query(req)
    if not payload:
        raise InvalidInputError("Missing or invalid JSON body")

    mode = _safe_str(payload.get("mode")).lower()
    if mode == "match_judgement":
        interpreted_request = payload.get("interpreted_request")
        if not isinstance(interpreted_request, dict):
            interpreted_request = payload.get("classification") if isinstance(payload.get("classification"), dict) else {}
        original_request = _safe_str(payload.get("original_request"))
        relaxed_criteria = _lower_list(payload.get("relaxed_criteria"))

        evaluated_candidates = payload.get("candidate_evaluations")
        if not isinstance(evaluated_candidates, list):
            evaluated_candidates = payload.get("evaluated_candidates")

        if isinstance(evaluated_candidates, list):
            evaluated = [c for c in evaluated_candidates if isinstance(c, dict)]
        else:
            source_candidates = _extract_evaluator_candidates(payload)
            evaluated = [
                _evaluate_candidate(candidate, interpreted_request, relaxed_criteria)
                for candidate in source_candidates
                if isinstance(candidate, dict)
            ]
            evaluated.sort(key=lambda c: c.get("match_score", 0.0), reverse=True)

        return _build_match_judgement_payload(
            evaluated_candidates=evaluated,
            interpreted_request=interpreted_request,
            relaxed_criteria=relaxed_criteria,
            original_request=original_request,
        )

    original_request = _safe_str(
        _first_non_empty(payload.get("original_request"), payload.get("query"))
    )
    final_answer = _safe_str(
        _first_non_empty(payload.get("final_answer"), payload.get("answer"), payload.get("response"))
    )

    if not original_request or not final_answer:
        raise InvalidInputError("'original_request' e 'final_answer' sono obbligatori")

    result = await asyncio.to_thread(_judge_response_compatibility, original_request, final_answer)
    return result


@app.route(route="db/candidates/search", methods=["POST"])
@http_error_handler
async def db_candidates_search(req: func.HttpRequest):
    """
    POST /api/db/candidates/search

    Ricerca read-only sul DB candidati persistito in MariaDB/MySQL.
    """
    payload = _body_params(req)
    if not payload:
        payload = _payload_from_query(req)

    query = _safe_str(_first_non_empty(payload.get("q"), payload.get("query"), req.params.get("q")))
    role = _safe_str(_first_non_empty(payload.get("role"), req.params.get("role"))) or None
    location = _safe_str(_first_non_empty(payload.get("location"), req.params.get("location"))) or None
    seniority = _safe_str(_first_non_empty(payload.get("seniority"), req.params.get("seniority"))) or None
    language = _safe_str(_first_non_empty(payload.get("language"), req.params.get("language"))) or None

    limit = _parse_int(
        _first_non_empty(payload.get("limit"), payload.get("max_items"), req.params.get("limit")),
        default=10,
    )
    if limit > 50:
        limit = 50

    min_experience_years = _parse_float(
        _first_non_empty(payload.get("min_experience_years"), req.params.get("min_experience_years"))
    )
    max_experience_years = _parse_float(
        _first_non_empty(payload.get("max_experience_years"), req.params.get("max_experience_years"))
    )

    async with acquire_conn() as conn:
        repo = CandidateRepository(conn)
        items = await repo.search_candidates(
            q=query or None,
            limit=limit,
            role=role,
            location=location,
            seniority=seniority,
            language=language,
            min_experience_years=min_experience_years,
            max_experience_years=max_experience_years,
        )

    return {
        "items": items,
        "meta": {
            "total": len(items),
            "limit": limit,
        },
    }


@app.route(route="db/candidates/details", methods=["POST"])
@http_error_handler
async def db_candidate_details(req: func.HttpRequest):
    """
    POST /api/db/candidates/details

    Recupera il dettaglio candidato partendo da match_key (o email come alias).
    """
    payload = _body_params(req)
    if not payload:
        payload = _payload_from_query(req)

    match_key = _safe_str(
        _first_non_empty(
            payload.get("match_key"),
            payload.get("email"),
            req.params.get("match_key"),
            req.params.get("email"),
        )
    ).lower()
    if not match_key:
        raise InvalidInputError("Missing required field: match_key")

    include_payload = _parse_bool(
        _first_non_empty(payload.get("include_payload"), req.params.get("include_payload")),
        default=True,
    )

    async with acquire_conn() as conn:
        repo = CandidateRepository(conn)
        candidate = await repo.get_candidate_by_match_key(match_key, include_payload=include_payload)

    return {
        "found": candidate is not None,
        "candidate": candidate,
    }


@app.route(route="db/candidates/{match_key}", methods=["GET"])
@http_error_handler
async def db_candidate_details_by_path(req: func.HttpRequest):
    """
    GET /api/db/candidates/{match_key}

    Variante REST del dettaglio candidato.
    """
    match_key = _safe_str(req.route_params.get("match_key")).lower()
    if not match_key:
        raise InvalidInputError("Missing route param: match_key")

    include_payload = _parse_bool(req.params.get("include_payload"), default=True)

    async with acquire_conn() as conn:
        repo = CandidateRepository(conn)
        candidate = await repo.get_candidate_by_match_key(match_key, include_payload=include_payload)

    return {
        "found": candidate is not None,
        "candidate": candidate,
    }


@app.route(route="backfill/incoming-cv", methods=["POST"])
@http_error_handler
async def backfill_incoming_cv(req: func.HttpRequest):
    """
    POST /api/backfill/incoming-cv

    Enqueue dei blob gia' presenti in incoming-cv verso document-processing.
    Parametri (query o JSON body):
    - dry_run: true/false (default true)
    - prefix: prefisso opzionale blob
    - max_items: limite enqueue (default 100)
    - only_pdf: true/false (default true)
    """

    payload = _body_params(req)

    def get_value(key: str):
        query_value = req.params.get(key)
        return query_value if query_value is not None else payload.get(key)

    dry_run = _parse_bool(get_value("dry_run"), default=True)
    only_pdf = _parse_bool(get_value("only_pdf"), default=True)
    max_items = _parse_int(get_value("max_items"), default=100)
    prefix = get_value("prefix")
    if isinstance(prefix, str):
        prefix = prefix.strip() or None
    else:
        prefix = None

    connection_string = settings.storage_account_connection_string or settings.storage_connection_string
    if not connection_string:
        raise InvalidInputError("Missing AzureWebJobsStorage configuration")

    enqueuer = BackfillEnqueuer(
        connection_string=connection_string,
        container_name=settings.storage_container_incoming,
        queue_name=settings.document_processing_queue_name,
    )

    result = await enqueuer.enqueue_existing(
        prefix=prefix,
        max_items=max_items,
        dry_run=dry_run,
        only_pdf=only_pdf,
    )

    logger.info(
        "Backfill completed dry_run=%s selected=%s scanned=%s queue=%s",
        result["dry_run"],
        result["selected"],
        result["scanned"],
        result["target_queue"],
    )

    return result



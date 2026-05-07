import azure.functions as func
import logging
import json
import re
import math
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

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
    rerank,
    normalise_search_request,
    resolve_index,
)

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


async def _run_search_pipeline(payload: dict) -> dict:
    p = normalise_search_request(payload)

    if not p["query"] and not p["skills"] and not p["role"]:
        raise InvalidInputError("At least one of 'query', 'skills' or 'role' is required")

    index_name = resolve_index(p["subco"])

    odata_filter = build_odata_filter(
        skills=p["skills"],
        seniority=p["seniority"],
        min_experience_years=p["min_experience_years"],
        max_experience_years=p["max_experience_years"],
        language=p["language"],
        availability_required=p["availability_required"],
    )

    augmented_query = " ".join(filter(None, [
        p["query"],
        " ".join(p["skills"]),
        p["role"] or "",
        p["seniority"] or "",
    ])).strip()

    embedding: list[float] | None = None
    if p["hybrid"] and augmented_query:
        try:
            from infra.llm_client import get_embedding_client
            emb_client = get_embedding_client()
            embedding = await emb_client.aembed_query(augmented_query)
        except Exception:
            logger.exception("Embedding generation failed, falling back to lexical-only search")

    search = SearchService()
    raw_hits = await search.search_chunks(
        query=augmented_query or "*",
        odata_filter=odata_filter,
        embedding=embedding,
        top=p["top"],
        index_name=index_name,
    )

    hits = rerank(
        raw_hits,
        query_skills=p["skills"],
        query_role=p["role"],
        query_location=p["location"],
        top=p["top"],
    )

    relaxed = False
    suggestions: list[str] = []
    fallback_min = math.ceil(p["top"] * settings.search_fallback_threshold)

    if len(hits) < fallback_min and p["skills"]:
        relaxed = True
        relaxed_filter = build_odata_filter_relaxed(
            seniority=p["seniority"],
            min_experience_years=p["min_experience_years"],
            max_experience_years=p["max_experience_years"],
            language=p["language"],
        )
        relaxed_hits = await search.search_chunks(
            query=augmented_query or "*",
            odata_filter=relaxed_filter,
            embedding=embedding,
            top=p["top"],
            index_name=index_name,
        )
        relaxed_reranked = rerank(
            relaxed_hits,
            query_skills=p["skills"],
            query_role=p["role"],
            query_location=p["location"],
            top=p["top"],
        )
        existing_ids = {h["document_id"] for h in hits}
        for h in relaxed_reranked:
            if h["document_id"] not in existing_ids:
                hits.append(h)
                existing_ids.add(h["document_id"])
        hits = hits[:p["top"]]
        suggestions = [f"{s} (not found, relaxed)" for s in p["skills"]]

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
            "hybrid": p["hybrid"],
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


def _extract_evaluator_candidates(payload: dict) -> list[dict[str, Any]]:
    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        return candidates

    search_response = payload.get("search_response")
    if isinstance(search_response, dict):
        if isinstance(search_response.get("hits"), list):
            return search_response["hits"]
        data = search_response.get("data")
        if isinstance(data, dict) and isinstance(data.get("hits"), list):
            return data["hits"]

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
    cand_role = _safe_str(candidate.get("role")).lower()
    cand_location = _safe_str(candidate.get("location")).lower()
    cand_seniority = _safe_str(candidate.get("seniority")).lower()
    cand_language = _safe_str(_first_non_empty(candidate.get("language"), candidate.get("english_level"))).lower()
    cand_availability = candidate.get("availability_days")

    relaxed_criteria = _lower_list(_first_non_empty(candidate.get("relaxed_criteria"), fallback_relaxed_criteria))

    weights: dict[str, float] = {}
    component_scores: dict[str, float] = {}
    reasons: list[str] = []
    strengths: list[str] = []
    weaknesses: list[str] = []
    missing_requirements: list[str] = []

    if req_skills:
        overlap = [s for s in req_skills if s in cand_skills]
        skill_score = len(overlap) / len(req_skills)
        weights["skills"] = 0.45
        component_scores["skills"] = skill_score
        if skill_score >= 0.7:
            reasons.append("skill principali coerenti")
            strengths.append("copertura skill elevata")
        elif skill_score > 0:
            reasons.append("copertura skill parziale")
            weaknesses.append("non tutte le skill richieste sono presenti")
            missing_requirements.append("copertura skill completa")
        else:
            weaknesses.append("assenza delle skill chiave richieste")
            missing_requirements.append("skill principali")

    if req_role:
        role_score = 1.0 if (req_role in cand_role or cand_role in req_role) else 0.0
        weights["role"] = 0.2
        component_scores["role"] = role_score
        if role_score >= 1.0:
            reasons.append("ruolo compatibile")
        else:
            weaknesses.append("ruolo non allineato")
            missing_requirements.append("ruolo")

    if req_location and work_mode in {"onsite", "hybrid", "unknown"}:
        location_score = 1.0 if req_location in cand_location else 0.0
        location_weight = 0.2 if work_mode in {"onsite", "hybrid"} else 0.08
        weights["location"] = location_weight
        component_scores["location"] = location_score
        if location_score >= 1.0:
            reasons.append("location compatibile")
        else:
            weaknesses.append("location non compatibile")
            missing_requirements.append("location")

    if req_seniority:
        seniority_score = 1.0 if req_seniority == cand_seniority else 0.0
        weights["seniority"] = 0.1
        component_scores["seniority"] = seniority_score
        if seniority_score < 1.0:
            missing_requirements.append("seniority")

    if req_language:
        language_score = 1.0 if req_language in cand_language else 0.0
        weights["language"] = 0.05
        component_scores["language"] = language_score
        if language_score < 1.0:
            missing_requirements.append("lingua")

    if req_availability:
        availability_score = 0.0
        if isinstance(cand_availability, (int, float)):
            if cand_availability <= 30:
                availability_score = 1.0
            elif cand_availability <= 60:
                availability_score = 0.6
            else:
                availability_score = 0.2
        weights["availability"] = 0.05
        component_scores["availability"] = availability_score
        if availability_score < 0.6:
            missing_requirements.append("disponibilita'")

    total_weight = sum(weights.values())
    if total_weight <= 0:
        match_score = 0.0
    else:
        weighted_sum = sum(component_scores.get(k, 0.0) * w for k, w in weights.items())
        match_score = weighted_sum / total_weight

    if relaxed_criteria:
        match_type = "extended"
    elif match_score >= 0.8:
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

    retrieval_score = candidate.get("retrieval_score")
    if retrieval_score is None:
        retrieval_score = candidate.get("score")

    return {
        "candidate_id": _safe_str(_first_non_empty(candidate.get("candidate_id"), candidate.get("document_id"), candidate.get("id"), candidate_name)),
        "name": candidate_name,
        "role": candidate_role,
        "location": candidate_location,
        "skills": cand_skills,
        "availability_days": cand_availability,
        "language": _safe_str(candidate.get("language")) or None,
        "retrieval_score": retrieval_score,
        "source_path": candidate.get("source_path"),
        "match_score": round(float(match_score), 4),
        "match_type": match_type,
        "reasons": reasons,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "missing_requirements": list(dict.fromkeys(missing_requirements)),
        "relaxed_criteria": relaxed_criteria,
        "summary": f"{candidate_name}: coerenza {match_type} con score {round(float(match_score), 2)}.",
        "_component_scores": component_scores,
    }


def _level_from_score(score: float | None) -> str:
    if score is None:
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
        }

    top = evaluated_candidates[0]
    components = top.get("_component_scores", {})
    return {
        "skills": _level_from_score(components.get("skills")),
        "role": _level_from_score(components.get("role")),
        "location": _level_from_score(components.get("location")),
        "seniority": _level_from_score(components.get("seniority")),
        "language": _level_from_score(components.get("language")),
    }


def _evaluate_match_payload(payload: dict[str, Any]) -> dict[str, Any]:
    original_request = _safe_str(payload.get("original_request"))
    interpreted_request = payload.get("interpreted_request")
    if not isinstance(interpreted_request, dict):
        interpreted_request = payload.get("classification") if isinstance(payload.get("classification"), dict) else {}

    candidates = _extract_evaluator_candidates(payload)
    if not isinstance(candidates, list):
        candidates = []

    relaxed_criteria = _lower_list(payload.get("relaxed_criteria"))

    if not candidates:
        return {
            "verdict": "invalid_input",
            "confidence": 0.1,
            "summary": "Input non valido: manca sia candidates sia search_response.hits/data.hits.",
            "coverage": {
                "skills": "unknown",
                "role": "unknown",
                "location": "unknown",
                "seniority": "unknown",
                "language": "unknown",
            },
            "critical_gaps": ["missing_candidates"],
            "relaxation_suggestions": ["availability", "languages", "role", "location"],
            "best_candidates": [],
            "evaluated_candidates": [],
            "original_request": original_request,
            "interpreted_request": interpreted_request,
        }

    evaluated = [
        _evaluate_candidate(candidate, interpreted_request, relaxed_criteria)
        for candidate in candidates
        if isinstance(candidate, dict)
    ]
    evaluated.sort(key=lambda c: c.get("match_score", 0.0), reverse=True)

    for c in evaluated:
        c.pop("_component_scores", None)

    top_score = evaluated[0].get("match_score", 0.0) if evaluated else 0.0
    if top_score >= 0.8:
        verdict = "strong_match"
    elif top_score >= 0.6:
        verdict = "partial_match"
    elif top_score >= 0.4:
        verdict = "weak_match"
    else:
        verdict = "no_match"

    confidence = round(min(1.0, 0.35 + 0.5 * float(top_score) + min(len(evaluated), 5) * 0.03), 4)

    critical_gaps: list[str] = []
    top_missing = evaluated[0].get("missing_requirements", []) if evaluated else []
    if isinstance(top_missing, list):
        critical_gaps.extend(top_missing[:3])

    best_candidates = [
        {
            "full_name": c.get("name"),
            "role": c.get("role"),
            "location": c.get("location"),
            "why_fit": (c.get("reasons") or ["coerenza parziale"])[0],
            "risk": (c.get("missing_requirements") or [None])[0],
        }
        for c in evaluated[:5]
    ]

    return {
        "verdict": verdict,
        "confidence": confidence,
        "summary": f"Valutati {len(evaluated)} candidati. Miglior score: {round(float(top_score), 2)}.",
        "coverage": _aggregate_coverage(evaluated),
        "critical_gaps": list(dict.fromkeys(critical_gaps)),
        "relaxation_suggestions": ["availability", "languages", "role", "location"],
        "best_candidates": best_candidates,
        "evaluated_candidates": evaluated,
        "original_request": original_request,
        "interpreted_request": interpreted_request,
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

    search_response = await _run_search_pipeline(search_request)

    classification_fields = {
        k: v
        for k, v in payload.items()
        if k != "search_request"
    }

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

    Wrapper API per Match Evaluator:
    - accetta payload con original_request + interpreted_request + candidates
    - fallback compatibile: candidates da search_response.hits o search_response.data.hits
    - restituisce valutazione strutturata del match
    """
    payload = _body_params(req)
    if not payload:
        payload = _payload_from_query(req)
    if not payload:
        raise InvalidInputError("Missing or invalid JSON body")

    return _evaluate_match_payload(payload)


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



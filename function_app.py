import asyncio
import azure.functions as func
import logging
import json
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from core.config import settings
from core.errors import InvalidInputError, FileTooLargeError

from infra.blob_storage import StorageService
from infra.backfill_enqueuer import BackfillEnqueuer
from infra.mcflash_candidates import MCFlashCandidatesClient
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
)
from services.search_pipeline import run_search_pipeline

from utils.http_errors import http_error_handler

# Re-export con i nomi storici: gli handler qui sotto (e i monkeypatch dei test)
# continuano a riferirsi a `_nome`, ma l'implementazione vive nei moduli.
from utils.app_settings import settings_value as _settings_value
from utils.values import (
    extract_json_safe as _extract_json_safe,
    first_non_empty as _first_non_empty,
    lower_list as _lower_list,
    safe_str as _safe_str,
    to_float as _to_float,
)
from utils.http_params import (
    body_params as _body_params,
    parse_bool as _parse_bool,
    parse_float as _parse_float,
    parse_int as _parse_int,
    payload_from_query as _payload_from_query,
)
from infra.foundry_agent import (
    foundry_project_endpoint as _foundry_project_endpoint,
    normalise_ai_matcher_candidates as _normalise_ai_matcher_candidates,
    response_to_plain_dict as _response_to_plain_dict,
    responses_api_version as _responses_api_version,
    run_foundry_agent as _run_foundry_agent,
    split_ai_matcher_answer as _split_ai_matcher_answer,
)
from infra.chat_history import (
    chat_history_append_turn as _chat_history_append_turn,
    chat_history_load_thread as _chat_history_load_thread,
)
from services.coherence_evaluator import (
    get_judge_client as _get_judge_client,
    run_candidate_coherence_evaluator as _run_candidate_coherence_evaluator,
)

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
_mcflash_candidates_client: MCFlashCandidatesClient | None = None


def _get_mcflash_candidates_client() -> MCFlashCandidatesClient:
    global _mcflash_candidates_client
    if _mcflash_candidates_client is not None:
        return _mcflash_candidates_client

    base_url = _settings_value(
        "MCFLASH_CANDIDATI_URL",
        "MCFLASH_CANDIDATES_URL",
        default="https://mcflashcandidati.mcengineering.eu/api/Candidati",
    )
    api_key = _settings_value(
        "MCFLASH_CANDIDATI_API_KEY",
        "MCFLASH_CANDIDATES_API_KEY",
        default="MCFlash-Candidati-2026!",
    )

    _mcflash_candidates_client = MCFlashCandidatesClient(
        base_url=base_url,
        api_key=api_key,
        timeout_seconds=20,
    )
    return _mcflash_candidates_client


def _build_processing_message(*, blob_name: str, last_modified: str | None = None) -> dict:
    filename = blob_name.split("/")[-1]
    return {
        "blob": f"{settings.storage_container_incoming}/{blob_name}",
        "filename": filename,
        "source_path": f"/{settings.storage_container_incoming}/{blob_name}",
        "last_modified": last_modified or datetime.now(timezone.utc).isoformat(),
        "correlation_id": f"blob-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8]}",
    }


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


@app.route(route="ai-matcher-wrapper", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
@http_error_handler
async def ai_matcher_wrapper(req: func.HttpRequest):
    """
    POST /api/ai-matcher-wrapper

    Wrapper riusabile per chiamare il chatbot Foundry `ai_matcher` da sistemi esterni.
    Pensato per essere consumato come tool OpenAPI da altri agenti.
    """
    payload = _body_params(req)
    if not payload:
        payload = _payload_from_query(req)
    if not payload:
        raise InvalidInputError("Missing or invalid JSON body")

    include_raw_response = _parse_bool(
        _first_non_empty(payload.get("include_raw_response"), payload.get("return_raw_response")),
        default=False,
    )
    include_result = _parse_bool(
        _first_non_empty(payload.get("include_result"), payload.get("return_result")),
        default=False,
    )

    user_id = _safe_str(_first_non_empty(payload.get("user_id"), payload.get("userId")))
    chat_id = _safe_str(
        _first_non_empty(
            payload.get("chat_id"),
            payload.get("chatId"),
            payload.get("conversation_id"),
            payload.get("conversationId"),
        )
    )
    if not user_id:
        raise InvalidInputError("'user_id' is required")
    if not chat_id:
        raise InvalidInputError("'chat_id' is required")

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

    session_context = {
        "user_id": user_id,
        "chat_id": chat_id,
    }
    if context:
        context = {
            **context,
            "session": {
                **session_context,
                **(context.get("session") if isinstance(context.get("session"), dict) else {}),
            },
        }
    else:
        context = {"session": session_context}

    model_name = _safe_str(payload.get("model")) or _settings_value(
        "FOUNDRY_MODEL",
        default="",
    )
    agent_name = _safe_str(payload.get("agent_name")) or _settings_value(
        "AI_MATCHER_AGENT_NAME",
        default="mc_matcher",
    )

    agent_input = user_request
    if context:
        agent_input = f"{user_request}\n\nCONTEXT_JSON:\n{json.dumps(context, ensure_ascii=False)}"

    previous_response_id = _safe_str(payload.get("previous_response_id")) or None
    history_status: dict[str, Any] = {
        "enabled": False,
        "persisted": False,
        "continued": False,
        "source_previous_response_id": "request" if previous_response_id else None,
    }

    if not previous_response_id:
        try:
            thread = await asyncio.to_thread(_chat_history_load_thread, user_id, chat_id)
            stored_previous_response_id = _safe_str((thread or {}).get("last_response_id"))
            if stored_previous_response_id:
                previous_response_id = stored_previous_response_id
                history_status["source_previous_response_id"] = "storage"
                history_status["continued"] = True
            history_status["enabled"] = True
        except Exception as exc:
            history_status["error"] = str(exc)
            logger.warning(
                "chat_history_load_failed user_id=%s chat_id=%s error=%s",
                user_id,
                chat_id,
                exc,
            )

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

    # The agent appends a machine-readable trailer after its prose answer:
    #   <<<CANDIDATES_JSON>>>
    #   {"candidates":[{"id_mcflash":..,"trigramma":..}, ...]}
    # Keep `answer` pure natural language and surface the trailer as `candidates`.
    answer_body, proposed_candidates = _split_ai_matcher_answer(output_text)
    effective_text = answer_body or output_text
    parsed_output = _extract_json_safe(effective_text) if effective_text else None

    result_payload: dict[str, Any]
    if isinstance(parsed_output, dict):
        result_payload = parsed_output
    else:
        result_payload = {"raw_text": effective_text}

    response_id = _safe_str(raw_response.get("id")) or None
    assistant_response_text = effective_text if effective_text else json.dumps(result_payload, ensure_ascii=False)

    answer_text = _safe_str(effective_text)
    if not answer_text and isinstance(result_payload, dict):
        answer_text = _safe_str(
            _first_non_empty(
                result_payload.get("answer"),
                result_payload.get("final_answer"),
                result_payload.get("response"),
                result_payload.get("raw_text"),
            )
        )
    if not answer_text:
        answer_text = _truncate_text(json.dumps(result_payload, ensure_ascii=False), max_len=4000)

    try:
        persist_meta = await asyncio.to_thread(
            _chat_history_append_turn,
            user_id=user_id,
            chat_id=chat_id,
            user_request=user_request,
            assistant_response=assistant_response_text,
            previous_response_id=previous_response_id,
            response_id=response_id,
            agent_name=agent_name,
            model_name=model_name or None,
        )
        history_status["enabled"] = True
        history_status["persisted"] = True
        history_status["turn_count"] = persist_meta.get("turn_count")
    except Exception as exc:
        history_status["enabled"] = True
        history_status["persisted"] = False
        history_status["error"] = str(exc)
        logger.warning(
            "chat_history_persist_failed user_id=%s chat_id=%s error=%s",
            user_id,
            chat_id,
            exc,
        )

    response_payload = {
        "agent": {
            "name": agent_name,
            "model": model_name or None,
            "response_id": response_id,
            "previous_response_id": previous_response_id,
        },
        "session": {
            "user_id": user_id,
            "chat_id": chat_id,
        },
        "conversation": history_status,
        "answer": answer_text,
        "candidates": proposed_candidates,
    }

    if include_result:
        response_payload["result"] = result_payload
    if include_raw_response:
        response_payload["raw_response"] = raw_response

    return response_payload


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




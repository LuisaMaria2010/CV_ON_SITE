import asyncio
import azure.functions as func
import logging
import json
import re
import math
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

try:
    from openai import AzureOpenAI as _AzureOpenAI
    _openai_available = True
except ImportError:
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
    relaxed_criteria = list(p.get("relaxed_criteria") or [])
    suggestions: list[str] = []
    fallback_min = math.ceil(p["top"] * settings.search_fallback_threshold)

    if len(hits) < fallback_min and p["skills"]:
        relaxed = True
        if "skills" not in relaxed_criteria:
            relaxed_criteria.append("skills")
        relaxed_query = " ".join(filter(None, [
            p["role"] or "",
            p["seniority"] or "",
            p["query"] if not p["role"] else "",
        ])).strip() or p["role"] or p["query"] or "*"
        relaxed_filter = build_odata_filter_relaxed(
            seniority=p["seniority_explicit"],
            min_experience_years=p["min_experience_years"],
            max_experience_years=p["max_experience_years"],
            language=p["language"],
        )
        relaxed_hits = await search.search_chunks(
            query=relaxed_query,
            odata_filter=relaxed_filter,
            embedding=embedding,
            top=max(p["top"], fallback_min * 3),
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
                h["is_relaxed_result"] = True
                hits.append(h)
                existing_ids.add(h["document_id"])
        hits = hits[:p["top"]]
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
        work_mode=p["work_mode"],
        relaxed_criteria=relaxed_criteria,
    )

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

    match_features = candidate.get("match_features") if isinstance(candidate.get("match_features"), dict) else None
    feature_relaxed = []
    if match_features and isinstance(match_features.get("relaxed_criteria"), list):
        feature_relaxed = _lower_list(match_features.get("relaxed_criteria"))
    relaxed_criteria = _lower_list(_first_non_empty(feature_relaxed, candidate.get("relaxed_criteria"), fallback_relaxed_criteria))

    weights: dict[str, float] = {}
    component_scores: dict[str, float] = {}
    reasons: list[str] = []
    strengths: list[str] = []
    weaknesses: list[str] = []
    missing_requirements: list[str] = []

    if req_skills:
        if match_features and isinstance(match_features.get("skills"), dict):
            skills_block = match_features.get("skills") or {}
            exact = _lower_list(skills_block.get("matched"))
            semantic = _lower_list(skills_block.get("semantic_matches"))
            matched_count = len(set(exact) | set(semantic))
            skill_score = min(1.0, matched_count / len(req_skills))
            overlap = exact + semantic
            missing_from_features = _lower_list(skills_block.get("missing"))
            for miss in missing_from_features:
                if miss not in missing_requirements:
                    missing_requirements.append(miss)
        else:
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
        if match_features and isinstance(match_features.get("role"), dict):
            feature_role_score = match_features.get("role", {}).get("score")
            if isinstance(feature_role_score, (int, float)):
                role_score = max(0.0, min(1.0, float(feature_role_score)))
            else:
                role_score = 1.0 if (req_role in cand_role or cand_role in req_role) else 0.0
        else:
            role_score = 1.0 if (req_role in cand_role or cand_role in req_role) else 0.0
        weights["role"] = 0.2
        component_scores["role"] = role_score
        if role_score >= 1.0:
            reasons.append("ruolo compatibile")
        else:
            weaknesses.append("ruolo non allineato")
            missing_requirements.append("ruolo")

    if req_location and work_mode in {"onsite", "hybrid", "unknown"}:
        if match_features and isinstance(match_features.get("location"), dict):
            location_label = _safe_str(match_features.get("location", {}).get("match")).lower()
            location_score = {
                "exact": 1.0,
                "strong": 1.0,
                "soft": 0.6,
                "weak": 0.35,
                "none": 0.0,
                "not_applicable": 1.0,
            }.get(location_label, 0.0)
        else:
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
        if match_features and isinstance(match_features.get("language"), dict):
            feature_language = match_features.get("language", {}).get("match")
            if isinstance(feature_language, bool):
                language_score = 1.0 if feature_language else 0.0
            else:
                language_score = 1.0 if req_language in cand_language else 0.0
        else:
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
        "matched_on": _lower_list(match_features.get("matched_on")) if match_features else [],
        "match_features": match_features,
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


# ---------------------------------------------------------------------------
# LLM Judge — stesso pattern di run_evaluation_classifier.py
# ---------------------------------------------------------------------------

_JUDGE_MATCH_SYSTEM = """\
Sei il Match Evaluator del sistema MC Flash.

Ricevi la richiesta originale del cliente, il payload interpretato e i profili restituiti
dal motore di ricerca. Devi classificare la qualita' del match tra richiesta e candidati.

Regole di valutazione (priorita'):
1. Skills (peso ~0.45): presenza e rilevanza delle skill richieste
2. Location/work_mode (peso ~0.20 onsite/hybrid, ~0.08 remote): compatibilita' sede
3. Ruolo (peso ~0.20): coerenza del ruolo professionale
4. Seniority (peso ~0.10): coerenza di seniority/anni esperienza
5. Lingue (peso ~0.05): solo se richieste esplicitamente
6. Disponibilita' (peso ~0.05): solo se richiesta esplicitamente

Vincoli:
- Le skills hanno peso maggiore del ruolo.
- La location e' importante solo per onsite/hybrid; NON penalizzare richieste remote.
- Usa `match_features` del search come fonte primaria quando disponibile.
- NON inventare skill o informazioni mancanti.
- NON assegnare score artificialmente alti.
- NON promuovere tutti i candidati.
- Sii determinista. Restituisci SOLO JSON valido, senza markdown o commenti.
"""

_JUDGE_MATCH_PROMPT = """\
Valuta il match tra la richiesta del cliente e i candidati trovati.

RICHIESTA ORIGINALE:
_ORIGINAL_REQUEST_

RICHIESTA INTERPRETATA (strutturata):
_INTERPRETED_REQUEST_

CANDIDATI:
_CANDIDATES_

Restituisci esattamente questo JSON (nessun markdown):
{
    "verdict": "strong_match|partial_match|weak_match|no_match|invalid_input",
    "confidence": 0.0,
    "summary": "stringa breve",
    "failure_type": "no_matches|poor_skill_coverage|location_mismatch|seniority_mismatch|ambiguous_query|invalid_input|none",
    "recovery_strategy": "RETURN_ANSWER|RELAX_AND_RETRY|REWRITE_QUERY|ASK_USER_CLARIFICATION|RETURN_PARTIAL_ANSWER|SAFE_REFUSAL",
    "needs_clarification": false,
    "clarifying_questions": [],
    "improved_queries": [],
    "missing_entities": [],
    "coverage": {
        "skills": "high|medium|low|unknown",
        "role": "high|medium|low|unknown",
        "location": "high|medium|low|unknown",
        "seniority": "high|medium|low|unknown",
        "language": "high|medium|low|unknown"
    },
    "critical_gaps": [],
    "relaxation_suggestions": [],
    "search_evaluation": {
        "quality": "excellent|good|fair|poor|insufficient_data",
        "summary": "stringa breve",
        "coverage": {
            "skills": "high|medium|low|unknown",
            "role": "high|medium|low|unknown",
            "location": "high|medium|low|unknown",
            "seniority": "high|medium|low|unknown",
            "language": "high|medium|low|unknown"
        },
        "critical_gaps": []
    },
    "candidate_evaluations": [
        {
            "candidate_id": "string",
            "full_name": "string",
            "role": "string|null",
            "location": "string|null",
            "match_score": 0.0,
            "match_type": "strong|good|weak|extended",
            "why_fit": "string",
            "risk": "string|null",
            "reasons": [],
            "strengths": [],
            "weaknesses": [],
            "missing_requirements": [],
            "matched_on": ["skills", "role"]
        }
    ],
    "best_candidates": [
        {
            "full_name": "string",
            "role": "string",
            "location": "string",
            "why_fit": "string",
            "risk": "string|null"
        }
    ]
}

Verdetti globali (basati sul top match_score tra tutti i candidati):
- strong_match: top match_score >= 0.80
- partial_match: top match_score >= 0.60
- weak_match: top match_score >= 0.40
- no_match: top match_score < 0.40
- invalid_input: nessun candidato valido o payload non valido

Recovery strategy:
- RETURN_ANSWER: strong_match o partial_match con buona copertura
- RELAX_AND_RETRY: weak/no_match, ricerca NON ancora rilassata (relaxed_criteria=[])
- ASK_USER_CLARIFICATION: ricerca gia' rilassata e ancora no_match, oppure query ambigua senza segnali
- RETURN_PARTIAL_ANSWER: ricerca gia' rilassata e weak_match
- SAFE_REFUSAL: invalid_input o zero candidati
- REWRITE_QUERY: query vaga, candidati esistono ma non vengono raggiunti

needs_clarification: true solo se recovery_strategy = ASK_USER_CLARIFICATION
"""


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


def _judge_match_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    """
    Valuta il match usando Azure OpenAI come judge LLM.
    Stesso pattern di judge_score() in run_evaluation_classifier.py:
      - temperature=0, response_format=json_object
      - _extract_json_safe() per parsing robusto
      - Fallback a None su qualsiasi errore
    """
    client = _get_judge_client()
    if client is None:
        return None

    original_request = _safe_str(payload.get("original_request")) or ""
    interpreted_request = payload.get("interpreted_request") or {}
    if not isinstance(interpreted_request, dict):
        interpreted_request = {}
    relaxed_criteria = _lower_list(payload.get("relaxed_criteria"))

    candidates = _extract_evaluator_candidates(payload)
    if not isinstance(candidates, list) or not candidates:
        return None

    # Compact candidates — evita token overflow (limit ridotto a 5 candidati, skills a 3)
    compact: list[dict[str, Any]] = []
    for c in candidates[:5]:
        compact.append({
            "candidate_id": c.get("candidate_id") or c.get("document_id"),
            "full_name": c.get("full_name") or c.get("name"),
            "role": c.get("role"),
            "location": c.get("location"),
            "skills": (c.get("skills") or [])[:3],
            "seniority": c.get("seniority"),
            "language": c.get("language"),
            "retrieval_score": c.get("retrieval_score"),
            "match_features": c.get("match_features"),
        })

    # Aggiunge info relaxation al contesto
    if relaxed_criteria:
        for item in compact:
            item["relaxed_criteria"] = relaxed_criteria

    user_msg = (
        _JUDGE_MATCH_PROMPT
        .replace("_ORIGINAL_REQUEST_", original_request)
        .replace("_INTERPRETED_REQUEST_", json.dumps(interpreted_request, ensure_ascii=False))
        .replace("_CANDIDATES_", json.dumps(compact, ensure_ascii=False, indent=2))
    )

    # Esegui la chiamata bloccante in un thread con timeout per evitare attese
    # indefinite. Riduciamo anche `max_tokens` per abbassare latenza e costi.
    try:
        import functools

        call = functools.partial(
            client.chat.completions.create,
            model=settings.azure_openai_model,
            messages=[
                {"role": "system", "content": _JUDGE_MATCH_SYSTEM},
                {"role": "user",   "content": user_msg},
            ],
            max_tokens=getattr(settings, "judge_max_tokens", 800),
            temperature=0,
            response_format={"type": "json_object"},
        )

        # Questo metodo viene invocato in un thread tramite `asyncio.to_thread`
        # da chi lo richiama. Qui eseguiamo la chiamata in modo sincrono
        # (non usare `await` fuori da funzioni async).
        resp = call()
        raw = (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.warning("LLM judge call failed: %s", exc)
        return None

    parsed = _extract_json_safe(raw)
    if not parsed:
        logger.warning("LLM judge returned malformed JSON")
        return None

    # Normalizza e arricchisce con campi obbligatori mancanti
    verdict = str(parsed.get("verdict") or "no_match").lower()
    candidate_evals = parsed.get("candidate_evaluations") or []
    best = parsed.get("best_candidates") or [
        {
            "full_name": c.get("full_name"),
            "role": c.get("role"),
            "location": c.get("location"),
            "why_fit": c.get("why_fit") or "",
            "risk": c.get("risk"),
        }
        for c in candidate_evals[:5]
    ]

    return {
        "verdict": verdict,
        "confidence": float(parsed.get("confidence") or 0.5),
        "summary": str(parsed.get("summary") or ""),
        "failure_type": str(parsed.get("failure_type") or "none"),
        "recovery_strategy": str(parsed.get("recovery_strategy") or "RETURN_ANSWER"),
        "needs_clarification": bool(parsed.get("needs_clarification", False)),
        "clarifying_questions": list(parsed.get("clarifying_questions") or []),
        "improved_queries": list(parsed.get("improved_queries") or []),
        "missing_entities": list(parsed.get("missing_entities") or []),
        "coverage": parsed.get("coverage") or {},
        "critical_gaps": list(parsed.get("critical_gaps") or []),
        "relaxation_suggestions": list(parsed.get("relaxation_suggestions") or []),
        "search_evaluation": parsed.get("search_evaluation") or {},
        "candidate_evaluations": candidate_evals,
        "evaluated_candidates": candidate_evals,
        "best_candidates": best,
        "original_request": original_request,
        "interpreted_request": interpreted_request,
    }


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
        "failure_type": recovery["failure_type"],
        "recovery_strategy": recovery["recovery_strategy"],
        "needs_clarification": recovery["needs_clarification"],
        "clarifying_questions": recovery["clarifying_questions"],
    }


def _evaluate_match_payload(payload: dict[str, Any]) -> dict[str, Any]:
    interpreted_request = payload.get("interpreted_request")
    if not isinstance(interpreted_request, dict):
        interpreted_request = payload.get("classification") if isinstance(payload.get("classification"), dict) else {}

    candidates = _extract_evaluator_candidates(payload)
    if not isinstance(candidates, list):
        candidates = []

    relaxed_criteria = _lower_list(payload.get("relaxed_criteria"))

    if not candidates:
        return {
            "best_candidates": [],
            "candidate_evaluations": [],
            "evaluated_candidates": [],
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

    for c in evaluated:
        c.pop("_component_scores", None)

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
        "best_candidates": best_candidates,
        "candidate_evaluations": evaluated,
        "evaluated_candidates": evaluated,
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

    # Availability is handled in the later DB stage, not in index search.
    search_request_for_index = {
        k: v for k, v in search_request.items()
        if k not in {"availability", "availability_required", "availability_days", "availability_date"}
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



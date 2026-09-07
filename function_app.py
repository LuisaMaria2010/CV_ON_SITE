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
    from azure.data.tables import TableServiceClient
except ImportError:
    TableServiceClient = None

from core.config import settings
from core.errors import InvalidInputError, FileTooLargeError

from infra.blob_storage import StorageService
from infra.backfill_enqueuer import BackfillEnqueuer
from infra.mcflash_candidates import MCFlashApiError, MCFlashCandidatesClient
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
from services.search_pipeline import run_search_pipeline

from utils.http_errors import http_error_handler

try:
    from openai import AzureOpenAI as _AzureOpenAI
    from openai import OpenAI as _OpenAI
    _openai_available = True
except ImportError:
    _OpenAI = None
    _openai_available = False

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
_chat_history_table_client: Any = None
_mcflash_candidates_client: MCFlashCandidatesClient | None = None
JUDGE_DEFAULT_TIMEOUT = getattr(settings, "judge_timeout_seconds", 10)


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


async def _run_search_pipeline(payload: dict) -> dict:
    return await run_search_pipeline(
        payload,
        get_mcflash_candidates_client=_get_mcflash_candidates_client,
        logger=logger,
    )
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


def _extract_coherence_evaluator_candidates(payload: dict) -> list[dict[str, Any]]:
    """Candidates are passed through verbatim (same fixed searcher-wrapper
    contract) — no field whitelist needed, the coherence evaluator never
    rewrites candidate data, only reorders it."""
    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        return [c for c in candidates if isinstance(c, dict)]

    search_response = payload.get("search_response")
    if isinstance(search_response, dict):
        if isinstance(search_response.get("hits"), list):
            return [c for c in search_response["hits"] if isinstance(c, dict)]
        data = search_response.get("data")
        if isinstance(data, dict) and isinstance(data.get("hits"), list):
            return [c for c in data["hits"] if isinstance(c, dict)]

    return []


def _mcflash_value(candidate: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in candidate and candidate.get(key) is not None:
            return candidate.get(key)
    return None


def _build_minimal_search_hit(hit: dict[str, Any], requested_skills: list[str]) -> dict[str, Any]:
    """Fixed 12-field contract, always present regardless of which search_request
    fields were used: id_mcflash, nome, ruolo, eta, seniority, location, skills,
    workmode, lingue, disponibilita, budget, semantic_snippet."""
    mcflash_profile = hit.get("mcflash_profile") if isinstance(hit.get("mcflash_profile"), dict) else {}
    compact_skills = _compact_skills_for_wrapper(hit.get("skills"), requested_skills)

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
        "semantic_snippet": hit.get("semantic_evidence"),
    }


def _compact_skills_for_wrapper(
    candidate_skills: Any,
    requested_skills: list[str],
    *,
    max_extra: int = 3,
) -> list[str]:
    requested = [s for s in _lower_list(requested_skills) if s]
    requested_set = set(requested)
    candidate = [s for s in _lower_list(candidate_skills) if s]

    selected: list[str] = []
    seen: set[str] = set()

    for skill in requested:
        if skill in seen:
            continue
        selected.append(skill)
        seen.add(skill)

    extra_count = 0
    for skill in candidate:
        if skill in seen or skill in requested_set:
            continue
        selected.append(skill)
        seen.add(skill)
        extra_count += 1
        if extra_count >= max_extra:
            break

    return selected


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
        has_project_endpoint = bool(_foundry_project_endpoint())
        raise InvalidInputError(
            "Foundry mc-matcher client not initialized. "
            f"openai_available={_openai_available}, "
            f"has_api_key={has_api_key}, "
            f"has_project_endpoint={has_project_endpoint}. "
            "Required configuration: AZURE_OPENAI_KEY and a Foundry project endpoint. "
            "Supported keys: AZURE_AI_PROJECT_ENDPOINT, FOUNDRY_PROJECT_ENDPOINT, "
            "AZURE_FOUNDRY_PROJECT_ENDPOINT, or FOUNDRY_ENDPOINT + FOUNDRY_PROJECT."
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
    project_endpoint = _foundry_project_endpoint().rstrip("/")
    if not api_key:
        logger.warning("mc-matcher client not initialized: missing AZURE_OPENAI_KEY")
        return None

    if not project_endpoint:
        logger.warning(
            "mc-matcher client not initialized: missing Foundry project endpoint "
            "(AZURE_AI_PROJECT_ENDPOINT/FOUNDRY_PROJECT_ENDPOINT/FOUNDRY_ENDPOINT+FOUNDRY_PROJECT)"
        )
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


def _chat_history_table_name() -> str:
    return _settings_value(
        "CHAT_HISTORY_TABLE_NAME",
        "AI_MATCHER_HISTORY_TABLE_NAME",
        default="AiMatcherChatHistory",
    )


def _chat_history_connection_string() -> str:
    return _settings_value(
        "AzureWebJobsStorage",
        "STORAGE_ACCOUNT_CONNECTION_STRING",
        "STORAGE_CONNECTION_STRING",
        default=settings.storage_connection_string or "",
    )


def _sanitize_table_key_part(value: Any, fallback: str) -> str:
    raw = _safe_str(value) or fallback
    sanitized = re.sub(r"[\\/#?\x00-\x1F\x7F]", "_", raw)
    return sanitized[:256] or fallback


def _chat_partition_key(user_id: str, chat_id: str) -> str:
    return f"{_sanitize_table_key_part(user_id, 'user')}|{_sanitize_table_key_part(chat_id, 'chat')}"


def _chat_exchange_row_key() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    return f"e_{ts}_{uuid4().hex[:8]}"


def _truncate_text(value: str, max_len: int = 30000) -> str:
    if len(value) <= max_len:
        return value
    return value[:max_len]


def _get_chat_history_table_client() -> Any:
    global _chat_history_table_client
    if _chat_history_table_client is not None:
        return _chat_history_table_client

    if TableServiceClient is None:
        raise InvalidInputError(
            "Chat history table support unavailable: install azure-data-tables"
        )

    connection_string = _chat_history_connection_string()
    if not connection_string:
        raise InvalidInputError("Missing Azure storage connection string for chat history")

    table_name = _chat_history_table_name()
    service = TableServiceClient.from_connection_string(conn_str=connection_string)
    service.create_table_if_not_exists(table_name=table_name)
    _chat_history_table_client = service.get_table_client(table_name=table_name)
    return _chat_history_table_client


def _chat_history_load_thread(user_id: str, chat_id: str) -> dict[str, Any] | None:
    client = _get_chat_history_table_client()
    pk = _chat_partition_key(user_id, chat_id)
    try:
        entity = client.get_entity(partition_key=pk, row_key="thread")
        return dict(entity)
    except Exception:
        return None


def _chat_history_append_turn(
    *,
    user_id: str,
    chat_id: str,
    user_request: str,
    assistant_response: str,
    previous_response_id: str | None,
    response_id: str | None,
    agent_name: str,
    model_name: str | None,
) -> dict[str, Any]:
    client = _get_chat_history_table_client()
    now = datetime.now(timezone.utc).isoformat()
    pk = _chat_partition_key(user_id, chat_id)

    turn_count = 0
    existing_thread = _chat_history_load_thread(user_id, chat_id)
    if existing_thread:
        try:
            turn_count = int(existing_thread.get("turn_count") or 0)
        except Exception:
            turn_count = 0

    thread_entity = {
        "PartitionKey": pk,
        "RowKey": "thread",
        "entity_type": "thread",
        "user_id": user_id,
        "chat_id": chat_id,
        "turn_count": turn_count + 1,
        "last_response_id": _safe_str(response_id) or _safe_str(previous_response_id) or None,
        "agent_name": agent_name,
        "model_name": model_name,
        "updated_at": now,
    }
    if not existing_thread:
        thread_entity["created_at"] = now

    exchange_entity = {
        "PartitionKey": pk,
        "RowKey": _chat_exchange_row_key(),
        "entity_type": "exchange",
        "user_id": user_id,
        "chat_id": chat_id,
        "turn_number": turn_count + 1,
        "request_text": _truncate_text(_safe_str(user_request)),
        "response_text": _truncate_text(_safe_str(assistant_response)),
        "previous_response_id": _safe_str(previous_response_id) or None,
        "foundry_response_id": _safe_str(response_id) or None,
        "agent_name": agent_name,
        "model_name": model_name,
        "created_at": now,
    }

    client.upsert_entity(entity=thread_entity, mode="merge")
    client.upsert_entity(entity=exchange_entity, mode="merge")

    return {
        "partition_key": pk,
        "turn_count": turn_count + 1,
        "persisted": True,
    }


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


# ---------------------------------------------------------------------------
# Candidate coherence evaluator — unico controllo di qualita' sopravvissuto a
# deterministic match evaluator + response judger. Non ricalcola punteggi via
# match_features (mai popolato da searcher-wrapper): giudica la coerenza dei
# candidati gia' restituiti rispetto alla richiesta originale, li riordina, e
# propone eventuali domande di chiarimento. I valori dei candidati non sono
# mai riscritti dall'LLM: solo l'ordine (per indice) viene applicato ai record
# originali, cosi' un'allucinazione del modello non puo' alterare i dati.
# ---------------------------------------------------------------------------

_CANDIDATE_EVALUATOR_SYSTEM = """\
Sei un valutatore di coerenza per il sistema di ricerca candidati MC Flash.
Il tuo UNICO compito e' giudicare quanto ciascun candidato ricevuto sia coerente
con la richiesta originale dell'utente, e riordinarli dal piu' al meno coerente.

NON devi:
- Inventare candidati che non sono nella lista
- Omettere candidati presenti nella lista
- Modificare i valori dei campi di un candidato
- Assegnare punteggi numerici: riordina soltanto

Restituisci SOLO JSON valido, senza markdown o commenti.
"""

_CANDIDATE_EVALUATOR_PROMPT = """\
RICHIESTA ORIGINALE DELL'UTENTE:
_ORIGINAL_REQUEST_

CANDIDATI (ognuno ha un campo _idx univoco):
_CANDIDATES_JSON_

Valuta la coerenza di ciascun candidato rispetto alla richiesta e riordinali dal piu' al meno coerente.

Restituisci esattamente questo JSON (nessun markdown):
{
    "verdict": "strong|partial|weak|none",
    "ordered_idx": [0, 1, 2],
    "clarifying_questions": []
}

- verdict: valutazione complessiva di quanto la lista risponde alla richiesta
- ordered_idx: DEVE contenere esattamente gli stessi _idx ricevuti in input (stesso insieme, stesso numero, nessuna aggiunta/omissione/duplicazione), solo riordinati per coerenza decrescente
- clarifying_questions: 0-3 domande brevi che aiuterebbero a migliorare la risposta se verdict e' "weak" o "none"; lista vuota se verdict e' "strong" o "partial"
"""


def _run_candidate_coherence_evaluator(
    original_request: str,
    hits: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Riordina `hits` (invariati nei campi) per coerenza con `original_request` e
    propone eventuali domande di chiarimento. Nessun ricalcolo di score deterministico.

    Fallback (ordine originale, verdict "unknown") quando: meno di 2 candidati,
    nessun testo libero da giudicare, client LLM non configurato, la chiamata
    fallisce, o la risposta non e' un riordino valido dello stesso identico insieme.
    """
    fallback = {"candidates": hits, "verdict": "unknown", "clarifying_questions": []}

    if len(hits) <= 1 or not original_request:
        return fallback

    client = _get_judge_client()
    if client is None:
        return fallback

    indexed_candidates = [{"_idx": i, **hit} for i, hit in enumerate(hits)]
    user_msg = (
        _CANDIDATE_EVALUATOR_PROMPT
        .replace("_ORIGINAL_REQUEST_", original_request)
        .replace("_CANDIDATES_JSON_", json.dumps(indexed_candidates, ensure_ascii=False))
    )

    try:
        resp = client.chat.completions.create(
            model=settings.azure_openai_model,
            messages=[
                {"role": "system", "content": _CANDIDATE_EVALUATOR_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=600,
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw = (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.warning("Candidate coherence evaluator LLM call failed: %s", exc)
        return fallback

    parsed = _extract_json_safe(raw)
    if not parsed:
        return fallback

    ordered_idx = parsed.get("ordered_idx")
    expected_idx_set = set(range(len(hits)))
    if (
        not isinstance(ordered_idx, list)
        or len(ordered_idx) != len(hits)
        or set(ordered_idx) != expected_idx_set
    ):
        logger.warning("Candidate coherence evaluator returned an invalid ordering, keeping original order")
        ordered_hits = hits
    else:
        ordered_hits = [hits[i] for i in ordered_idx]

    verdict = _safe_str(parsed.get("verdict")).lower()
    if verdict not in {"strong", "partial", "weak", "none"}:
        verdict = "unknown"

    clarifying_questions = [
        _safe_str(q) for q in (parsed.get("clarifying_questions") or []) if _safe_str(q)
    ][:3]

    return {
        "candidates": ordered_hits,
        "verdict": verdict,
        "clarifying_questions": clarifying_questions,
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

    if isinstance(search_response.get("hits"), list) and search_response["hits"]:
        logger.info(
            "search hit keys=%s",
            list(search_response["hits"][0].keys()),
        )

    requested_skills = _lower_list(search_request.get("skills")) if isinstance(search_request, dict) else []
    raw_hits = search_response.get("hits") if isinstance(search_response.get("hits"), list) else []
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

    return {
        "original_request": original_request,
        "interpreted_request": search_request,
        "search_response": {
            "hits": evaluation["candidates"],
        },
        "verdict": evaluation["verdict"],
        "clarifying_questions": evaluation["clarifying_questions"],
    }


@app.route(route="match-evaluator-wrapper", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
@http_error_handler
async def match_evaluator_wrapper(req: func.HttpRequest):
    """
    POST /api/match-evaluator-wrapper

    Valutatore di coerenza candidati/richiesta (LLM), utilizzabile standalone:
    - accetta original_request + candidates (o search_response.hits)
    - riordina i candidati per coerenza (campi mai alterati, solo l'ordine)
    - propone eventuali domande di chiarimento
    - fallback: ordine originale, verdict "unknown", se l'LLM non e' disponibile

    Nota: /api/searcher-wrapper chiama gia' questa stessa logica internamente
    dopo ogni ricerca, quindi in genere non serve invocare questa route a parte.
    """
    payload = _body_params(req)
    if not payload:
        payload = _payload_from_query(req)
    if not payload:
        raise InvalidInputError("Missing or invalid JSON body")

    original_request = _safe_str(
        _first_non_empty(payload.get("original_request"), payload.get("query"))
    )
    candidates = _extract_coherence_evaluator_candidates(payload)

    return await asyncio.to_thread(
        _run_candidate_coherence_evaluator, original_request, candidates
    )


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
    parsed_output = _extract_json_safe(output_text) if output_text else None

    result_payload: dict[str, Any]
    if isinstance(parsed_output, dict):
        result_payload = parsed_output
    else:
        result_payload = {"raw_text": output_text}

    response_id = _safe_str(raw_response.get("id")) or None
    assistant_response_text = output_text if output_text else json.dumps(result_payload, ensure_ascii=False)

    answer_text = _safe_str(output_text)
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
    }

    if include_result:
        response_payload["result"] = result_payload
    if include_raw_response:
        response_payload["raw_response"] = raw_response

    return response_payload


@app.route(route="mcflash/candidati", methods=["GET", "POST"])
@http_error_handler
async def mcflash_candidati(req: func.HttpRequest):
    """
    GET|POST /api/mcflash/candidati

    Estrae candidati dal servizio MCFlash remoto.
    Supporta anche filtri opzionali lato API wrapper.
    Restituisce tutti i campi originali della risposta upstream.
    """
    payload = _body_params(req)
    if not payload:
        payload = _payload_from_query(req)

    query = _safe_str(_first_non_empty(payload.get("q"), payload.get("query"), req.params.get("q")))
    role = _safe_str(_first_non_empty(payload.get("role"), req.params.get("role"))) or None
    # MCFlash returns and filters by "sede"; accept "location" only as input alias.
    sede = _safe_str(
        _first_non_empty(
            payload.get("sede"),
            payload.get("location"),
            req.params.get("sede"),
            req.params.get("location"),
        )
    ) or None
    work_mode_raw = _safe_str(_first_non_empty(payload.get("work_mode"), req.params.get("work_mode"))).lower()
    work_mode = work_mode_raw if work_mode_raw and work_mode_raw not in {"unknown", "any"} else None
    seniority = _safe_str(_first_non_empty(payload.get("seniority"), req.params.get("seniority"))) or None
    language = _safe_str(_first_non_empty(payload.get("language"), req.params.get("language"))) or None
    lingue = _safe_str(_first_non_empty(payload.get("lingue"), req.params.get("lingue"), language)) or None
    budget = _safe_str(_first_non_empty(payload.get("budget"), req.params.get("budget"))) or None
    disponibilita = _safe_str(
        _first_non_empty(
            payload.get("disponibilita"),
            payload.get("availability"),
            req.params.get("disponibilita"),
            req.params.get("availability"),
        )
    ) or None

    limit = _parse_int(
        _first_non_empty(payload.get("limit"), payload.get("max_items"), req.params.get("limit")),
        default=100,
    )
    limit = min(limit, 1000)

    offset_raw = _first_non_empty(payload.get("offset"), req.params.get("offset"))
    try:
        offset = int(str(offset_raw).strip()) if offset_raw is not None else 0
    except Exception as exc:
        raise InvalidInputError(f"Invalid integer value: {offset_raw}") from exc
    if offset < 0:
        raise InvalidInputError("offset must be >= 0")

    client = _get_mcflash_candidates_client()
    try:
        if any([query, role, sede, work_mode, seniority, lingue, budget, disponibilita]):
            items = await client.filter_candidates(
                limit=limit,
                offset=offset,
                query=query or None,
                role=role,
                work_mode=work_mode,
                language=language,
                sede=sede,
                seniority=seniority,
                lingue=lingue,
                budget=budget,
                disponibilita=disponibilita,
            )
        else:
            items = await client.fetch_candidates(limit=limit, offset=offset)
    except MCFlashApiError as exc:
        raise InvalidInputError(str(exc)) from exc

    return {
        "items": items,
        "meta": {
            "total": len(items),
            "limit": limit,
            "offset": offset,
            "source": "mcflash_candidates_api",
        },
    }


@app.route(route="mcflash/candidati/details", methods=["POST"])
@http_error_handler
async def mcflash_candidati_details(req: func.HttpRequest):
    """
    POST /api/mcflash/candidati/details

    Recupera il dettaglio candidato da endpoint MCFlash partendo da Id
    (con fallback su Nome/Nomi per compatibilita').
    """
    payload = _body_params(req)
    if not payload:
        payload = _payload_from_query(req)

    match_key = _safe_str(
        _first_non_empty(
            payload.get("match_key"),
            payload.get("candidate_id"),
            payload.get("id"),
            payload.get("email"),
            req.params.get("match_key"),
            req.params.get("candidate_id"),
            req.params.get("id"),
            req.params.get("email"),
        )
    ).lower()
    if not match_key:
        raise InvalidInputError("Missing required field: match_key|candidate_id|id")

    client = _get_mcflash_candidates_client()
    try:
        candidate = await client.find_candidate(match_key)
    except MCFlashApiError as exc:
        raise InvalidInputError(str(exc)) from exc

    return {
        "found": candidate is not None,
        "candidate": candidate,
        "source": "mcflash_candidates_api",
    }


@app.route(route="mcflash/candidati/{match_key}", methods=["GET"])
@http_error_handler
async def mcflash_candidati_details_by_path(req: func.HttpRequest):
    """
    GET /api/mcflash/candidati/{match_key}

    Variante REST del dettaglio candidato su endpoint MCFlash.
    """
    match_key = _safe_str(req.route_params.get("match_key")).lower()
    if not match_key:
        raise InvalidInputError("Missing route param: match_key")

    client = _get_mcflash_candidates_client()
    try:
        candidate = await client.find_candidate(match_key)
    except MCFlashApiError as exc:
        raise InvalidInputError(str(exc)) from exc

    return {
        "found": candidate is not None,
        "candidate": candidate,
        "source": "mcflash_candidates_api",
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




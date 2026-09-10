"""Client e invocazione degli agenti Foundry via Responses API.

Estratto da function_app.py senza modifiche di comportamento. Unico consumer:
la route `POST /api/ai-matcher-wrapper`.

Contiene:
- risoluzione del project endpoint Foundry dalle impostazioni;
- client OpenAI (singleton per istanza) puntato su `<project>/openai/v1/`;
- retry con backoff sui soli errori di rate limit;
- normalizzazione della risposta e separazione del trailer
  `{"candidates": [...]}` dal testo discorsivo dell'agente.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from core.config import settings
from core.errors import InvalidInputError
from utils.app_settings import settings_value
from utils.values import extract_json_safe, first_non_empty, safe_str

try:
    from openai import OpenAI as _OpenAI
    _openai_available = True
except ImportError:  # pragma: no cover - dipende dall'ambiente di deploy
    _OpenAI = None
    _openai_available = False

logger = logging.getLogger(__name__)

_mc_matcher_client: Any = None


# =========================================================
# Endpoint / client
# =========================================================

def foundry_project_endpoint() -> str:
    explicit = settings_value(
        "AZURE_AI_PROJECT_ENDPOINT",
        "FOUNDRY_PROJECT_ENDPOINT",
        "AZURE_FOUNDRY_PROJECT_ENDPOINT",
        default="",
    )
    if explicit:
        return explicit.rstrip("/")

    endpoint = settings_value("FOUNDRY_ENDPOINT", default="").rstrip("/")
    project = settings_value(
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


def get_mc_matcher_client() -> Any:
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

    api_key = settings_value("AZURE_OPENAI_KEY", default=settings.azure_openai_key or "")
    project_endpoint = foundry_project_endpoint().rstrip("/")
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


# =========================================================
# Response helpers
# =========================================================

def response_to_plain_dict(response: Any) -> dict[str, Any]:
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


def responses_api_version(version: str | None, minimum: str = "2025-03-01-preview") -> str:
    """Ensure api-version is compatible with Azure OpenAI Responses API."""
    candidate = (version or "").strip()
    if not candidate:
        return minimum

    # Expected format is YYYY-MM-DD[-preview]. Compare by date part only.
    date_part = candidate[:10]
    if len(date_part) != 10 or date_part[4] != "-" or date_part[7] != "-":
        return minimum

    return candidate if date_part >= minimum[:10] else minimum


# =========================================================
# Retry policy
# =========================================================

retry_max_attempts = max(
    1,
    int(settings_value("FOUNDRY_RETRY_MAX_ATTEMPTS", default="3") or "3"),
)


def wait_for_foundry_slot(agent_name: str) -> None:
    """Hook per eventuale throttling locale/concurrency control."""
    _ = agent_name


def is_rate_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "429" in text
        or "rate limit" in text
        or "too_many_requests" in text
        or "rate_limited" in text
    )


def backoff_seconds(exc: Exception, attempt: int) -> float:
    text = str(exc)
    retry_after = re.search(r"retry[-_ ]?after\D*(\d+)", text, re.IGNORECASE)
    if retry_after:
        try:
            return max(0.5, float(retry_after.group(1)))
        except Exception:
            pass
    return min(8.0, 0.75 * (2 ** (attempt - 1)))


# =========================================================
# Invocazione agente
# =========================================================

def run_foundry_agent(
    *,
    agent_name: str,
    message: str,
    model_name: str | None = None,
    previous_response_id: str | None = None,
) -> Any:
    """Invoke Foundry modern agent via Responses API with agent_reference."""
    openai = get_mc_matcher_client()
    if openai is None:
        has_api_key = bool(settings_value("AZURE_OPENAI_KEY", default=settings.azure_openai_key or ""))
        has_project_endpoint = bool(foundry_project_endpoint())
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

    for attempt in range(1, retry_max_attempts + 1):
        wait_for_foundry_slot(agent_name)
        try:
            return openai.responses.create(**kwargs)
        except Exception as exc:
            if not is_rate_limit_error(exc) or attempt >= retry_max_attempts:
                raise

            backoff = backoff_seconds(exc, attempt)
            logger.warning(
                "foundry_rate_limited agent=%s attempt=%s/%s backoff=%.2fs",
                agent_name,
                attempt,
                retry_max_attempts,
                backoff,
            )
            time.sleep(backoff)

    raise RuntimeError("Foundry invocation exhausted retries")


# =========================================================
# Parsing della risposta dell'agente ai_matcher
# =========================================================

def normalise_ai_matcher_candidates(raw: Any) -> list[dict[str, Any]]:
    """Keep only id_mcflash + trigramma from each trailer entry, verbatim.

    `trigramma` falls back to `nome` because that hit field carries the MCFlash
    anonymised code (e.g. "ELO"), which is what the trailer is meant to expose.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        out.append(
            {
                "id_mcflash": entry.get("id_mcflash"),
                "trigramma": first_non_empty(entry.get("trigramma"), entry.get("nome")),
            }
        )
    return out


def split_ai_matcher_answer(output_text: str) -> tuple[str, list[dict[str, Any]]]:
    """Peel the machine-readable candidates trailer off the agent's prose answer.

    The agent appends, after the natural-language answer, a marker line followed
    by a single JSON object: {"candidates":[{"id_mcflash":..,"trigramma":..}, ...]}.
    Parsing is anchored on that JSON object, so it is independent of the exact
    marker string. Returns (prose_answer, candidates). When the agent instead
    returns the whole turn as one JSON object, prose comes from its answer field.
    """
    text = safe_str(output_text)
    if not text:
        return "", []

    # Case 1: the entire output is a JSON object carrying both answer + candidates.
    whole = extract_json_safe(text) if text.lstrip().startswith("{") else None
    if isinstance(whole, dict) and "candidates" in whole:
        prose = safe_str(
            first_non_empty(
                whole.get("answer"),
                whole.get("final_answer"),
                whole.get("response"),
                whole.get("raw_text"),
            )
        )
        return prose, normalise_ai_matcher_candidates(whole.get("candidates"))

    # Case 2: prose followed by a trailing {"candidates": [...]} object.
    matches = list(re.finditer(r'\{\s*"candidates"\s*:', text))
    if not matches:
        return text.strip(), []

    trailer_start = matches[-1].start()
    trailer = extract_json_safe(text[trailer_start:])
    if not isinstance(trailer, dict) or "candidates" not in trailer:
        return text.strip(), []

    prose_lines = text[:trailer_start].rstrip().splitlines()
    # Drop trailing lines that are the trailer's marker rather than prose:
    # "<<<CANDIDATES_JSON>>>", "CANDIDATI:", an opening ```json fence, a rule of
    # dashes, etc. Anything short that names JSON/candidates or is pure punctuation.
    def _is_marker_line(line: str) -> bool:
        stripped = line.strip()
        if not stripped:
            return True
        # Pure punctuation / rule / bracket line, or a ```json fence opener.
        if re.fullmatch(r"[`~<>=\-_*#:\s]+", stripped):
            return True
        if re.fullmatch(r"`{3,}\s*json\s*", stripped, flags=re.IGNORECASE):
            return True
        # A bracketed / label-style marker: no lowercase letters (so real prose
        # like "Ecco i candidati:" is never stripped), short, names json/candidat.
        if (
            len(stripped) <= 40
            and not re.search(r"[a-zà-ÿ]", stripped)
            and re.search(r"JSON|CANDIDAT", stripped, flags=re.IGNORECASE)
        ):
            return True
        return False

    while prose_lines and _is_marker_line(prose_lines[-1]):
        prose_lines.pop()
    prose = "\n".join(prose_lines).rstrip()

    return prose, normalise_ai_matcher_candidates(trailer.get("candidates"))

"""Candidate coherence evaluator — unico controllo di qualita' sopravvissuto a
deterministic match evaluator + response judger.

Non ricalcola punteggi via match_features (mai popolato da searcher-wrapper):
giudica la coerenza dei candidati gia' restituiti rispetto alla richiesta
originale, li riordina, e propone eventuali domande di chiarimento. I valori dei
candidati non sono mai riscritti dall'LLM: solo l'ordine (per indice) viene
applicato ai record originali, cosi' un'allucinazione del modello non puo'
alterare i dati.

Estratto da function_app.py senza modifiche di comportamento. Consumer:
`POST /api/searcher-wrapper`.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from core.config import settings
from utils.values import extract_json_safe, safe_str

try:
    from openai import AzureOpenAI as _AzureOpenAI
    _openai_available = True
except ImportError:  # pragma: no cover - dipende dall'ambiente di deploy
    _AzureOpenAI = None
    _openai_available = False

logger = logging.getLogger(__name__)

# Singleton judge client — created once per Function App instance
_judge_client: Any = None

JUDGE_DEFAULT_TIMEOUT = getattr(settings, "judge_timeout_seconds", 10)


def get_judge_client() -> Any:
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


CANDIDATE_EVALUATOR_SYSTEM = """\
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

CANDIDATE_EVALUATOR_PROMPT = """\
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


def run_candidate_coherence_evaluator(
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

    client = get_judge_client()
    if client is None:
        return fallback

    indexed_candidates = [{"_idx": i, **hit} for i, hit in enumerate(hits)]
    user_msg = (
        CANDIDATE_EVALUATOR_PROMPT
        .replace("_ORIGINAL_REQUEST_", original_request)
        .replace("_CANDIDATES_JSON_", json.dumps(indexed_candidates, ensure_ascii=False))
    )

    try:
        resp = client.chat.completions.create(
            model=settings.azure_openai_model,
            messages=[
                {"role": "system", "content": CANDIDATE_EVALUATOR_SYSTEM},
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

    parsed = extract_json_safe(raw)
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

    verdict = safe_str(parsed.get("verdict")).lower()
    if verdict not in {"strong", "partial", "weak", "none"}:
        verdict = "unknown"

    clarifying_questions = [
        safe_str(q) for q in (parsed.get("clarifying_questions") or []) if safe_str(q)
    ][:3]

    return {
        "candidates": ordered_hits,
        "verdict": verdict,
        "clarifying_questions": clarifying_questions,
    }

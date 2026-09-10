"""Coercizioni di valore condivise (stringhe, liste, JSON best-effort).

Copie verbatim delle implementazioni che vivevano in function_app.py: le stesse
funzioni esistono, identiche, anche in services/search_pipeline.py — quel
duplicato NON viene toccato qui, per tenere questo passaggio circoscritto al
codice Foundry / chat history.
"""
from __future__ import annotations

import json
from typing import Any


def safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def lower_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip().lower() for v in value if str(v).strip()]
    if isinstance(value, str):
        parts = [p.strip().lower() for p in value.split(",")]
        return [p for p in parts if p]
    return []


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def first_non_empty(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def extract_json_safe(raw: str) -> dict[str, Any] | None:
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

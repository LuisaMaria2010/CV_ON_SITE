"""Parsing di parametri e body delle richieste HTTP delle Function.

Copie verbatim delle implementazioni che vivevano in function_app.py.

Nota su `parse_int`: quando il valore e' <= 0 solleva "max_items must be > 0".
Il messaggio nomina un parametro specifico anche quando la funzione e' usata
per altri campi: e' il comportamento storico e viene mantenuto tale qui.
"""
from __future__ import annotations

import json
from typing import Any

import azure.functions as func

from core.errors import InvalidInputError


def parse_bool(raw: str | None, default: bool) -> bool:
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


def parse_int(raw: str | None, default: int) -> int:
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


def parse_float(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, str) and raw.strip() == "":
        return None
    try:
        return float(raw)
    except Exception as exc:
        raise InvalidInputError(f"Invalid number value: {raw}") from exc


def body_params(req: func.HttpRequest) -> dict:
    body = req.get_body()
    if not body:
        return {}

    try:
        payload = json.loads(body.decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def payload_from_query(req: func.HttpRequest, param_name: str = "payload_json") -> dict:
    raw = req.params.get(param_name)
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}

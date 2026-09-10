"""Lettura impostazioni applicative: env prima, poi fallback su local.settings.json.

Estratto da function_app.py senza modifiche di comportamento: stesso ordine di
precedenza (env var non vuota -> Values di local.settings.json -> default) e
stessa tolleranza agli errori di parsing del file locale.
"""
from __future__ import annotations

import json
import os


def settings_value(*keys: str, default: str = "") -> str:
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

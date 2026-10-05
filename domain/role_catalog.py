"""Catalogo dei ruoli MC: le chiavi di infra/mcflash_budget_placeholders.json.

Il ruolo in alto nel CV MCFlash ("Information > Ruolo") si sceglie da qui, cosi'
e' coerente con i ruoli (e le tariffe junior/mid/senior) usati in MCFlash.
"""
from __future__ import annotations

import difflib
import json
import re
from functools import lru_cache
from pathlib import Path

CATALOG_PATH = Path(__file__).resolve().parent.parent / "infra" / "mcflash_budget_placeholders.json"

# Parole che nel CV vanno scritte in un modo preciso (sigle, marchi).
_SPECIAL_WORDS = {
    ".net": ".NET", "ai": "AI", "am": "AM", "bi": "BI", "bmc": "BMC", "sap": "SAP", "abap": "ABAP",
    "soc": "SOC", "iam": "IAM", "ios": "iOS", "it": "IT", "ml": "ML", "rpa": "RPA", "sas": "SAS",
    "sql": "SQL", "dba": "DBA", "etl": "ETL", "pl": "PL", "devops": "DevOps", "vmware": "VMware",
    "qlik": "Qlik", "maui": "MAUI",
}
_LOWER_WORDS = {"e", "di", "del", "della", "de", "a", "per"}


@lru_cache(maxsize=1)
def load_catalog() -> dict[str, dict[str, float]]:
    """{ruolo: {junior, mid, senior}} cosi' come sta nel JSON (chiavi minuscole)."""
    with open(CATALOG_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return {str(k).strip().lower(): v for k, v in raw.items() if str(k).strip() and isinstance(v, dict)}


def role_names() -> list[str]:
    return sorted(load_catalog())


def _key(text: str) -> str:
    text = (text or "").casefold().strip()
    text = text.replace("full stack", "fullstack").replace("full-stack", "fullstack")
    text = text.replace("front end", "frontend").replace("front-end", "frontend")
    text = text.replace("back end", "backend").replace("back-end", "backend")
    return re.sub(r"\s+", " ", text)


def match_catalog_role(text: str | None) -> str | None:
    """Ruolo del catalogo corrispondente a `text`: uguale, con varianti di scrittura, o molto simile."""
    if not text or not text.strip():
        return None
    catalog = role_names()
    wanted = _key(text)
    by_key = {}
    for name in catalog:
        by_key.setdefault(_key(name), name)
    if wanted in by_key:
        return by_key[wanted]
    close = difflib.get_close_matches(wanted, list(by_key), n=1, cutoff=0.88)
    return by_key[close[0]] if close else None


def display_role(name: str | None) -> str:
    """Come scriverlo nel CV: "data scientist" -> "Data Scientist", ".net developer" -> ".NET Developer"."""
    if not name:
        return ""
    words = []
    for i, word in enumerate(name.split()):
        if i > 0 and word in _LOWER_WORDS:
            words.append(word)
            continue
        parts = [_SPECIAL_WORDS.get(p, p[:1].upper() + p[1:]) for p in word.split("-")]
        words.append("-".join(parts))
    return " ".join(words)


def prompt_catalog_text() -> str:
    """Elenco per il prompt, una voce per riga."""
    return "\n".join(f"- {name}" for name in role_names())

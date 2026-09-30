"""Perimetro MCFlash della ricerca: il `site`.

Il frontend manda `site` nel body di /api/ai-matcher-wrapper
('mcflashcandidati' o 'mcflashrisorse'); l'agente lo ripassa a
/api/searcher-wrapper e il pipeline di ricerca seleziona SOLO i record MCFlash
visibili da quel site. Nessun candidato fuori da quel perimetro entra nella
ricerca, in nessuna fase (hard select, allargamento, second pass).

Visibilita':
- mcflashrisorse   -> record di mcflashrisorse E di mcflashcandidati
- mcflashcandidati -> solo record di mcflashcandidati

Nel DB il valore puo' essere un host ('mcflashcandidati.mcengineering.eu') o
un nome secco ('mcflashrisorse'): il confronto avviene sul primo segmento del
nome host, senza distinzione di maiuscole.
"""
from __future__ import annotations

from typing import Any

from core.errors import InvalidInputError
from utils.values import safe_str

SITE_CANDIDATI = "mcflashcandidati"
SITE_RISORSE = "mcflashrisorse"
ALLOWED_SITES = frozenset({SITE_CANDIDATI, SITE_RISORSE})

# site richiesto -> site dei record che puo' vedere
VISIBLE_SITES: dict[str, frozenset[str]] = {
    SITE_RISORSE: frozenset({SITE_RISORSE, SITE_CANDIDATI}),
    SITE_CANDIDATI: frozenset({SITE_CANDIDATI}),
}


def site_key(value: Any) -> str:
    """'https://McFlashCandidati.mcengineering.eu/' -> 'mcflashcandidati'."""
    raw = safe_str(value).strip().lower()
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    return raw.split("/", 1)[0].split(".", 1)[0].strip()


def resolve_site(payload: dict[str, Any] | None) -> str:
    """Il site richiesto, normalizzato. Assente o non riconosciuto -> 400."""
    payload = payload if isinstance(payload, dict) else {}
    key = site_key(payload.get("site"))
    if key not in ALLOWED_SITES:
        allowed = ", ".join(sorted(ALLOWED_SITES))
        raise InvalidInputError(f"'site' is required and must be one of: {allowed}")
    return key


def record_site(record: dict[str, Any]) -> str:
    """Il site di un record MCFlash (nome del campo senza distinzione di maiuscole)."""
    for key, value in record.items():
        if isinstance(key, str) and key.lower() == "site":
            return site_key(value)
    return ""


def visible_sites(site: str) -> frozenset[str]:
    """I site dei record visibili da chi si connette con `site` (vuoto se sconosciuto)."""
    return VISIBLE_SITES.get(site_key(site), frozenset())


def record_visible(record: Any, site: str) -> bool:
    """Il record MCFlash e' visibile da chi si connette con `site`?"""
    return isinstance(record, dict) and record_site(record) in visible_sites(site)

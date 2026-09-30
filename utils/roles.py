"""Ruoli generici che non sono ruoli.

L'agente a volte mette in `role` una parola che indica solo "una persona"
("Figura con esperienza X" -> role="Figura"). Come ruolo vero azzera la ricerca:
nessun record MCFlash e nessun CV ha ruolo "Figura". Un ruolo fatto SOLO di
parole generiche (piu' eventuali qualificatori come "senior") viene scartato;
se contiene anche una parola reale ("Consulente SAP") resta com'e'.
"""
from __future__ import annotations

import re
from typing import Any

_GENERIC_ROLE_WORDS = frozenset({
    "figura", "figure", "profilo", "profili", "persona", "persone", "risorsa", "risorse",
    "qualcuno", "candidato", "candidata", "candidati", "consulente", "consulenti",
    "professionista", "professionisti", "esperto", "esperta", "esperti", "specialista",
    "collaboratore", "collaboratrice", "tecnico", "tecnica", "elemento", "nominativo",
    "resource", "person", "profile", "candidate", "consultant", "expert", "someone",
})
_QUALIFIERS = frozenset({
    "senior", "junior", "mid", "middle", "medium", "sr", "jr", "un", "una", "uno", "di", "del",
    "della", "con", "qualificato", "qualificata", "valido", "valida", "esperienza", "it",
})


def drop_generic_role(role: Any) -> str | None:
    """Il ruolo, oppure None se e' composto solo da parole generiche."""
    text = str(role or "").strip()
    if not text:
        return None
    tokens = [t for t in re.split(r"[^\wÀ-ÿ]+", text.lower()) if t]
    meaningful = [t for t in tokens if t not in _QUALIFIERS]
    if meaningful and all(t in _GENERIC_ROLE_WORDS for t in meaningful):
        return None
    return text

"""Aiuti per la ricerca estesa dei CV (services/cv_finder.py): codici simili e
parole del ruolo. Le regole per collegare un CV a un record (intestazione, eta',
mestiere, livelli, scelta) sono in domain/cv_matching.py, uniche per la
ricostruzione della tabella CV -> MCFlash e per la dashboard."""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

_STOP = {"di", "and", "the", "e", "of"}


def _plain(value: Any) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def letters(code: Any) -> str:
    return re.sub(r"[^A-Za-z]", "", _plain(code)).upper()


def code_similarity(code: str, other: str) -> int:
    """Quanto due codici possono indicare la stessa persona, 0-3.

    3 = uguali; 2 = due lettere su tre al loro posto, oppure stesse lettere in
    ordine diverso (cognome davanti, refuso); 1 = stessa iniziale del nome e
    stessa prima lettera del cognome; 0 = nulla in comune.
    """
    a, b = letters(code)[:3], letters(other)[:3]
    if len(a) < 3 or len(b) < 3:
        return 0
    if a == b:
        return 3
    if sum(x == y for x, y in zip(a, b)) == 2 or sorted(a) == sorted(b):
        return 2
    if a[0] == b[0] and a[1] == b[1]:
        return 1
    return 0


def role_words(value: Any) -> set[str]:
    words = re.split(r"[^a-z0-9+#.]+", _plain(value).lower())
    return {w for w in words if len(w) > 1 and w not in _STOP}


def role_overlap(cv_role: Any, skills: Iterable[str], mc_role: Any) -> int:
    """Parole del ruolo MCFlash presenti nel ruolo o nelle skill del CV."""
    have = role_words(cv_role) | {w for s in skills or [] for w in role_words(s)}
    return len(role_words(mc_role) & have)

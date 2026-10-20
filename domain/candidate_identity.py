"""Identita' del candidato nel modello MC: trittico e nome del file CV.

Trittico: prima lettera del nome + prime due lettere del cognome, maiuscole,
senza accenti (es. Mario De Luca -> MDE, Luca D'Amico -> LDA). Non e' univoco:
a parita' di trittico i candidati si distinguono per nome.

Nome file: i CV MC si chiamano "Skill Tecnico_<TRITTICO>_MC Engineering" e le
varianti con spazi, trattini e underscore servono a non sovrascrivere il CV di
un'altra persona con lo stesso trittico. Il confronto con i file esistenti e'
case-insensitive (Blob e OneDrive non distinguono davvero le maiuscole per noi)
e ignora l'estensione, cosi' un .pdf e un .docx omonimi non convivono.

Per un candidato gia' presente si riusa il SUO nome file: stesso percorso con
contenuto nuovo = stesso document_id, versione +1, e l'indice sostituisce il
vecchio CV (vedi DocumentRegistry.should_process).
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath
from typing import Iterable

# Particelle che fanno parte del cognome ("De Luca", "Della Valle", "Van Der Berg").
_SURNAME_PARTICLES = frozenset({
    "de", "di", "da", "del", "della", "dei", "degli", "delle", "dal", "dalla",
    "la", "lo", "le", "li", "van", "von", "der", "den", "dos", "das", "du", "mc", "mac", "st", "san",
})

# Varianti del nome file, in ordine di preferenza. {t} = trittico.
FILENAME_PATTERNS: tuple[str, ...] = (
    "Skill Tecnico_{t}_MC Engineering",
    "Skill_Tecnico_{t}_MC_Engineering",
    "Skill Tecnico_{t}_MC",
    "Skill_Tecnico_{t}_MC",
    "Skill Tecnico_{t}_MC_",
    "Skill_Tecnico_{t}_MC_",
    "Skill Tecnico_{t} _MC Engineering",
    "Skill Tecnico_{t}_ MC Engineering",
    "Skill Tecnico - {t} - MC Engineering",
    "Skill-Tecnico-{t}-MC-Engineering",
    "Skill Tecnico {t} MC Engineering",
    "Skill Tecnico_{t}_MC-Engineering",
)


def _ascii_letters(text: str) -> str:
    """Solo lettere A-Z maiuscole, accenti rimossi (D'Amico -> DAMICO)."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    no_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^A-Za-z]", "", no_marks).upper()


def split_full_name(full_name: str | None) -> tuple[str | None, str | None]:
    """Divide "Nome [Secondo] Cognome" in (nome, cognome).

    Regole, dalla piu' forte:
    - token tutto maiuscolo (e non tutti lo sono): e' il cognome ("ROSSI Mario");
    - altrimenti il cognome e' l'ultimo token, con le particelle che lo precedono
      ("Mario De Luca" -> "De Luca");
    - un solo token: solo nome.
    E' un'euristica: quando l'LLM restituisce nome e cognome separati, vincono quelli.
    """
    tokens = [t for t in (full_name or "").split() if t.strip(".,")]
    if not tokens:
        return None, None
    if len(tokens) == 1:
        return tokens[0], None

    def _is_upper(tok: str) -> bool:
        letters = _ascii_letters(tok)
        return len(letters) > 1 and tok.upper() == tok

    upper = [_is_upper(t) for t in tokens]
    if any(upper) and not all(upper):
        surname = " ".join(t for t, u in zip(tokens, upper) if u)
        name = " ".join(t for t, u in zip(tokens, upper) if not u)
        return name, surname

    i = len(tokens) - 1
    while i - 1 >= 1 and tokens[i - 1].lower().strip("'") in _SURNAME_PARTICLES:
        i -= 1
    return " ".join(tokens[:i]), " ".join(tokens[i:])


def trittico(first_name: str | None, last_name: str | None) -> str | None:
    """Prima lettera del nome + prime due lettere del cognome; None se mancano dati."""
    first = _ascii_letters(first_name or "")
    last = _ascii_letters(last_name or "")
    if not first or len(last) < 2:
        return None
    return first[0] + last[:2]


def trittico_from_full_name(full_name: str | None) -> str | None:
    return trittico(*split_full_name(full_name))


def possible_tritticos(full_name: str | None) -> list[str]:
    """Tutti i trittici possibili di un nome completo: ogni divisione tra nome e
    cognome, anche con il cognome davanti. "Luisa Maria Serrano" -> LSE (nome
    "Luisa Maria"), LMA (cognome "Maria Serrano"), SLU e SLM...: con i nomi
    composti non si sa dove finisce il nome."""
    words = (full_name or "").split()
    out: list[str] = []
    for i in range(1, len(words)):
        first, last = " ".join(words[:i]), " ".join(words[i:])
        for tri in (trittico(first, last), trittico(last, first)):
            if tri and tri not in out:
                out.append(tri)
    return out


def _stem_key(filename: str) -> str:
    """Chiave di confronto: solo il nome (no cartelle, no estensione), NFC, casefold."""
    name = PurePosixPath((filename or "").replace("\\", "/")).name
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return unicodedata.normalize("NFC", stem).strip().casefold()


def propose_cv_filename(
    tri: str,
    existing_names: Iterable[str],
    *,
    extension: str = ".pdf",
    current_name: str | None = None,
) -> str:
    """Propone il nome del file CV per il trittico `tri`.

    - current_name (candidato esistente): si riusa quello, cosi' il nuovo CV
      sostituisce il vecchio nell'indice.
    - altrimenti la prima variante di FILENAME_PATTERNS che non collide con
      existing_names; esaurite le varianti, si aggiunge " (2)", " (3)", ...
    """
    if current_name:
        return PurePosixPath(current_name.replace("\\", "/")).name

    code = _ascii_letters(tri)
    if len(code) != 3:
        raise ValueError(f"Trittico non valido: {tri!r} (servono 3 lettere)")
    ext = extension if extension.startswith(".") else f".{extension}"

    taken = {_stem_key(n) for n in existing_names if n}
    for pattern in FILENAME_PATTERNS:
        stem = pattern.format(t=code)
        if _stem_key(stem) not in taken:
            return stem + ext

    base = FILENAME_PATTERNS[0].format(t=code)
    n = 2
    while _stem_key(f"{base} ({n})") in taken:
        n += 1
    return f"{base} ({n}){ext}"

"""Riga MCFlash dai dati della dashboard (bozza confermata).

Campi come quelli che legge l'API candidati (`nome` = trittico, `nomi` = nome
completo, ...), piu' email e telefono, che il CV anonimo non contiene ma
MCFlash si'. Seniority, disponibilita', budget e site non stanno nel CV: la
dashboard li raccoglie nella sezione "Dati MCFlash" del form (edited["mcflash"]).
"""
from __future__ import annotations

from typing import Any

from domain.role_catalog import display_role

# Valori in uso su MCFlash.
SENIORITY_VALUES = ("Neo", "Junior", "Medium", "Senior")
SITE_VALUES = ("mcflashcandidati.mcengineering.eu", "mcflashrisorse")

# Seniority stimata dall'estrazione (db_data/postprocess) -> valore MCFlash.
_SENIORITY_FROM_EXTRACTION = {"junior": "Junior", "mid": "Medium", "medium": "Medium", "senior": "Senior"}


def _s(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _int_or_none(value: Any) -> int | None:
    try:
        return int(float(value)) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def seniority_from_extraction(value: Any) -> str | None:
    return _SENIORITY_FROM_EXTRACTION.get(_s(value).lower())


def languages_text(languages: list[dict[str, Any]] | None) -> str:
    """Formato MCFlash: "Inglese: Avanzato, Francese: Base"."""
    parts = []
    for item in languages or []:
        lang = _s(item.get("lang"))
        if not lang:
            continue
        lang = lang[:1].upper() + lang[1:]
        level = _s(item.get("level"))
        parts.append(f"{lang}: {level}" if level else lang)
    return ", ".join(parts)


def build_record(edited: dict[str, Any]) -> dict[str, Any]:
    """Campi da scrivere su MCFlash. I valori vuoti restano None: chi scrive
    decide se ignorarli (aggiornamento) o lasciarli vuoti (creazione)."""
    extra = edited.get("mcflash") or {}
    age = edited.get("age")
    budget = extra.get("budget")
    full_name = _s(edited.get("full_name")) or " ".join(
        p for p in (_s(edited.get("first_name")), _s(edited.get("last_name"))) if p)
    return {
        "nome": _s(edited.get("trittico")).upper() or None,
        "nomi": full_name or None,
        "ruolo": display_role(edited.get("catalog_role")) or _s(edited.get("role")) or None,
        "eta": _int_or_none(age),
        "sede": _s(edited.get("location")) or None,
        "lingue": languages_text(edited.get("languages")) or None,
        "email": _s(edited.get("email")) or None,
        "telefono": _s(edited.get("phone")) or None,
        "seniority": _s(extra.get("seniority")) or None,
        "disponibilita": _s(extra.get("disponibilita")) or None,
        "budget": _int_or_none(budget),
        "site": _s(extra.get("site")) or None,
    }


def missing_for_create(record: dict[str, Any]) -> list[str]:
    """Campi senza i quali non ha senso creare un candidato su MCFlash."""
    required = {"nome": "trittico", "nomi": "nome e cognome", "ruolo": "ruolo",
                "seniority": "seniority", "site": "site"}
    return [label for key, label in required.items() if not record.get(key)]

"""
Mapper per la conversione da output LLM (RAW) a modelli dominio tipizzati.

Responsabilità:
- conversione tipi e parsing date
- pulizia minima di stringhe e liste
- nessuna business logic o deduplica avanzata

Tutte le funzioni sono pure e senza side effect.
"""

from __future__ import annotations

from datetime import date
from typing import Optional, Iterable

from core.schema import (
    LLMExtractionRaw,
    CVExtraction,
    WorkExperience,
)


# =========================================================
# Helpers
# =========================================================

def _clean(value: Optional[str]) -> Optional[str]:
    """
    Rimuove spazi iniziali/finali da una stringa e converte stringhe vuote in None.

    Args:
        value (Optional[str]): Stringa da pulire.

    Returns:
        Optional[str]: Stringa pulita o None se vuota.
    """
    if not value:
        return None
    value = value.strip()
    return value or None


def _parse_date(value: Optional[str]) -> Optional[date]:
    """
    Converte una stringa ISO (YYYY-MM-DD) in oggetto date.
    Restituisce None se la stringa non è valida o assente.

    Args:
        value (Optional[str]): Data in formato stringa.

    Returns:
        Optional[date]: Oggetto date o None.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _clean_skills(skills: Iterable[str]) -> list[str]:
    """
    Pulisce una lista di skill:
    - rimuove spazi
    - elimina elementi vuoti
    (NO deduplica: demandata al business layer)

    Args:
        skills (Iterable[str]): Lista di skill da pulire.

    Returns:
        list[str]: Lista pulita di skill.
    """
    return [s.strip() for s in skills if s and s.strip()]


def _clean_list(values: Iterable[str]) -> list[str]:
    """
    Pulisce una lista generica di stringhe:
    - rimuove spazi
    - elimina elementi vuoti

    Args:
        values (Iterable[str]): Lista di stringhe da pulire.

    Returns:
        list[str]: Lista pulita.
    """
    return [value.strip() for value in values if value and value.strip()]


# =========================================================
# Public API
# =========================================================

def to_domain(raw: LLMExtractionRaw) -> CVExtraction:
    """
    Converte l'output LLM (LLMExtractionRaw) in un modello dominio tipizzato (CVExtraction).

    Esegue solo:
    - pulizia stringhe
    - parsing date
    - pulizia minima delle liste

    Args:
        raw (LLMExtractionRaw): Output grezzo prodotto dall'LLM.

    Returns:
        CVExtraction: Modello dominio pronto per business logic o API.
    """

    work_experiences = [
        WorkExperience(
            start_date=_parse_date(e.start_date),
            end_date=_parse_date(e.end_date),
        )
        for e in raw.employment_dates
        if e.start_date or e.end_date
    ]

    return CVExtraction(
        full_name=_clean(raw.name),
        role=_clean(raw.role),
        location=_clean(raw.location),
        email=_clean(raw.email),
        phone=_clean(raw.phone),
        language=_clean(raw.language),
        birth_date=_parse_date(raw.birth_date),
        skills=_clean_skills(raw.skills),
        education_titles=_clean_list(raw.education_titles),
        certifications=_clean_list(raw.certifications),
        employment_dates=work_experiences,
    )

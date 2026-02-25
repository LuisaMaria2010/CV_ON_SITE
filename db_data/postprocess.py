"""
Business enrichment layer: arricchimento deterministico dei dati estratti dal CV.

Responsabilità:
- calcoli deterministici (età, anni esperienza, seniority)
- normalizzazioni (skills)
- nessuna AI, nessun accesso a IO o servizi esterni

Tutte le funzioni sono pure e senza side effect.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from core.schema import CVExtraction


# =========================================================
# Helpers
# =========================================================

def _compute_age(birth_date: Optional[date]) -> Optional[int]:
    """
    Calcola l'età a partire dalla data di nascita.

    Args:
        birth_date (Optional[date]): Data di nascita.

    Returns:
        Optional[int]: Età calcolata o None se non disponibile.
    """
    if not birth_date:
        return None

    today = date.today()

    age = today.year - birth_date.year - (
        (today.month, today.day) < (birth_date.month, birth_date.day)
    )

    return age if age >= 0 else None


def _years_between(start: date, end: date) -> float:
    """
    Calcola il numero di anni (float) tra due date, evitando valori negativi.

    Args:
        start (date): Data di inizio.
        end (date): Data di fine.

    Returns:
        float: Anni trascorsi (>= 0).
    """
    if end <= start:
        return 0.0

    return (end - start).days / 365.25


def _compute_experience(cv: CVExtraction) -> Optional[float]:
    """
    Calcola il totale anni di esperienza lavorativa sommando tutti i periodi.

    Args:
        cv (CVExtraction): Modello dominio con esperienze lavorative.

    Returns:
        Optional[float]: Anni totali di esperienza (arrotondati a 1 decimale) o None.
    """
    total = 0.0
    today = date.today()

    for e in cv.employment_dates:
        if not e.start_date:
            continue

        end = e.end_date or today

        total += _years_between(e.start_date, end)

    return round(total, 1) if total > 0 else None


def _seniority_from_years(years: Optional[float]) -> Optional[str]:
    """
    Determina la seniority (junior, mid, senior, lead, principal) in base agli anni di esperienza.

    Args:
        years (Optional[float]): Anni di esperienza.

    Returns:
        Optional[str]: Livello di seniority o None.
    """
    if years is None:
        return None

    if years < 2:
        return "junior"
    if years < 5:
        return "mid"
    if years < 10:
        return "senior"
    if years < 15:
        return "lead"
    return "principal"


# =========================================================
# Public API
# =========================================================

def enrich(cv: CVExtraction) -> CVExtraction:
    """
    Applica enrichment business deterministico al modello CVExtraction.

    - Normalizza skills
    - Calcola età
    - Calcola anni di esperienza
    - Determina seniority

    Args:
        cv (CVExtraction): Modello dominio da arricchire.

    Returns:
        CVExtraction: Modello arricchito.
    """

    # -------------------------
    # skills
    # -------------------------
    if cv.skills:
        cv.normalize_skills()

    # -------------------------
    # age
    # -------------------------
    cv.age = _compute_age(cv.birth_date)

    # -------------------------
    # experience
    # -------------------------
    cv.experience_years = _compute_experience(cv)

    # -------------------------
    # seniority
    # -------------------------
    cv.seniority = _seniority_from_years(cv.experience_years)

    return cv

"""Livelli nella scala dei CV MC.

Skill:  Conoscenza base < Buona conoscenza < Ottima conoscenza
Lingue: Base < Intermedio < Avanzato < Fluente < Madrelingua (come MCFlash)

Un livello che non si riconosce resta com'e': lo corregge l'utente nel form.
"""
from __future__ import annotations

import re

SKILL_BASE = "Conoscenza base"
SKILL_GOOD = "Buona conoscenza"
SKILL_GREAT = "Ottima conoscenza"

# Scale complete, dal livello piu' basso al piu' alto (menu del form della dashboard).
SKILL_LEVELS: tuple[str, ...] = (SKILL_BASE, SKILL_GOOD, SKILL_GREAT)
LANGUAGE_LEVELS: tuple[str, ...] = ("Base", "Intermedio", "Avanzato", "Fluente", "Madrelingua")

# Ordine importante: il primo che corrisponde vince ("conoscenza avanzata" -> ottima).
_SKILL_RULES: tuple[tuple[str, str], ...] = (
    (r"ottim|eccellent|excellent|expert|esper|advanced|avanzat|proficient|master|senior|\b5\s*/\s*5\b", SKILL_GREAT),
    (r"buon|good|intermediate|intermedi|discret|solid|working|\b[34]\s*/\s*5\b", SKILL_GOOD),
    (r"base|basic|beginner|elementar|scolastic|junior|familiar|^conoscenza$|\b[12]\s*/\s*5\b", SKILL_BASE),
)

_LANGUAGE_RULES: tuple[tuple[str, str], ...] = (
    (r"madre|native|mother|nativ", "Madrelingua"),
    (r"fluen", "Fluente"),
    (r"\bc[12]\b|avanzat|advanced|proficient|ottim|excellent", "Avanzato"),
    (r"pre-?intermediate", "Base"),
    (r"\bb[12]\b|intermedi|buon|good|discret|upper|working", "Intermedio"),
    (r"\ba[12]\b|base|basic|elementar|scolastic|beginner", "Base"),
)


def _match(level: str | None, rules: tuple[tuple[str, str], ...]) -> str | None:
    text = (level or "").strip()
    if not text:
        return None
    low = text.lower()
    for pattern, normalized in rules:
        if re.search(pattern, low):
            return normalized
    return text


def normalize_skill_level(level: str | None) -> str | None:
    return _match(level, _SKILL_RULES)


def normalize_language_level(level: str | None) -> str | None:
    return _match(level, _LANGUAGE_RULES)

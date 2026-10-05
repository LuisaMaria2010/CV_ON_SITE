"""Livelli di skill e lingue nella scala dei CV MC."""
from __future__ import annotations

import pytest

from domain.mc_levels import normalize_language_level, normalize_skill_level


@pytest.mark.parametrize("raw,expected", [
    ("Ottima conoscenza", "Ottima conoscenza"),
    ("Advanced", "Ottima conoscenza"),
    ("Conoscenza avanzata", "Ottima conoscenza"),
    ("Expert", "Ottima conoscenza"),
    ("Buona Conoscenza", "Buona conoscenza"),
    ("Intermediate", "Buona conoscenza"),
    ("Discreta conoscenza", "Buona conoscenza"),
    ("Conoscenza base", "Conoscenza base"),
    ("Conoscenza", "Conoscenza base"),
    ("Basic", "Conoscenza base"),
    ("4/5", "Buona conoscenza"),
    ("Certificato AWS", "Certificato AWS"),  # non riconosciuto: resta com'e'
    (None, None),
    ("  ", None),
])
def test_skill_levels(raw, expected):
    assert normalize_skill_level(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("LINGUA MADRE", "Madrelingua"),
    ("Native speaker", "Madrelingua"),
    ("C2", "Avanzato"),
    ("C1 - Advanced", "Avanzato"),
    ("Fluente", "Fluente"),
    ("fluent", "Fluente"),
    ("B2", "Intermedio"),
    ("Upper intermediate", "Intermedio"),
    ("Pre-intermediate", "Base"),
    ("A2", "Base"),
    ("Scolastico", "Base"),
    ("Intermedio", "Intermedio"),
    (None, None),
])
def test_language_levels(raw, expected):
    assert normalize_language_level(raw) == expected

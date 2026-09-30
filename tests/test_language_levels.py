"""Filtro lingua MCFlash: il livello richiesto e' un minimo sulla scala
Base < Intermedio = Buono < Avanzato < Madrelingua."""
from __future__ import annotations

import pytest

from infra.mcflash_candidates import MCFlashCandidatesClient as C

MCFLASH_VALUES = ["Inglese: Base", "Inglese: Intermedio", "Inglese: Buono", "Inglese: Avanzato", "Inglese: Madrelingua"]


def _passing(asked):
    return [v for v in MCFLASH_VALUES if C._language_matches(asked, v)]


@pytest.mark.parametrize("asked,expected", [
    ("Inglese: Base", MCFLASH_VALUES),
    ("Inglese: Intermedio", ["Inglese: Intermedio", "Inglese: Buono", "Inglese: Avanzato", "Inglese: Madrelingua"]),
    ("Inglese: Buono", ["Inglese: Intermedio", "Inglese: Buono", "Inglese: Avanzato", "Inglese: Madrelingua"]),
    ("Inglese: Avanzato", ["Inglese: Avanzato", "Inglese: Madrelingua"]),
    ("Inglese: Madrelingua", ["Inglese: Madrelingua"]),
    ("Inglese", MCFLASH_VALUES),
])
def test_level_is_a_minimum(asked, expected):
    assert _passing(asked) == expected


@pytest.mark.parametrize("asked,equivalent", [
    ("Inglese: Fluente", "Inglese: Avanzato"),
    ("Inglese: C1", "Inglese: Avanzato"),
    ("Inglese: B2", "Inglese: Intermedio"),
    ("Inglese: A2", "Inglese: Base"),
    ("English: Advanced", "Inglese: Avanzato"),
    ("Inglese (Avanzato)", "Inglese: Avanzato"),
    ("inglese: ottimo", "Inglese: Avanzato"),
])
def test_synonyms(asked, equivalent):
    assert _passing(asked) == _passing(equivalent)


def test_other_language_does_not_match():
    assert _passing("Tedesco") == []
    assert _passing("Tedesco: Base") == []


def test_multiple_languages_on_candidate():
    value = "Inglese: Avanzato, Francese: Base"
    assert C._language_matches("Francese", value)
    assert C._language_matches("Inglese: Intermedio", value)
    assert not C._language_matches("Francese: Intermedio", value)


def test_missing_values():
    assert C._language_matches("", "Inglese: Base")
    assert not C._language_matches("Inglese", "")
    assert not C._language_matches("Inglese: Base", "Inglese")  # livello del candidato ignoto

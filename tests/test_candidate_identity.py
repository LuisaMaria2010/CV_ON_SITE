"""Trittico (prima lettera del nome + prime due del cognome) e proposta del nome file CV."""
from __future__ import annotations

import pytest

from domain.candidate_identity import (
    FILENAME_PATTERNS,
    propose_cv_filename,
    split_full_name,
    trittico,
    trittico_from_full_name,
)


@pytest.mark.parametrize("first,last,expected", [
    ("Davide", "Carbone", "DCA"),
    ("mario", "rossi", "MRO"),
    ("Mario", "De Luca", "MDE"),
    ("Luca", "D'Amico", "LDA"),
    ("Élodie", "Ångström", "EAN"),
    ("Anna Maria", "Bianchi", "ABI"),
    ("Mario", "R", None),
    (None, "Rossi", None),
])
def test_trittico(first, last, expected):
    assert trittico(first, last) == expected


@pytest.mark.parametrize("full,expected", [
    ("Mario Rossi", ("Mario", "Rossi")),
    ("Mario De Luca", ("Mario", "De Luca")),
    ("Anna Maria Della Valle", ("Anna Maria", "Della Valle")),
    ("ROSSI Mario", ("Mario", "Rossi".upper())),
    ("Mario", ("Mario", None)),
    ("", (None, None)),
])
def test_split_full_name(full, expected):
    assert split_full_name(full) == expected


def test_trittico_from_full_name():
    assert trittico_from_full_name("BIANCHI Luisa Maria") == "LBI"


def test_filename_first_choice_when_free():
    assert propose_cv_filename("dca", []) == "Skill Tecnico_DCA_MC Engineering.pdf"


def test_filename_skips_taken_ignoring_case_and_extension():
    existing = ["incoming/skill tecnico_DCA_mc engineering.DOCX"]
    assert propose_cv_filename("DCA", existing, extension="docx") == "Skill_Tecnico_DCA_MC_Engineering.docx"


def test_filename_variants_then_numbered():
    taken = [p.format(t="DCA") + ".pdf" for p in FILENAME_PATTERNS]
    assert propose_cv_filename("DCA", taken) == "Skill Tecnico_DCA_MC Engineering (2).pdf"
    assert propose_cv_filename("DCA", taken + ["Skill Tecnico_DCA_MC Engineering (2).pdf"]) == \
        "Skill Tecnico_DCA_MC Engineering (3).pdf"


def test_filename_reuses_current_name_for_existing_candidate():
    # Stesso percorso -> stesso document_id: l'indice sostituisce il vecchio CV.
    name = propose_cv_filename("DCA", ["Skill_Tecnico_DCA_MC_.pdf"], current_name="cv/Skill_Tecnico_DCA_MC_.pdf")
    assert name == "Skill_Tecnico_DCA_MC_.pdf"


def test_filename_rejects_bad_trittico():
    with pytest.raises(ValueError):
        propose_cv_filename("DC", [])

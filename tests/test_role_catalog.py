"""Catalogo ruoli MC (infra/mcflash_budget_placeholders.json) e ruolo in alto nel CV MCFlash."""
from __future__ import annotations

import pytest

from core.llm_chain import _load_prompt
from core.config import settings
from core.schema import LLMExtractionMCFlashRaw
from db_data.mapper import to_domain
from domain.role_catalog import display_role, match_catalog_role, role_names
from services.cv_mcflash_renderer import build_context


def test_catalog_loaded_from_budget_json():
    names = role_names()
    assert "data scientist" in names and "android developer" in names
    assert len(names) > 50


@pytest.mark.parametrize("text,expected", [
    ("data scientist", "data scientist"),
    ("Data Scientist", "data scientist"),
    ("Full Stack Developer", "full-stack developer"),
    ("data scentist", "data scientist"),        # refuso
    ("astronauta", None),
    (None, None),
])
def test_match_catalog_role(text, expected):
    assert match_catalog_role(text) == expected


@pytest.mark.parametrize("name,expected", [
    ("data scientist", "Data Scientist"),
    (".net developer", ".NET Developer"),
    ("sap abap developer", "SAP ABAP Developer"),
    ("full-stack developer", "Full-Stack Developer"),
    ("helpdesk 1° e 2° livello", "Helpdesk 1° e 2° Livello"),
    ("devops engineer", "DevOps Engineer"),
])
def test_display_role(name, expected):
    assert display_role(name) == expected


def test_prompt_contains_catalog():
    prompt = _load_prompt(settings.prompt_mcflash_addendum_file)
    assert "<<ROLE_CATALOG>>" not in prompt
    assert "- data scientist" in prompt and "- android developer" in prompt


def test_mapper_and_renderer_use_catalog_role():
    raw = LLMExtractionMCFlashRaw(name="Luisa Serrano", role="IT Consultant", catalog_role="AI Engineer")
    cv = to_domain(raw)
    assert cv.catalog_role == "ai engineer"
    assert build_context(cv)["ruolo"] == "AI Engineer"


def test_catalog_role_outside_list_falls_back_to_cv_role():
    cv = to_domain(LLMExtractionMCFlashRaw(name="Mario Rossi", role="Astronauta", catalog_role="astronauta"))
    assert cv.catalog_role is None
    assert build_context(cv)["ruolo"] == "Astronauta"

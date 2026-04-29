"""
Tests for core/schema.py — NormalizedCVMetadata, CandidateInfo,
LanguageEntry, ImageRef, WorkExperience, CVExtraction, LLMExtractionRaw.
"""
from __future__ import annotations

from datetime import date

import pytest

from core.schema import (
    CandidateInfo,
    CVExtraction,
    ImageRef,
    LanguageEntry,
    LLMExtractionRaw,
    NormalizedCVMetadata,
    SkillEntry,
    WorkExperience,
    WorkExperienceRaw,
    ExtractHttpResponse,
)


# =========================================================
# CandidateInfo
# =========================================================

def test_candidate_info_defaults():
    c = CandidateInfo()
    assert c.full_name is None
    assert c.role is None
    assert c.seniority is None


def test_candidate_info_all_fields():
    c = CandidateInfo(
        full_name="Mario Rossi",
        role="DevOps Engineer",
        location="Milano",
        email="mario@example.com",
        phone="+39 333 0000000",
        birth_date="1990-01-15",
        age=35,
        availability="immediate",
        seniority="senior",
    )
    assert c.full_name == "Mario Rossi"
    assert c.seniority == "senior"
    assert c.age == 35


def test_candidate_info_extra_fields_ignored():
    c = CandidateInfo(full_name="X", unknown_field="ignored")
    assert not hasattr(c, "unknown_field")


# =========================================================
# LanguageEntry
# =========================================================

def test_language_entry_required_lang():
    le = LanguageEntry(lang="italiano")
    assert le.lang == "italiano"
    assert le.level is None


def test_language_entry_with_level():
    le = LanguageEntry(lang="english", level="C1")
    assert le.level == "C1"


def test_language_entry_missing_lang_raises():
    with pytest.raises(Exception):
        LanguageEntry()


# =========================================================
# ImageRef
# =========================================================

def test_image_ref_required_blob_path():
    img = ImageRef(blob_path="cv-images/mario/photo.png")
    assert img.blob_path == "cv-images/mario/photo.png"
    assert img.description is None


def test_image_ref_with_description():
    img = ImageRef(blob_path="cv-images/x.jpg", description="Profile photo")
    assert img.description == "Profile photo"


# =========================================================
# WorkExperience (domain model)
# =========================================================

def test_work_experience_defaults():
    w = WorkExperience()
    assert w.start_date is None
    assert w.end_date is None


def test_work_experience_with_dates():
    w = WorkExperience(start_date=date(2020, 1, 1), end_date=date(2023, 12, 31))
    assert w.start_date.year == 2020
    assert w.end_date.year == 2023


# =========================================================
# WorkExperienceRaw (LLM output)
# =========================================================

def test_work_experience_raw_string_fields():
    wr = WorkExperienceRaw(start_date="2020-05-01", end_date=None)
    assert wr.start_date == "2020-05-01"
    assert wr.end_date is None


# =========================================================
# LLMExtractionRaw
# =========================================================

def test_llm_extraction_raw_defaults():
    raw = LLMExtractionRaw()
    assert raw.name is None
    assert raw.skills == []
    assert raw.employment_dates == []


def test_llm_extraction_raw_skills_list():
    raw = LLMExtractionRaw(name="Laura Bianchi", skills=[{"name": "Python"}, {"name": "Azure"}, {"name": "Docker"}])
    assert any(s.name == "Python" for s in raw.skills)
    assert len(raw.skills) == 3


def test_llm_extraction_raw_extra_ignored():
    raw = LLMExtractionRaw(name="X", unknown="ignored")
    assert not hasattr(raw, "unknown")


# =========================================================
# CVExtraction
# =========================================================

def test_cv_extraction_defaults():
    cv = CVExtraction()
    assert cv.full_name is None
    assert cv.skills == []
    assert cv.seniority is None


def test_cv_extraction_normalize_skills():
    cv = CVExtraction(skills=[SkillEntry(name="Python"), SkillEntry(name=" AZURE "), SkillEntry(name="python"), SkillEntry(name="Docker")])
    cv.normalize_skills()
    assert [s.name for s in cv.skills] == ["azure", "docker", "python"]
    assert len(cv.skills) == 3  # duplicates removed


def test_cv_extraction_email_validation():
    cv = CVExtraction(email="test@example.com")
    assert str(cv.email) == "test@example.com"


def test_cv_extraction_invalid_email_raises():
    with pytest.raises(Exception):
        CVExtraction(email="not-an-email")


def test_cv_extraction_employment_dates():
    cv = CVExtraction(employment_dates=[
        WorkExperience(start_date=date(2018, 1, 1), end_date=date(2020, 12, 31)),
    ])
    assert len(cv.employment_dates) == 1
    assert cv.employment_dates[0].start_date.year == 2018


# =========================================================
# NormalizedCVMetadata
# =========================================================

def _make_normalized() -> NormalizedCVMetadata:
    return NormalizedCVMetadata(
        document_id="mario-rossi",
        source_paths=["incoming/mario_rossi_cv.pdf"],
        version=1,
        hash="abc123",
        processed_at="2026-04-23T12:00:00+00:00",
        language="it",
        candidate=CandidateInfo(full_name="Mario Rossi", role="Software Engineer", seniority="senior"),
        skills=[SkillEntry(name="python"), SkillEntry(name="azure"), SkillEntry(name="docker")],
        certifications=["AZ-900"],
        education_titles=["Laurea in Informatica"],
        languages_spoken=[LanguageEntry(lang="italiano", level="madrelingua"), LanguageEntry(lang="english", level="B2")],
        experience_years=7.5,
        employment_dates=[WorkExperience(start_date=date(2017, 3, 1))],
        images=[ImageRef(blob_path="cv-images/mario/photo.png")],
        element_count=42,
        image_count=1,
        metadata={"source": "upload"},
    )


def test_normalized_cv_metadata_round_trip():
    meta = _make_normalized()
    d = meta.model_dump()
    meta2 = NormalizedCVMetadata(**d)
    assert meta2.document_id == "mario-rossi"
    assert [s.name for s in meta2.skills] == ["python", "azure", "docker"]
    assert meta2.candidate.full_name == "Mario Rossi"


def test_normalized_cv_metadata_required_fields():
    with pytest.raises(Exception):
        # document_id is required
        NormalizedCVMetadata(hash="x", processed_at="2026-01-01T00:00:00")


def test_normalized_cv_metadata_languages_spoken():
    meta = _make_normalized()
    assert len(meta.languages_spoken) == 2
    langs = [le.lang for le in meta.languages_spoken]
    assert "italiano" in langs


def test_normalized_cv_metadata_images():
    meta = _make_normalized()
    assert meta.images[0].blob_path == "cv-images/mario/photo.png"


def test_normalized_cv_metadata_model_dump_is_serializable():
    import json
    meta = _make_normalized()
    d = meta.model_dump()
    # date objects inside employment_dates should be json-serializable via model_dump()
    # Pydantic v2 model_dump returns date objects by default; use mode='json' for strings
    d_json = meta.model_dump(mode="json")
    serialized = json.dumps(d_json)
    assert "mario-rossi" in serialized


# =========================================================
# ExtractHttpResponse
# =========================================================

def test_extract_http_response_fields():
    cv = CVExtraction(full_name="Test User", skills=[SkillEntry(name="python")])
    resp = ExtractHttpResponse(
        data=cv,
        prompt_id="cv-extraction-v1",
        prompt_version="1.0",
        prompt_hash="deadbeef",
        model="gpt-4o-mini",
    )
    assert resp.data.full_name == "Test User"
    assert resp.model == "gpt-4o-mini"

"""
Tests for services/document_processor.py — focus su:
  - apply_cv_extraction: rebuild del front matter con output LLM
  - _build_yaml_front_matter: struttura e chiavi attese
  - round-trip: front matter → strip → riapplicazione
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from core.schema import (
    CandidateInfo,
    CVExtraction,
    ImageRef,
    LanguageEntry,
    NormalizedCVMetadata,
    SkillEntry,
    WorkExperience,
)
from services.document_processor import DocumentProcessor


# =========================================================
# Helpers
# =========================================================

def _make_base_metadata(document_id: str = "test-doc", version: int = 1) -> dict:
    return {
        "document_id": document_id,
        "source_paths": ["incoming/test_cv.pdf"],
        "version": version,
        "hash": "sha256abc",
        "processed_at": "2026-04-23T12:00:00+00:00",
        "language": "it",
        "candidate": {"full_name": "Candidato Base", "role": None},
        "skills": [],
        "certifications": [],
        "education_titles": [],
        "languages_spoken": [],
        "experience_years": None,
        "employment_dates": [],
        "images": [],
        "element_count": 10,
        "image_count": 0,
        "metadata": {},
    }


def _make_cv_extraction(**kwargs) -> CVExtraction:
    defaults = dict(
        full_name="Mario Rossi",
        role="Software Engineer",
        location="Milano, Italy",
        email="mario@example.com",
        phone="+39 333 1234567",
        language="italiano",
        birth_date=date(1990, 5, 15),
        age=35,
        skills=[SkillEntry(name="python"), SkillEntry(name="azure"), SkillEntry(name="docker")],
        certifications=["AZ-900"],
        education_titles=["Laurea Magistrale Informatica"],
        employment_dates=[
            WorkExperience(start_date=date(2018, 1, 1), end_date=date(2022, 12, 31)),
        ],
        experience_years=7.5,
        seniority="senior",
    )
    defaults.update(kwargs)
    return CVExtraction(**defaults)


SAMPLE_MARKDOWN = """\
---
document_id: test-doc
language: it
candidate:
  full_name: Candidato Base
skills: []
---

# Mario Rossi

Software Engineer con esperienza in Python e Azure.
"""


# =========================================================
# _build_yaml_front_matter
# =========================================================

class TestBuildYamlFrontMatter:

    def setup_method(self):
        self.proc = DocumentProcessor()

    def test_starts_and_ends_with_separator(self):
        meta = NormalizedCVMetadata(
            document_id="d1", hash="h", processed_at="2026-01-01T00:00:00",
            candidate=CandidateInfo(full_name="X"),
        )
        lines = self.proc._build_yaml_front_matter(meta)
        assert lines[0] == "---"
        assert "---" in lines[-2:]  # closing separator

    def test_document_id_present(self):
        meta = NormalizedCVMetadata(
            document_id="my-doc", hash="h", processed_at="2026-01-01T00:00:00",
        )
        lines = self.proc._build_yaml_front_matter(meta)
        joined = "\n".join(lines)
        assert "document_id: my-doc" in joined

    def test_skills_list_emitted(self):
        meta = NormalizedCVMetadata(
            document_id="d1", hash="h", processed_at="2026-01-01T00:00:00",
            skills=[SkillEntry(name="python"), SkillEntry(name="azure")],
        )
        lines = self.proc._build_yaml_front_matter(meta)
        joined = "\n".join(lines)
        assert "name: python" in joined
        assert "name: azure" in joined

    def test_candidate_nested_fields(self):
        meta = NormalizedCVMetadata(
            document_id="d1", hash="h", processed_at="2026-01-01T00:00:00",
            candidate=CandidateInfo(full_name="Mario Rossi", role="DevOps", seniority="senior"),
        )
        lines = self.proc._build_yaml_front_matter(meta)
        joined = "\n".join(lines)
        # YAML scalar with spaces is quoted: full_name: "Mario Rossi"
        assert "Mario Rossi" in joined
        assert "full_name" in joined
        assert "seniority: senior" in joined

    def test_languages_spoken_nested(self):
        meta = NormalizedCVMetadata(
            document_id="d1", hash="h", processed_at="2026-01-01T00:00:00",
            languages_spoken=[LanguageEntry(lang="italiano", level="madrelingua")],
        )
        lines = self.proc._build_yaml_front_matter(meta)
        joined = "\n".join(lines)
        assert "lang: italiano" in joined
        assert "madrelingua" in joined

    def test_ordered_keys(self):
        meta = NormalizedCVMetadata(
            document_id="d1", hash="h", processed_at="2026-01-01T00:00:00",
            language="it",
        )
        lines = self.proc._build_yaml_front_matter(meta)
        joined = "\n".join(lines)
        doc_id_pos = joined.index("document_id")
        lang_pos = joined.index("language")
        assert doc_id_pos < lang_pos, "document_id must appear before language"

    def test_accepts_dict_input(self):
        data = {"document_id": "x", "hash": "h", "processed_at": "2026-01-01", "candidate": {}}
        lines = self.proc._build_yaml_front_matter(data)
        joined = "\n".join(lines)
        assert "document_id: x" in joined


# =========================================================
# apply_cv_extraction
# =========================================================

class TestApplyCVExtraction:

    def setup_method(self):
        self.proc = DocumentProcessor()
        self.base_meta = _make_base_metadata()
        self.cv_ext = _make_cv_extraction()

    def test_returns_markdown_and_dict(self):
        md, meta = self.proc.apply_cv_extraction(SAMPLE_MARKDOWN, self.base_meta, self.cv_ext)
        assert isinstance(md, str)
        assert isinstance(meta, dict)

    def test_front_matter_replaced_not_duplicated(self):
        md, _ = self.proc.apply_cv_extraction(SAMPLE_MARKDOWN, self.base_meta, self.cv_ext)
        # Only one opening --- should appear at the very start
        assert md.startswith("---\n")
        count = md.count("\n---\n")
        assert count == 1, f"Expected exactly one closing ---, got: {count}"

    def test_body_preserved(self):
        md, _ = self.proc.apply_cv_extraction(SAMPLE_MARKDOWN, self.base_meta, self.cv_ext)
        assert "# Mario Rossi" in md
        assert "Software Engineer con esperienza" in md

    def test_full_name_from_extraction(self):
        md, meta = self.proc.apply_cv_extraction(SAMPLE_MARKDOWN, self.base_meta, self.cv_ext)
        assert "Mario Rossi" in md
        assert meta["candidate"]["full_name"] == "Mario Rossi"

    def test_role_from_extraction(self):
        md, meta = self.proc.apply_cv_extraction(SAMPLE_MARKDOWN, self.base_meta, self.cv_ext)
        assert meta["candidate"]["role"] == "Software Engineer"

    def test_skills_from_extraction(self):
        md, meta = self.proc.apply_cv_extraction(SAMPLE_MARKDOWN, self.base_meta, self.cv_ext)
        skill_names = [s["name"] if isinstance(s, dict) else s.name for s in meta["skills"]]
        assert "python" in skill_names
        assert "azure" in skill_names
        assert "name: python" in md or "name: azure" in md

    def test_certifications_from_extraction(self):
        _, meta = self.proc.apply_cv_extraction(SAMPLE_MARKDOWN, self.base_meta, self.cv_ext)
        assert "AZ-900" in meta["certifications"]

    def test_experience_years_from_extraction(self):
        _, meta = self.proc.apply_cv_extraction(SAMPLE_MARKDOWN, self.base_meta, self.cv_ext)
        assert meta["experience_years"] == 7.5

    def test_seniority_from_extraction(self):
        _, meta = self.proc.apply_cv_extraction(SAMPLE_MARKDOWN, self.base_meta, self.cv_ext)
        assert meta["candidate"]["seniority"] == "senior"

    def test_language_from_extraction(self):
        _, meta = self.proc.apply_cv_extraction(SAMPLE_MARKDOWN, self.base_meta, self.cv_ext)
        assert meta["language"] == "italiano"
        assert len(meta["languages_spoken"]) >= 1
        assert meta["languages_spoken"][0]["lang"] == "italiano"

    def test_document_id_preserved_from_base(self):
        _, meta = self.proc.apply_cv_extraction(SAMPLE_MARKDOWN, self.base_meta, self.cv_ext)
        assert meta["document_id"] == "test-doc"

    def test_version_preserved_from_base(self):
        base = _make_base_metadata(version=3)
        _, meta = self.proc.apply_cv_extraction(SAMPLE_MARKDOWN, base, self.cv_ext)
        assert meta["version"] == 3

    def test_extraction_with_no_name_falls_back_to_base(self):
        cv = _make_cv_extraction(full_name=None)
        _, meta = self.proc.apply_cv_extraction(SAMPLE_MARKDOWN, self.base_meta, cv)
        # should fall back to base_metadata candidate full_name
        assert meta["candidate"]["full_name"] == "Candidato Base"

    def test_images_from_base_metadata_preserved(self):
        base = _make_base_metadata()
        base["images"] = [{"blob_path": "cv-images/photo.png", "description": "Foto"}]
        _, meta = self.proc.apply_cv_extraction(SAMPLE_MARKDOWN, base, self.cv_ext)
        assert len(meta["images"]) == 1
        assert meta["images"][0]["blob_path"] == "cv-images/photo.png"

    def test_markdown_without_front_matter(self):
        raw_md = "# Solo testo\n\nSenza front matter."
        md, _ = self.proc.apply_cv_extraction(raw_md, self.base_meta, self.cv_ext)
        assert md.startswith("---\n")
        assert "Solo testo" in md

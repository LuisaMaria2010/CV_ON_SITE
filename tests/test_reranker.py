"""
Tests per services/search_handler.py (Phase F):
  - build_odata_filter: clause generation, SQL injection safety
  - build_odata_filter_relaxed: skills rimossi, resto manttenuto
  - rerank: scoring formula, ordinamento, top-N
  - normalise_search_request: normalizzazione, clamp di top
  - resolve_index: routing subco
  - _months_ago helper
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.config import settings
from services.search_handler import (
    _months_ago,
    build_odata_filter,
    build_odata_filter_relaxed,
    normalise_search_request,
    rerank,
    resolve_index,
)


# =========================================================
# build_odata_filter
# =========================================================

class TestBuildOdataFilter:

    def test_none_when_no_params(self):
        assert build_odata_filter() is None

    def test_single_skill(self):
        f = build_odata_filter(skills=["python"])
        assert f == "skills/any(s: s eq 'python')"

    def test_multiple_skills(self):
        f = build_odata_filter(skills=["python", "azure"])
        assert "skills/any(s: s eq 'python')" in f
        assert "skills/any(s: s eq 'azure')" in f
        assert " and " in f

    def test_seniority_clause(self):
        f = build_odata_filter(seniority="senior")
        assert "seniority eq 'senior'" in f

    def test_min_experience(self):
        f = build_odata_filter(min_experience_years=5)
        assert "experience_years ge 5" in f

    def test_max_experience(self):
        f = build_odata_filter(max_experience_years=10)
        assert "experience_years le 10" in f

    def test_experience_range(self):
        f = build_odata_filter(min_experience_years=3, max_experience_years=8)
        assert "ge 3" in f
        assert "le 8" in f

    def test_language_clause(self):
        f = build_odata_filter(language="it")
        assert "language eq 'it'" in f

    def test_availability_required(self):
        f = build_odata_filter(availability_required=True)
        assert "availability ne null" in f

    def test_all_params_combined(self):
        f = build_odata_filter(
            skills=["python"],
            seniority="senior",
            min_experience_years=5,
            max_experience_years=10,
            language="it",
            availability_required=True,
        )
        assert f is not None
        assert "python" in f
        assert "senior" in f
        assert "ge 5" in f
        assert "le 10" in f
        assert "language eq 'it'" in f
        assert "availability ne null" in f

    def test_sql_injection_single_quotes_escaped(self):
        f = build_odata_filter(skills=["O'Brien"])
        # Single quote in skill must be doubled
        assert "O''Brien" in f
        assert "O'Brien'" not in f.replace("O''Brien", "")  # raw unescaped must not appear

    def test_seniority_injection_escaped(self):
        f = build_odata_filter(seniority="senior' or 1 eq 1")
        assert "senior'' or 1 eq 1" in f

    def test_empty_skills_list_no_clause(self):
        f = build_odata_filter(skills=[])
        assert f is None

    def test_zero_min_experience(self):
        f = build_odata_filter(min_experience_years=0)
        assert "experience_years ge 0" in f


# =========================================================
# build_odata_filter_relaxed
# =========================================================

class TestBuildOdataFilterRelaxed:

    def test_skills_not_present(self):
        f = build_odata_filter_relaxed(seniority="senior", language="it")
        assert f is not None
        assert "skills" not in f

    def test_seniority_preserved(self):
        f = build_odata_filter_relaxed(seniority="junior")
        assert "seniority eq 'junior'" in f

    def test_min_experience_preserved(self):
        f = build_odata_filter_relaxed(min_experience_years=3)
        assert "ge 3" in f

    def test_max_experience_preserved(self):
        f = build_odata_filter_relaxed(max_experience_years=8)
        assert "le 8" in f

    def test_language_preserved(self):
        f = build_odata_filter_relaxed(language="en")
        assert "language eq 'en'" in f

    def test_all_none_returns_none(self):
        assert build_odata_filter_relaxed() is None


# =========================================================
# _months_ago
# =========================================================

class TestMonthsAgo:

    def test_recent_date(self):
        now = datetime(2026, 4, 23, tzinfo=timezone.utc)
        # 3 months ago
        months = _months_ago("2026-01-23T00:00:00+00:00", now)
        assert months is not None
        assert abs(months - 3.0) < 0.5

    def test_old_date(self):
        now = datetime(2026, 4, 23, tzinfo=timezone.utc)
        months = _months_ago("2020-01-01T00:00:00+00:00", now)
        assert months is not None
        assert months > 24

    def test_none_input(self):
        now = datetime(2026, 4, 23, tzinfo=timezone.utc)
        assert _months_ago(None, now) is None

    def test_malformed_date(self):
        now = datetime(2026, 4, 23, tzinfo=timezone.utc)
        assert _months_ago("not-a-date", now) is None

    def test_no_timezone_handled(self):
        now = datetime(2026, 4, 23, tzinfo=timezone.utc)
        months = _months_ago("2025-10-23T00:00:00", now)
        assert months is not None
        assert months > 0


# =========================================================
# rerank
# =========================================================

def _make_hit(
    document_id: str = "doc1",
    semantic_score: float = 0.0,
    lex_score: float = 0.5,
    vec_score: float = 0.5,
    skills: list[str] | None = None,
    role: str = "developer",
    location: str = "milano",
    # Default to >6 months ago so recency_boost is NOT applied unless explicitly set
    processed_at: str = "2020-01-01T00:00:00+00:00",
) -> dict:
    return {
        "document_id": document_id,
        "semantic_score": semantic_score,
        "lex_score": lex_score,
        "vec_score": vec_score,
        "skills": skills or [],
        "role": role,
        "location": location,
        "processed_at": processed_at,
        "full_name": "Test User",
    }


class TestRerank:

    def test_base_score_formula(self):
        hit = _make_hit(semantic_score=0.0, lex_score=1.0, vec_score=0.0)
        ranked = rerank([hit], top=1)
        # retrieval-only: 0.0*0.70 + 0.0*0.20 + 1.0*0.10 = 0.10
        assert abs(ranked[0]["score"] - 0.10) < 0.001

    def test_vec_weight(self):
        hit = _make_hit(semantic_score=0.0, lex_score=0.0, vec_score=1.0)
        ranked = rerank([hit], top=1)
        assert abs(ranked[0]["score"] - 0.20) < 0.001

    def test_mixed_lex_vec(self):
        hit = _make_hit(semantic_score=0.0, lex_score=1.0, vec_score=1.0)
        ranked = rerank([hit], top=1)
        assert abs(ranked[0]["score"] - 0.30) < 0.001

    def test_semantic_score_dominates(self):
        hit = _make_hit(semantic_score=4.0, lex_score=0.0, vec_score=0.0)
        ranked = rerank([hit], top=1)
        assert abs(ranked[0]["score"] - 0.70) < 0.001

    def test_query_business_params_do_not_change_score(self):
        hit = _make_hit(lex_score=0.0, vec_score=0.0, skills=["python", "azure", "docker"])
        ranked = rerank([hit], query_skills=["python", "azure"], top=1)
        assert ranked[0]["score"] == 0.0

    def test_recency_boost_recent(self):
        recent_date = "2026-03-01T00:00:00+00:00"  # ~2 months ago from April 2026
        hit = _make_hit(lex_score=0.0, vec_score=0.0, processed_at=recent_date)
        ranked = rerank([hit], top=1)
        assert abs(ranked[0]["score"] - settings.search_reranker_recency_boost) < 0.001

    def test_no_recency_boost_old(self):
        old_date = "2020-01-01T00:00:00+00:00"  # > 6 months ago
        hit = _make_hit(lex_score=0.0, vec_score=0.0, processed_at=old_date)
        ranked = rerank([hit], top=1)
        assert ranked[0]["score"] == 0.0

    def test_ordering_desc(self):
        h1 = _make_hit(document_id="low", lex_score=0.1, vec_score=0.1)
        h2 = _make_hit(document_id="high", lex_score=0.9, vec_score=0.9)
        ranked = rerank([h1, h2], top=2)
        assert ranked[0]["document_id"] == "high"
        assert ranked[1]["document_id"] == "low"

    def test_top_n_limit(self):
        hits = [_make_hit(document_id=f"doc{i}", lex_score=float(i) * 0.1) for i in range(20)]
        ranked = rerank(hits, top=5)
        assert len(ranked) == 5

    def test_top_n_more_than_available(self):
        hits = [_make_hit(document_id="only")]
        ranked = rerank(hits, top=10)
        assert len(ranked) == 1

    def test_score_added_to_output(self):
        hit = _make_hit()
        ranked = rerank([hit])
        assert "score" in ranked[0]

    def test_original_fields_preserved(self):
        hit = _make_hit(document_id="preserve-me")
        ranked = rerank([hit])
        assert ranked[0]["document_id"] == "preserve-me"
        assert ranked[0]["full_name"] == "Test User"

    def test_all_boosts_combined(self):
        recent = "2026-04-01T00:00:00+00:00"
        hit = _make_hit(
            semantic_score=4.0,
            lex_score=1.0, vec_score=1.0,
            skills=["python", "azure"],
            role="software developer",
            location="Milano",
            processed_at=recent,
        )
        ranked = rerank([hit], query_skills=["python", "azure"], query_role="developer", query_location="milano")
        expected = (
            0.70
            + 0.20
            + 0.10
            + settings.search_reranker_recency_boost
        )
        assert abs(ranked[0]["score"] - expected) < 0.001


# =========================================================
# normalise_search_request
# =========================================================

class TestNormaliseSearchRequest:

    def test_skills_lowercased_deduped_sorted(self):
        p = normalise_search_request({"skills": ["Python", "AZURE", "python"]})
        assert p["skills"] == sorted({"python", "azure"})

    def test_empty_skills(self):
        p = normalise_search_request({})
        assert p["skills"] == []

    def test_top_default(self):
        p = normalise_search_request({"query": "developer"})
        assert p["top"] == 10

    def test_top_clamped_min(self):
        p = normalise_search_request({"top": -5})
        assert p["top"] == 1

    def test_top_clamped_max(self):
        p = normalise_search_request({"top": 999})
        assert p["top"] == 100

    def test_top_valid(self):
        p = normalise_search_request({"top": 25})
        assert p["top"] == 25

    def test_hybrid_default_true(self):
        p = normalise_search_request({})
        assert p["hybrid"] is True

    def test_hybrid_false(self):
        p = normalise_search_request({"hybrid": False})
        assert p["hybrid"] is False

    def test_subco_lowercased(self):
        p = normalise_search_request({"subco": "Risorse"})
        assert p["subco"] == "risorse"

    def test_subco_none_when_missing(self):
        p = normalise_search_request({})
        assert p["subco"] is None

    def test_min_max_experience(self):
        p = normalise_search_request({"min_experience_years": "3", "max_experience_years": 8})
        assert p["min_experience_years"] == 3.0
        assert p["max_experience_years"] == 8.0

    def test_invalid_min_experience_becomes_none(self):
        p = normalise_search_request({"min_experience_years": "abc"})
        assert p["min_experience_years"] is None

    def test_query_stripped(self):
        p = normalise_search_request({"query": "  python developer  "})
        assert p["query"] == "python developer"

    def test_role_none_when_empty(self):
        p = normalise_search_request({"role": "  "})
        assert p["role"] is None

    def test_availability_required_default_false(self):
        p = normalise_search_request({})
        assert p["availability_required"] is False


# =========================================================
# resolve_index
# =========================================================

class TestResolveIndex:

    def test_none_returns_default(self):
        assert resolve_index(None) == settings.document_search_index_name

    def test_risorse_subco(self):
        assert resolve_index("risorse") == settings.search_subco_risorse_index

    def test_candidati_subco(self):
        assert resolve_index("candidati") == settings.search_subco_candidati_index

    def test_unknown_subco_returns_default(self):
        assert resolve_index("unknown-value") == settings.document_search_index_name

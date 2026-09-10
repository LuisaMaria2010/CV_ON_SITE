"""
Tests per services/search_handler.py (Phase F):
  - build_odata_filter: clause generation, SQL injection safety
  - build_odata_filter_relaxed: skills rimossi, resto manttenuto
  - rerank: scoring formula, ordinamento, top-N
  - normalise_search_request: normalizzazione, clamp di top
  - _months_ago helper
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.config import settings
from services.search_handler import (
    _availability_match_features,
    _months_ago,
    build_match_features,
    build_odata_filter,
    build_odata_filter_relaxed,
    normalise_search_request,
    rerank,
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

    def test_all_params_combined(self):
        f = build_odata_filter(
            skills=["python"],
            min_experience_years=5,
            max_experience_years=10,
        )
        assert f is not None
        assert "python" in f
        assert "ge 5" in f
        assert "le 10" in f

    def test_sql_injection_single_quotes_escaped(self):
        f = build_odata_filter(skills=["O'Brien"])
        # Single quote in skill must be doubled
        assert "O''Brien" in f
        assert "O'Brien'" not in f.replace("O''Brien", "")  # raw unescaped must not appear

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
        f = build_odata_filter_relaxed(min_experience_years=3, max_experience_years=8)
        assert f is not None
        assert "skills" not in f

    def test_min_experience_preserved(self):
        f = build_odata_filter_relaxed(min_experience_years=3)
        assert "ge 3" in f

    def test_max_experience_preserved(self):
        f = build_odata_filter_relaxed(max_experience_years=8)
        assert "le 8" in f

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
        # No semantic/vec signal and no structured request -> ranking falls back
        # to batch-relative lex. With a single hit that is its own max -> 1.0.
        hit = _make_hit(semantic_score=0.0, lex_score=1.0, vec_score=0.0)
        ranked = rerank([hit], top=1)
        assert abs(ranked[0]["score"] - 1.0) < 0.001

    def test_lex_fallback_is_batch_relative(self):
        # No semantic/vec/structured signal: order by lex, normalised to batch max.
        h_low = _make_hit(document_id="low", semantic_score=0.0, lex_score=2.0, vec_score=0.0)
        h_high = _make_hit(document_id="high", semantic_score=0.0, lex_score=10.0, vec_score=0.0)
        ranked = rerank([h_low, h_high], top=2)
        assert ranked[0]["document_id"] == "high"
        assert abs(ranked[0]["score"] - 1.0) < 0.001
        assert abs(ranked[1]["score"] - 0.2) < 0.001

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

    def test_structured_score_ranks_when_no_retrieval_signal(self):
        # No semantic/vec signal: the structured skill/role score becomes the
        # ranking. Full skill coverage -> score 1.0 (single dimension requested).
        hit = _make_hit(lex_score=0.0, vec_score=0.0, skills=["python", "azure", "docker"])
        ranked = rerank([hit], query_skills=["python", "azure"], top=1)
        assert abs(ranked[0]["score"] - 1.0) < 0.001

    def test_structured_score_orders_by_skill_overlap(self):
        # Fully structured query (no free text -> no semantic pass): the
        # candidate with more of the requested skills must rank first.
        full = _make_hit(document_id="full", lex_score=0.0, vec_score=0.0,
                         skills=["python", "azure", "kafka"])
        partial = _make_hit(document_id="partial", lex_score=0.0, vec_score=0.0,
                            skills=["python", "php"])
        none = _make_hit(document_id="none", lex_score=0.0, vec_score=0.0,
                         skills=["cobol"])
        ranked = rerank([none, partial, full], query_skills=["python", "azure", "kafka"], top=3)
        assert [h["document_id"] for h in ranked] == ["full", "partial", "none"]

    def test_structured_is_only_tiebreaker_when_semantic_present(self):
        # Same strong semantic score, different skill coverage: semantic still
        # dominates but the better skill match wins the tie.
        good = _make_hit(document_id="good", semantic_score=4.0, lex_score=0.0, vec_score=0.0,
                         skills=["python", "azure"])
        weak = _make_hit(document_id="weak", semantic_score=4.0, lex_score=0.0, vec_score=0.0,
                         skills=["cobol"])
        ranked = rerank([weak, good], query_skills=["python", "azure"], top=2)
        assert ranked[0]["document_id"] == "good"
        # semantic block (0.70) still contributes the bulk of the score
        assert ranked[0]["score"] > 0.70

    def test_recency_boost_recent(self):
        # ~2 months ago relative to now, so the < 6 months boost applies.
        recent_date = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
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
        recent = "2026-08-01T00:00:00+00:00"
        hit = _make_hit(
            semantic_score=4.0,
            lex_score=1.0, vec_score=1.0,
            skills=["python", "azure"],
            role="software developer",
            location="Milano",
            processed_at=recent,
        )
        ranked = rerank(
            [hit],
            query_skills=["python", "azure"],
            query_role="developer",
            query_seniority=None,
            query_location="milano",
        )
        w_r = settings.search_rerank_retrieval_weight
        w_s = settings.search_rerank_structured_weight
        retrieval = 1.0 * 0.70 + 1.0 * 0.20 + 1.0 * 0.10  # sem+vec+lex all maxed
        # structured: skills fully matched (1.0), role substring match (0.8)
        structured = (
            settings.search_rerank_w_skill * 1.0
            + settings.search_rerank_w_role * 0.8
        ) / (settings.search_rerank_w_skill + settings.search_rerank_w_role)
        expected = retrieval * w_r + structured * w_s + settings.search_reranker_recency_boost
        assert abs(ranked[0]["score"] - expected) < 0.001


# =========================================================
# normalise_search_request
# =========================================================

class TestNormaliseSearchRequest:

    def test_skills_lowercased_deduped_sorted(self):
        p = normalise_search_request({"skills": ["Python", "AZURE", "python"]})
        assert p["skills"] == ["python", "azure"]

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

    def test_min_max_experience(self):
        p = normalise_search_request({"min_experience_years": "3", "max_experience_years": 8})
        assert p["min_experience_years"] == 3.0
        assert p["max_experience_years"] == 8.0

    def test_invalid_min_experience_becomes_none(self):
        p = normalise_search_request({"min_experience_years": "abc"})
        assert p["min_experience_years"] is None

    def test_years_of_experience_alias_number_sets_min_and_infers_seniority(self):
        p = normalise_search_request({"years_of_experience": 6})
        assert p["min_experience_years"] == 6.0
        assert p["max_experience_years"] is None
        assert p["seniority"] == "senior"
        assert p["seniority_inferred"] is True

    def test_years_of_experience_alias_range_string_sets_bounds(self):
        p = normalise_search_request({"years_of_experience": "3-5"})
        assert p["min_experience_years"] == 3.0
        assert p["max_experience_years"] == 5.0
        assert p["seniority"] == "mid"

    def test_explicit_min_max_override_years_of_experience_alias(self):
        p = normalise_search_request({
            "years_of_experience": "10-12",
            "min_experience_years": 2,
            "max_experience_years": 4,
        })
        assert p["min_experience_years"] == 2.0
        assert p["max_experience_years"] == 4.0
        assert p["seniority"] == "mid"

    def test_query_stripped(self):
        p = normalise_search_request({"query": "  python developer  "})
        assert p["query"] == "python developer"

    def test_role_none_when_empty(self):
        p = normalise_search_request({"role": "  "})
        assert p["role"] is None

    def test_availability_required_default_false(self):
        p = normalise_search_request({})
        assert p["availability_required"] is False

    def test_availability_days_numeric_passthrough(self):
        p = normalise_search_request({"availability_days": 15})
        assert p["availability_days"] == 15

    def test_availability_days_immediata_parsed_as_zero(self):
        # MCFlash's own Disponibilita' field uses this exact free-text format.
        p = normalise_search_request({"availability_days": "immediata"})
        assert p["availability_days"] == 0

    def test_availability_days_gg_string_parsed(self):
        p = normalise_search_request({"availability_days": "20 gg"})
        assert p["availability_days"] == 20

    def test_availability_days_weeks_string_parsed(self):
        p = normalise_search_request({"availability_days": "2 settimane"})
        assert p["availability_days"] == 14

    def test_availability_days_missing_is_none(self):
        p = normalise_search_request({})
        assert p["availability_days"] is None

    def test_availability_days_unparseable_string_is_none(self):
        p = normalise_search_request({"availability_days": "boh"})
        assert p["availability_days"] is None


# =========================================================
# _availability_match_features / build_match_features
# (candidate + requested availability parsing, MCFlash-consistent)
# =========================================================

class TestAvailabilityMatchFeatures:

    def test_not_required_is_not_applicable(self):
        features = _availability_match_features(False, 10)
        assert features == {"applicable": False, "score": None, "match": "not_requested"}

    def test_candidate_none_scores_zero(self):
        features = _availability_match_features(True, None)
        assert features["score"] == 0.0
        assert features["match"] == "none"

    def test_fixed_thresholds_used_when_no_requested_cap(self):
        assert _availability_match_features(True, 25)["match"] == "exact"
        assert _availability_match_features(True, 45)["match"] == "partial"
        assert _availability_match_features(True, 90)["match"] == "weak"

    def test_requested_cap_overrides_fixed_thresholds(self):
        # Candidate available in 15 days would be "exact" under the fixed
        # 30-day default, but must NOT be "exact" against a tighter request.
        exact = _availability_match_features(True, 5, requested_availability_days=10)
        partial = _availability_match_features(True, 15, requested_availability_days=10)
        weak = _availability_match_features(True, 25, requested_availability_days=10)

        assert exact["match"] == "exact"
        assert partial["match"] == "partial"
        assert weak["match"] == "weak"

    def test_build_match_features_parses_candidate_immediata(self):
        candidate = {"availability_days": "Immediata"}
        features = build_match_features(
            candidate,
            query_skills=[],
            query_role=None,
            query_location=None,
            query_language=None,
            query_seniority=None,
            query_availability_required=True,
            work_mode="unknown",
            relaxed_criteria=[],
            is_relaxed_result=False,
        )
        assert features["availability"]["match"] == "exact"

    def test_build_match_features_parses_candidate_date_string(self):
        # Old naive regex extracted "19" from "dal 19/01" as a day-count; the
        # MCFlash-consistent parser instead computes the actual day delta.
        candidate = {"availability": "dal 19/01"}
        features = build_match_features(
            candidate,
            query_skills=[],
            query_role=None,
            query_location=None,
            query_language=None,
            query_seniority=None,
            query_availability_required=True,
            work_mode="unknown",
            relaxed_criteria=[],
            is_relaxed_result=False,
        )
        # Not asserting an exact day count (depends on "today"), just that it
        # was parsed into a real day-count rather than the literal "19".
        assert features["availability"]["applicable"] is True
        assert features["availability"]["match"] in {"exact", "partial", "weak"}

    def test_build_match_features_uses_requested_availability_days(self):
        candidate = {"availability_days": 15}
        features = build_match_features(
            candidate,
            query_skills=[],
            query_role=None,
            query_location=None,
            query_language=None,
            query_seniority=None,
            query_availability_required=True,
            work_mode="unknown",
            relaxed_criteria=[],
            is_relaxed_result=False,
            query_availability_days=10,
        )
        # 15 <= 10*2 -> partial, NOT exact (would be exact under the fixed
        # 30-day default), proving the requested cap is actually threaded through.
        assert features["availability"]["match"] == "partial"



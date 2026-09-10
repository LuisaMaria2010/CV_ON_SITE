"""
Logica di ricerca candidati: OData builder, reranker, fallback.

Tutte le funzioni sono pure e senza side-effect: facili da testare isolatamente.
L'handler HTTP in function_app.py le orchestra.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from core.config import settings
from infra.mcflash_candidates import MCFlashCandidatesClient


ROLE_NOISE = {
    "junior",
    "mid",
    "middle",
    "senior",
    "staff",
}

# Rank words that qualify a discipline ("QA lead", "QA manager") rather than
# name a role. They stay in the token set (so they still nudge the score once
# the discipline matches), but a match whose ONLY overlap is a level token is
# downgraded to score 0 — otherwise "lead"/"manager" <-> "responsabile" scores
# every Project Manager on a "QA lead" request. The downgrade only applies when
# the requested discipline actually exists in the catalog vocabulary (so
# "Delivery Manager" still scores against Project Manager). Mirrors
# MCFlashCandidatesClient._ROLE_LEVEL_TOKENS — keep the two in sync.
ROLE_LEVEL_TOKENS = {
    "lead",
    "principal",
    "head",
    "chief",
    "manager",
    "responsabile",
    "director",
    "direttore",
    "capo",
}

# Job titles in this catalog are routinely compound ("devops/cloud engineer",
# "automation tester/python developer", "help desk & sistemista") — a plain
# str.split() glues the two halves into one token ("tester/python") and they
# never overlap with anything again. Split on whitespace AND / , & so each
# half becomes its own token. Not on "-": compounds like "full-stack" are
# meant to stay one word.
_ROLE_TOKEN_SPLIT = re.compile(r"[\s/,&]+")


def _role_tokens(role: str) -> set[str]:
    return {t for t in _ROLE_TOKEN_SPLIT.split(role) if t}


def _phrase_in(needle: str, haystack: str) -> bool:
    """Whole-phrase containment, bounded by whitespace/\"/,&\"/start/end — not a
    raw substring test. A naive `needle in haystack` let short acronyms match
    embedded inside unrelated words: "po" (Product Owner) inside "power
    platform developer", "ux" inside "linux", "ui" inside "maui developer"."""
    if not needle:
        return False
    pattern = r"(?:^|[\s/,&])" + re.escape(needle) + r"(?:$|[\s/,&])"
    return re.search(pattern, haystack) is not None


# Role acronyms -> the tokens they expand to. Applied to both the requested and
# the candidate role before token overlap, so "SRE" matches "Site Reliability
# Engineer", "BA" matches "Business Analyst", etc. Kept in the Search Layer only
# (not in the MCFlash hard-select).
ROLE_ACRONYMS = {
    # Bare "QA" is placed in the test-engineer domain: it must overlap with
    # candidates titled test engineer / software tester / automation tester,
    # the only QA-adjacent roles that actually exist in the MCFlash catalog.
    # Deliberately NOT adding a bare "engineer" token here: on the real
    # catalog that alone drags in every unrelated *Engineer title (data
    # engineer, ai engineer, ...) — verified against all 115 roles.
    "qa": {"quality", "assurance", "test", "tester", "testing"},
    # Same "no bare engineer" reasoning as qa/sre below.
    "qe": {"quality", "test", "tester", "testing"},
    # Placed in the Cloud/DevOps domain, since that's who actually does SRE
    # work in this catalog (no "SRE" title exists). Deliberately NOT "engineer"
    # (drags in every unrelated *Engineer title, same issue as "qa") nor
    # "platform" (only added Power Platform noise) — verified against all 115
    # real MCFlash roles.
    "sre": {"site", "reliability", "devops", "infrastructure", "cloud"},
    "ba": {"business", "analyst"},
    "pm": {"project", "manager"},
    "po": {"product", "owner"},
    "pmo": {"project", "management", "office"},
    "sm": {"scrum", "master"},
    "dba": {"database", "administrator"},
    # Bare "engineer" dropped (same reasoning as qa/sre/qe) — "software" alone
    # already reaches every software developer/tester title in the catalog.
    "swe": {"software"},
    "ml": {"machine", "learning"},
    "ux": {"user", "experience"},
    "ui": {"user", "interface"},
    "bi": {"business", "intelligence"},
}


def _expand_role_acronyms(tokens: set[str]) -> set[str]:
    """Replace each known acronym token with the words it stands for, so token
    overlap compares like with like (req "sre" -> {site, reliability, engineer})."""
    expanded: set[str] = set()
    for tok in tokens:
        if tok in ROLE_ACRONYMS:
            expanded |= ROLE_ACRONYMS[tok]
        else:
            expanded.add(tok)
    return expanded


# EN <-> IT for the common role/job-title vocabulary. The classifier may
# extract a role in either language ("Dynamics 365 consultant") while MCFlash
# / CV roles are mostly Italian ("Consulente ERP") — without this, token
# overlap is zero even when the role is conceptually identical. Each pair is
# listed once; _expand_role_translations adds both directions. Kept in the
# Search Layer only (not in the MCFlash hard-select), same as acronyms.
ROLE_TRANSLATION_PAIRS = [
    ("consultant", "consulente"),
    ("consultants", "consulenti"),
    ("developer", "sviluppatore"),
    ("engineer", "ingegnere"),
    ("analyst", "analista"),
    ("manager", "responsabile"),
    ("architect", "architetto"),
    ("administrator", "amministratore"),
    ("specialist", "specialista"),
    ("support", "supporto"),
    ("security", "sicurezza"),
    ("tester", "collaudatore"),
    ("designer", "progettista"),
    ("coordinator", "coordinatore"),
    ("director", "direttore"),
    ("owner", "responsabile"),
    ("lead", "responsabile"),
    ("assistant", "assistente"),
    ("technician", "tecnico"),
]
ROLE_TRANSLATIONS: dict[str, str] = {}
for _en, _it in ROLE_TRANSLATION_PAIRS:
    ROLE_TRANSLATIONS[_en] = _it
    ROLE_TRANSLATIONS[_it] = _en


def _expand_role_translations(tokens: set[str]) -> set[str]:
    """Add the EN<->IT counterpart of each recognised word, keeping the
    original (unlike acronyms, both forms can legitimately appear literally)."""
    expanded = set(tokens)
    for tok in tokens:
        translation = ROLE_TRANSLATIONS.get(tok)
        if translation:
            expanded.add(translation)
    return expanded


# =========================================================
# OData filter builder
# =========================================================

def build_odata_filter(
    skills: list[str] | None = None,
    min_experience_years: float | None = None,
    max_experience_years: float | None = None,
) -> str | None:
    """
    Costruisce un filtro OData combinando i constraint passati.
    Ritorna None se nessun filtro è richiesto.
    """
    clauses: list[str] = []

    if skills:
        for skill in skills:
            safe = skill.replace("'", "''")
            clauses.append(f"skills/any(s: s eq '{safe}')")

    if min_experience_years is not None:
        clauses.append(f"experience_years ge {min_experience_years}")

    if max_experience_years is not None:
        clauses.append(f"experience_years le {max_experience_years}")

    return " and ".join(clauses) if clauses else None


def build_odata_filter_relaxed(
    min_experience_years: float | None = None,
    max_experience_years: float | None = None,
) -> str | None:
    """Versione rilassata: rimuove il filtro skills, mantiene gli altri."""
    return build_odata_filter(
        skills=None,
        min_experience_years=min_experience_years,
        max_experience_years=max_experience_years,
    )


# =========================================================
# Reranker
# =========================================================

def _months_ago(iso_date: str | None, now: datetime) -> float | None:
    if not iso_date:
        return None
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = now - dt
        return delta.days / 30.0
    except Exception:
        return None


def _structured_rerank_score(
    hit: dict,
    requested_skills: list[str],
    requested_role: str,
    requested_seniority: str,
) -> float | None:
    """Deterministic 0..1 relevance from the structured signals the request
    actually carries (skills / role / seniority), each scored with the same
    helpers used for match_features and renormalised over the requested
    dimensions only. Returns None when the request carries no structured
    signal at all (pure free-text intent) — the caller then relies on
    retrieval scores."""
    w_skill = settings.search_rerank_w_skill
    w_role = settings.search_rerank_w_role
    w_seniority = settings.search_rerank_w_seniority

    parts: list[tuple[float, float]] = []  # (weight, score)

    if requested_skills:
        sk = _skills_match_features(requested_skills, _norm_list(hit.get("skills")))
        parts.append((w_skill, float(sk.get("score") or 0.0)))
    if requested_role:
        ro = _role_match_features(requested_role, _norm_text(hit.get("role")))
        parts.append((w_role, float(ro.get("score") or 0.0)))
    if requested_seniority:
        se = _seniority_match_features(
            requested_seniority, _norm_text(hit.get("seniority")), _norm_text(hit.get("role"))
        )
        parts.append((w_seniority, float(se.get("score") or 0.0)))

    total_weight = sum(w for w, _ in parts)
    if total_weight <= 0:
        return None
    return sum(w * s for w, s in parts) / total_weight


def rerank(
    hits: list[dict],
    query_skills: list[str] | None = None,
    query_role: str | None = None,
    query_location: str | None = None,
    top: int = 10,
    query_seniority: str | None = None,
) -> list[dict]:
    """
    Ranking ibrido: blocco retrieval (semantico/vettoriale/lessicale) + blocco
    structured deterministico (skill/ruolo/seniority).

    - Se il blocco retrieval ha segnale (semantico o vettoriale > 0):
        score = retrieval * retrieval_weight + structured * structured_weight
      lo structured fa da tie-breaker.
    - Se il blocco retrieval NON ha segnale (query tutta strutturata, nessun
      testo libero -> niente pass semantico/embedding): lo structured DIVENTA
      il ranking. Fallback finale su lex se non c'e' nemmeno lo structured.
    - recency_boost (< 6 mesi) sempre additivo.

    query_location e' mantenuto per compatibilita' di firma.
    """
    now = datetime.now(timezone.utc)
    recb = settings.search_reranker_recency_boost
    w_retrieval = settings.search_rerank_retrieval_weight
    w_structured = settings.search_rerank_structured_weight
    _ = query_location

    requested_skills = _norm_list(query_skills)
    requested_role = _norm_text(query_role)
    requested_seniority = _norm_text(query_seniority)
    # A rank word in the role ("QA lead") with no explicit seniority still
    # carries a level preference — score it as senior (soft: ranking only).
    if not requested_seniority and requested_role and MCFlashCandidatesClient._role_has_level_token(requested_role):
        requested_seniority = "senior"

    def _norm_semantic(value: Any) -> float:
        # Azure semantic reranker score is commonly in [0,4].
        try:
            return max(0.0, min(1.0, float(value) / 4.0))
        except Exception:
            return 0.0

    def _norm_retrieval(value: Any) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except Exception:
            return 0.0

    def _raw_lex(value: Any) -> float:
        try:
            return max(0.0, float(value))
        except Exception:
            return 0.0

    # Lexical (BM25) scores are unbounded (~0.5..15) and a hard clamp to [0,1]
    # collapses almost every hit to 1.0 -> no discrimination. Normalise
    # relative to the current batch instead.
    max_lex = max((_raw_lex(h.get("lex_score", 0.0)) for h in hits), default=0.0)

    scored: list[dict] = []
    for hit in hits:
        semantic_norm = _norm_semantic(hit.get("semantic_score", 0.0))
        vec_norm = _norm_retrieval(hit.get("vec_score", 0.0))
        lex_norm = (_raw_lex(hit.get("lex_score", 0.0)) / max_lex) if max_lex > 0 else 0.0

        retrieval = semantic_norm * 0.70 + vec_norm * 0.20 + lex_norm * 0.10
        structured = _structured_rerank_score(
            hit, requested_skills, requested_role, requested_seniority
        )
        has_retrieval_signal = semantic_norm > 0 or vec_norm > 0

        if has_retrieval_signal:
            if structured is not None:
                s = retrieval * w_retrieval + structured * w_structured
            else:
                s = retrieval
        else:
            if structured is not None:
                s = structured
            else:
                s = lex_norm

        # recency boost (< 6 months)
        months = _months_ago(hit.get("processed_at"), now)
        if months is not None and months <= 6:
            s += recb

        entry = dict(hit)
        entry["score"] = round(s, 6)
        scored.append(entry)

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top]


def _norm_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def _norm_list(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, list):
        return [_norm_text(v) for v in values if _norm_text(v)]
    if isinstance(values, str):
        return [_norm_text(v) for v in values.split(",") if _norm_text(v)]
    return []


def _seniority_from_years(years: float | None) -> str | None:
    if years is None:
        return None
    if years < 2:
        return "junior"
    if years < 5:
        return "mid"
    return "senior"


def _derive_request_seniority(
    explicit_seniority: str | None,
    *,
    min_experience_years: float | None,
) -> tuple[str | None, bool]:
    normalized_explicit = _norm_text(explicit_seniority) or None
    if normalized_explicit:
        return normalized_explicit, False

    # Derive only from the lower bound. Using max years would create an
    # arbitrary hard label and make broad requests overly brittle.
    if min_experience_years is not None:
        return _seniority_from_years(min_experience_years), True

    return None, False


def _parse_experience_years_from_query(query: str) -> tuple[float | None, float | None]:
    text = _norm_text(query)
    if not text:
        return None, None

    min_years: float | None = None
    max_years: float | None = None

    patterns_min = [
        r"\balmeno\s+(\d+(?:[.,]\d+)?)\s*(?:anni?|years?)\b",
        r"\bda\s+(\d+(?:[.,]\d+)?)\s*(?:anni?|years?)\b",
        r"\b(\d+(?:[.,]\d+)?)\s*\+\s*(?:anni?|years?)\b",
        r"\b(\d+(?:[.,]\d+)?)\s*(?:anni?|years?)\s*(?:di )?esperienz",
    ]
    patterns_max = [
        r"\bmassimo\s+(\d+(?:[.,]\d+)?)\s*(?:anni?|years?)\b",
        r"\bentro\s+(\d+(?:[.,]\d+)?)\s*(?:anni?|years?)\b",
        r"\bfino a\s+(\d+(?:[.,]\d+)?)\s*(?:anni?|years?)\b",
    ]
    range_patterns = [
        r"\btra\s+(\d+(?:[.,]\d+)?)\s+e\s+(\d+(?:[.,]\d+)?)\s*(?:anni?|years?)\b",
        r"\b(\d+(?:[.,]\d+)?)\s*[-–]\s*(\d+(?:[.,]\d+)?)\s*(?:anni?|years?)\b",
    ]

    for pattern in range_patterns:
        match = re.search(pattern, text)
        if match:
            try:
                min_years = float(match.group(1).replace(",", "."))
                max_years = float(match.group(2).replace(",", "."))
                return min_years, max_years
            except ValueError:
                pass

    for pattern in patterns_min:
        match = re.search(pattern, text)
        if match:
            try:
                min_years = float(match.group(1).replace(",", "."))
                break
            except ValueError:
                pass

    for pattern in patterns_max:
        match = re.search(pattern, text)
        if match:
            try:
                max_years = float(match.group(1).replace(",", "."))
                break
            except ValueError:
                pass

    return min_years, max_years


def _clamp_01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _score_value(value: Any, *, applicable: bool) -> float | None:
    if not applicable:
        return None
    try:
        return round(_clamp_01(float(value)), 4)
    except Exception:
        return 0.0


def _skills_match_features(requested_skills: list[str], candidate_skills: list[str]) -> dict[str, Any]:
    matched: list[str] = []
    semantic_matches: list[str] = []

    for req_skill in requested_skills:
        if req_skill in candidate_skills:
            matched.append(req_skill)
            continue

        # Only the generic->specific direction counts: requested "spring" is
        # covered by candidate "spring boot" (req_skill in cs). The opposite
        # (candidate "java" for requested "java 17") is NOT a match — a bare
        # "java" says nothing about the version asked for.
        semantic_hit = next(
            (cs for cs in candidate_skills if req_skill in cs),
            None,
        )
        if semantic_hit:
            semantic_matches.append(req_skill)

    if not requested_skills:
        score = 0.0
    else:
        score = (len(set(matched)) + 0.7 * len(set(semantic_matches))) / len(requested_skills)

    return {
        "score": round(_clamp_01(score), 4),
        "matched": sorted(set(matched)),
        "semantic_matches": sorted(set(semantic_matches)),
    }


def _role_match_features(requested_role: str, candidate_role: str) -> dict[str, Any]:
    if not requested_role:
        return {"applicable": False, "score": None, "match": "not_requested"}
    if requested_role == candidate_role:
        return {"applicable": True, "score": 1.0, "match": "exact"}

    if _phrase_in(requested_role, candidate_role) or _phrase_in(candidate_role, requested_role):
        return {"applicable": True, "score": 0.8, "match": "semantic"}

    req_raw = _role_tokens(requested_role) - ROLE_NOISE
    cand_raw = _role_tokens(candidate_role) - ROLE_NOISE

    # Acronym expansion (SRE, BA, PM, ...). Search Layer only.
    req_tokens = _expand_role_translations(_expand_role_acronyms(req_raw))
    cand_tokens = _expand_role_translations(_expand_role_acronyms(cand_raw))

    overlap = len(req_tokens & cand_tokens) if req_tokens and cand_tokens else 0
    if overlap <= 0:
        return {"applicable": True, "score": 0.0, "match": "none"}

    # The discipline must be what overlaps: if the request names a discipline
    # that exists in the catalog ("qa" in "qa lead" -> {test, tester, ...}) and
    # none of those tokens reach the candidate, a lone level-token overlap
    # ("lead"/"manager" <-> "responsabile") is not a role match. Unknown-
    # discipline requests ("Delivery Manager") keep scoring on "manager".
    _vocab = MCFlashCandidatesClient._role_discipline_vocab()
    domain_raw = {
        t
        for t in (req_raw - ROLE_LEVEL_TOKENS)
        if _expand_role_acronyms({t}) & _vocab
    }
    if domain_raw:
        domain_tokens = _expand_role_translations(_expand_role_acronyms(domain_raw))
        if not (domain_tokens & cand_tokens):
            return {"applicable": True, "score": 0.0, "match": "none"}

    score = overlap / max(1, len(req_tokens))
    if score >= 0.4:
        return {"applicable": True, "score": round(_clamp_01(score), 4), "match": "semantic"}
    return {"applicable": True, "score": round(_clamp_01(score), 4), "match": "partial"}


def _location_match_features(requested_location: str, candidate_location: str, work_mode: str) -> dict[str, Any]:
    if not requested_location:
        return {"applicable": False, "score": None, "match": "not_requested"}
    if work_mode == "remote":
        return {"applicable": True, "score": 1.0, "match": "not_applicable"}
    if not candidate_location:
        return {"applicable": True, "score": 0.0, "match": "none"}
    if requested_location == candidate_location:
        return {"applicable": True, "score": 1.0, "match": "exact"}
    if requested_location in candidate_location or candidate_location in requested_location:
        return {"applicable": True, "score": 0.6, "match": "soft"}

    req_tokens = set(requested_location.split())
    cand_tokens = set(candidate_location.split())
    overlap = len(req_tokens & cand_tokens) if req_tokens and cand_tokens else 0
    if overlap > 0:
        return {"applicable": True, "score": 0.35, "match": "weak"}
    return {"applicable": True, "score": 0.0, "match": "none"}


def _language_match_features(requested_language: str, candidate_language: str) -> dict[str, Any]:
    if not requested_language:
        return {"applicable": False, "score": None, "match": "not_requested"}
    if not candidate_language:
        return {"applicable": True, "score": 0.0, "match": False}
    is_match = requested_language in candidate_language or candidate_language in requested_language
    return {"applicable": True, "score": 1.0 if is_match else 0.0, "match": bool(is_match)}


_SENIORITY_RANK = {"junior": 0, "mid": 1, "senior": 2}


def _seniority_match_features(
    requested_seniority: str,
    candidate_seniority: str,
    candidate_role: str = "",
) -> dict[str, Any]:
    """Graded seniority match on the junior<mid<senior ladder.

    - request and candidate values normalised through the MCFlash rules
      (Medium->mid, Neo->junior, lead/manager/principal->senior);
    - a leadership word in the candidate's job title ("Test Leader") satisfies
      a senior/lead request whatever the Seniority field says;
    - candidate with no seniority data and no title signal -> neutral 0.5,
      not a zero (missing data must not sink an otherwise good hit).
    """
    req = MCFlashCandidatesClient._normalize_seniority_key(requested_seniority or "")
    if not req:
        return {"applicable": False, "score": None, "match": "not_requested"}

    cand = MCFlashCandidatesClient._normalize_seniority_key(candidate_seniority or "")
    if (
        req == "senior"
        and _SENIORITY_RANK.get(cand, -1) < 2
        and MCFlashCandidatesClient._role_title_has_leadership(candidate_role or "")
    ):
        cand = "senior"

    if not cand:
        return {"applicable": True, "score": 0.5, "match": "unknown"}
    if cand not in _SENIORITY_RANK or req not in _SENIORITY_RANK:
        exact = cand == req
        return {"applicable": True, "score": 1.0 if exact else 0.0, "match": "exact" if exact else "none"}

    delta = abs(_SENIORITY_RANK[req] - _SENIORITY_RANK[cand])
    if delta == 0:
        return {"applicable": True, "score": 1.0, "match": "exact"}
    score = max(0.0, 1.0 - delta / 2.0)
    return {"applicable": True, "score": round(score, 4), "match": "partial" if score > 0 else "none"}


def _coerce_availability_days(value: Any, *, parser) -> int | None:
    """
    Parse a raw availability value (int/float day-count, or free text like
    "immediata", "20 gg", "dal 19/01", a weekday name...) into a day-count.
    Delegates text parsing to MCFlashCandidatesClient's parsers so the
    interpretation is identical to the one used for MCFlash hard-select
    matching (MCFlash's own `Disponibilita` field uses these same formats).
    """
    if isinstance(value, (int, float)):
        n = int(value)
        return n if n >= 0 else None
    if isinstance(value, str) and value.strip():
        return parser(value)
    return None


def _availability_match_features(
    required: bool,
    candidate_availability_days: int | None,
    requested_availability_days: int | None = None,
) -> dict[str, Any]:
    if not required:
        return {"applicable": False, "score": None, "match": "not_requested"}
    if candidate_availability_days is None:
        return {"applicable": True, "score": 0.0, "match": "none"}
    if requested_availability_days is not None:
        # Compare against the cap the user actually asked for, instead of a
        # fixed default: "entro 20 giorni" must not be scored the same as
        # "entro 60 giorni".
        if candidate_availability_days <= requested_availability_days:
            return {"applicable": True, "score": 1.0, "match": "exact"}
        if candidate_availability_days <= requested_availability_days * 2:
            return {"applicable": True, "score": 0.6, "match": "partial"}
        return {"applicable": True, "score": 0.2, "match": "weak"}
    if candidate_availability_days <= 30:
        return {"applicable": True, "score": 1.0, "match": "exact"}
    if candidate_availability_days <= 60:
        return {"applicable": True, "score": 0.6, "match": "partial"}
    return {"applicable": True, "score": 0.2, "match": "weak"}


def _canonicalize_match_features_contract(match_features: dict[str, Any]) -> dict[str, Any]:
    skills = match_features.get("skills") if isinstance(match_features.get("skills"), dict) else {}
    role = match_features.get("role") if isinstance(match_features.get("role"), dict) else {}
    location = match_features.get("location") if isinstance(match_features.get("location"), dict) else {}
    language = match_features.get("language") if isinstance(match_features.get("language"), dict) else {}
    seniority = match_features.get("seniority") if isinstance(match_features.get("seniority"), dict) else {}
    availability = match_features.get("availability") if isinstance(match_features.get("availability"), dict) else {}

    role_match = _norm_text(role.get("match"))
    if role_match not in {"exact", "semantic", "partial", "none", "not_requested"}:
        role_match = "none"

    location_match = _norm_text(location.get("match"))
    if location_match not in {"exact", "soft", "weak", "none", "not_applicable", "not_requested"}:
        location_match = "none"

    language_match_raw = language.get("match")
    if isinstance(language_match_raw, bool):
        language_match: bool | str = language_match_raw
    else:
        language_match = "not_requested" if _norm_text(language_match_raw) == "not_requested" else False

    seniority_match = _norm_text(seniority.get("match"))
    if seniority_match not in {"exact", "partial", "unknown", "none", "not_requested"}:
        seniority_match = "none"

    availability_match = _norm_text(availability.get("match"))
    if availability_match not in {"exact", "partial", "weak", "none", "not_requested"}:
        availability_match = "none"

    role_applicable = bool(role.get("applicable", role_match != "not_requested"))
    location_applicable = bool(location.get("applicable", location_match != "not_requested"))
    language_applicable = bool(language.get("applicable", language_match != "not_requested"))
    seniority_applicable = bool(seniority.get("applicable", seniority_match != "not_requested"))
    availability_applicable = bool(availability.get("applicable", availability_match != "not_requested"))

    return {
        "skills": {
            "score": round(_clamp_01(skills.get("score", 0.0)), 4),
            "matched": _norm_list(skills.get("matched")),
            "semantic_matches": _norm_list(skills.get("semantic_matches")),
        },
        "role": {
            "applicable": role_applicable,
            "score": _score_value(role.get("score"), applicable=role_applicable),
            "match": role_match,
        },
        "location": {
            "applicable": location_applicable,
            "score": _score_value(location.get("score"), applicable=location_applicable),
            "match": location_match,
        },
        "language": {
            "applicable": language_applicable,
            "score": _score_value(language.get("score"), applicable=language_applicable),
            "match": language_match,
        },
        "seniority": {
            "applicable": seniority_applicable,
            "score": _score_value(seniority.get("score"), applicable=seniority_applicable),
            "match": seniority_match,
        },
        "availability": {
            "applicable": availability_applicable,
            "score": _score_value(availability.get("score"), applicable=availability_applicable),
            "match": availability_match,
        },
    }


def build_match_features(
    candidate: dict[str, Any],
    *,
    query_skills: list[str] | None,
    query_role: str | None,
    query_location: str | None,
    query_language: str | None,
    query_seniority: str | None,
    query_availability_required: bool,
    work_mode: str,
    relaxed_criteria: list[str],
    is_relaxed_result: bool,
    query_availability_days: int | None = None,
) -> dict[str, Any]:
    requested_skills = _norm_list(query_skills)
    candidate_skills = _norm_list(candidate.get("skills"))
    requested_role = _norm_text(query_role)
    candidate_role = _norm_text(candidate.get("role"))
    requested_location = _norm_text(query_location)
    candidate_location = _norm_text(candidate.get("location"))
    requested_language = _norm_text(query_language)
    candidate_language = _norm_text(candidate.get("language"))
    requested_seniority = _norm_text(query_seniority)
    if not requested_seniority and requested_role and MCFlashCandidatesClient._role_has_level_token(requested_role):
        requested_seniority = "senior"
    candidate_seniority = _norm_text(candidate.get("seniority"))

    # "availability_days" and "availability" both carry free-text formats in
    # practice (MCFlash's own Disponibilita' field uses "Immediata", "20 gg",
    # "dal 19/01", weekday names...) — parse with the same rules used for
    # MCFlash hard-select matching instead of a naive int()/digit-regex, which
    # silently drops "Immediata" (no digits) and misreads "dal 19/01" as day 19.
    candidate_availability_days = _coerce_availability_days(
        candidate.get("availability_days"),
        parser=MCFlashCandidatesClient._parse_candidate_availability_days,
    )
    if candidate_availability_days is None:
        candidate_availability_days = _coerce_availability_days(
            candidate.get("availability"),
            parser=MCFlashCandidatesClient._parse_candidate_availability_days,
        )

    skills_features = _skills_match_features(requested_skills, candidate_skills)
    role_features = _role_match_features(requested_role, candidate_role)
    location_features = _location_match_features(
        requested_location,
        candidate_location,
        _norm_text(work_mode) or "unknown",
    )
    language_features = _language_match_features(requested_language, candidate_language)
    seniority_features = _seniority_match_features(
        requested_seniority, candidate_seniority, candidate_role
    )
    availability_features = _availability_match_features(
        bool(query_availability_required),
        candidate_availability_days,
        query_availability_days,
    )

    matched_on: list[str] = []
    if skills_features["score"] > 0:
        matched_on.append("skills")
    if float(role_features.get("score") or 0.0) > 0:
        matched_on.append("role")
    if float(location_features.get("score") or 0.0) > 0:
        matched_on.append("location")
    if language_features["match"] is True:
        matched_on.append("language")
    if seniority_features.get("score") and float(seniority_features.get("score") or 0.0) > 0:
        matched_on.append("seniority")
    if availability_features.get("score") and float(availability_features.get("score") or 0.0) > 0:
        matched_on.append("availability")

    contract = _canonicalize_match_features_contract(
        {
            "skills": skills_features,
            "role": role_features,
            "location": location_features,
            "language": language_features,
            "seniority": seniority_features,
            "availability": availability_features,
        }
    )

    contract["relaxed_criteria"] = list(dict.fromkeys(relaxed_criteria if is_relaxed_result else []))
    contract["is_relaxed_result"] = bool(is_relaxed_result)
    contract["matched_on"] = matched_on
    return contract


def enrich_hits_with_match_features(
    hits: list[dict[str, Any]],
    *,
    query_skills: list[str] | None,
    query_role: str | None,
    query_location: str | None,
    query_language: str | None,
    query_seniority: str | None,
    query_availability_required: bool,
    work_mode: str,
    relaxed_criteria: list[str],
    query_availability_days: int | None = None,
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for hit in hits:
        entry = dict(hit)
        entry["match_features"] = build_match_features(
            entry,
            query_skills=query_skills,
            query_role=query_role,
            query_location=query_location,
            query_language=query_language,
            query_seniority=query_seniority,
            query_availability_required=query_availability_required,
            work_mode=work_mode,
            relaxed_criteria=relaxed_criteria,
            is_relaxed_result=bool(entry.get("is_relaxed_result", False)),
            query_availability_days=query_availability_days,
        )
        enriched.append(entry)
    return enriched


# =========================================================
# Request normaliser
# =========================================================

def normalise_search_request(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Normalizza e valida i parametri di ricerca dal body della richiesta.
    Ritorna il dict normalizzato pronto per l'uso nell'handler.
    """
    raw_skills = payload.get("skills") or []
    skills: list[str] = []
    seen_skills: set[str] = set()
    for raw_skill in raw_skills:
        skill = str(raw_skill).lower().strip() if raw_skill is not None else ""
        if not skill or skill in seen_skills:
            continue
        seen_skills.add(skill)
        skills.append(skill)

    query = str(payload.get("query") or "").strip()
    role = str(payload.get("role") or "").strip() or None
    location = str(payload.get("location") or "").strip() or None
    explicit_seniority = str(payload.get("seniority") or "").strip() or None
    language = str(payload.get("language") or "").strip() or None
    subco = str(payload.get("subco") or "").strip().lower() or None
    work_mode = str(payload.get("work_mode") or "").strip().lower() or "unknown"
    if work_mode not in {"remote", "hybrid", "onsite", "unknown"}:
        work_mode = "unknown"

    raw_relaxed = payload.get("relaxed_criteria") or []
    relaxed_criteria = sorted({str(v).strip().lower() for v in raw_relaxed if str(v).strip()})

    try:
        top = int(payload.get("top") or 10)
        top = max(1, min(top, 100))
    except (ValueError, TypeError):
        top = 10

    try:
        min_exp = float(payload["min_experience_years"]) if payload.get("min_experience_years") is not None else None
    except (ValueError, TypeError):
        min_exp = None

    try:
        max_exp = float(payload["max_experience_years"]) if payload.get("max_experience_years") is not None else None
    except (ValueError, TypeError):
        max_exp = None

    # Backward-compatible aliases from classifier/request wrappers.
    # If min/max are not provided explicitly, infer them from years_of_experience.
    raw_years = payload.get("years_of_experience")
    if raw_years is None:
        raw_years = payload.get("experience_years")

    def _to_years_number(value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            text = value.strip().replace(",", ".")
            if not text:
                return None
            try:
                return float(text)
            except ValueError:
                return None
        return None

    alias_min_exp: float | None = None
    alias_max_exp: float | None = None
    if isinstance(raw_years, dict):
        alias_min_exp = _to_years_number(raw_years.get("min"))
        alias_max_exp = _to_years_number(raw_years.get("max"))
    elif isinstance(raw_years, (int, float)):
        alias_min_exp = float(raw_years)
    elif isinstance(raw_years, str):
        raw_years_text = raw_years.strip()
        if raw_years_text:
            range_match = re.search(r"(\d+(?:[.,]\d+)?)\s*[-–]\s*(\d+(?:[.,]\d+)?)", raw_years_text)
            if range_match:
                try:
                    alias_min_exp = float(range_match.group(1).replace(",", "."))
                    alias_max_exp = float(range_match.group(2).replace(",", "."))
                except ValueError:
                    alias_min_exp = None
                    alias_max_exp = None
            else:
                alias_min_exp = _to_years_number(raw_years_text)
                if alias_min_exp is None:
                    alias_min_exp, alias_max_exp = _parse_experience_years_from_query(raw_years_text)

    if min_exp is None and alias_min_exp is not None:
        min_exp = alias_min_exp
    if max_exp is None and alias_max_exp is not None:
        max_exp = alias_max_exp

    inferred_min_exp, inferred_max_exp = _parse_experience_years_from_query(query)
    if min_exp is None:
        min_exp = inferred_min_exp
    if max_exp is None:
        max_exp = inferred_max_exp

    seniority, seniority_inferred = _derive_request_seniority(
        explicit_seniority,
        min_experience_years=min_exp,
    )

    hybrid = bool(payload.get("hybrid", True))

    # Support classifier outputs where availability can arrive as free text
    # (e.g. "entro lunedi") instead of an explicit boolean flag.
    availability = payload.get("availability")
    availability_date_raw = payload.get("availability_date")
    availability_date = (
        str(availability_date_raw).strip() if availability_date_raw is not None else ""
    ) or None

    # availability_days arrives from the classifier as free text as often as a
    # number ("immediata", "20 gg", "dal 19/01"...) — MCFlash's own
    # Disponibilita' field uses the same formats. Parse with the same rules
    # used for MCFlash hard-select matching instead of a bare int(), which
    # would silently discard any non-numeric value to None.
    availability_days = _coerce_availability_days(
        payload.get("availability_days"),
        parser=MCFlashCandidatesClient._parse_requested_availability_days,
    )

    availability_required = bool(payload.get("availability_required", False))
    if isinstance(availability, bool):
        availability_required = availability_required or availability
    elif isinstance(availability, (int, float)):
        availability_required = availability_required or bool(availability)
    elif isinstance(availability, str):
        availability_required = availability_required or bool(availability.strip())

    strict = bool(payload.get("strict", True))

    return {
        "query": query,
        "skills": skills,
        "role": role,
        "location": location,
        "work_mode": work_mode,
        "seniority": seniority,
        "seniority_explicit": _norm_text(explicit_seniority) or None,
        "seniority_inferred": seniority_inferred,
        "language": language,
        "subco": subco,
        "top": top,
        "strict": strict,
        "relaxed_criteria": relaxed_criteria,
        "min_experience_years": min_exp,
        "max_experience_years": max_exp,
        "hybrid": hybrid,
        "availability": availability,
        "availability_date": availability_date,
        "availability_days": availability_days,
        "availability_required": availability_required,
    }


# =========================================================
# Index routing
# =========================================================

def resolve_index(subco: str | None) -> str:
    return settings.document_search_index_name

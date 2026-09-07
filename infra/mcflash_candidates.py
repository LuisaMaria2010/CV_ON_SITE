from __future__ import annotations

import asyncio
import json
import os
import re
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class MCFlashApiError(RuntimeError):
    """Raised when MCFlash candidate API is unavailable or returns an invalid response."""


class MCFlashCandidatesClient:
    _ROLE_BUDGET_PLACEHOLDER_TABLE: dict[str, dict[str, float]] | None = None
    _ROLE_BUDGET_PLACEHOLDER_REDUCED: dict[str, dict[str, float]] | None = None

    # Qualitative budget phrasing (no explicit number) still needs a direction:
    # "budget non troppo elevato" should tighten the placeholder cap, "budget
    # importante" should loosen it. Negated/longer phrases are listed in the LOW
    # set explicitly and checked before the HIGH set, so "non troppo elevato"
    # wins over the bare "elevato" token it contains.
    _BUDGET_QUALIFIER_LOW_PHRASES: tuple[str, ...] = (
        "non troppo elevato", "non troppo elevata",
        "non troppo alto", "non troppo alta",
        "non elevato", "non elevata",
        "non alto", "non alta",
        "basso", "bassa",
        "contenuto", "contenuta",
        "limitato", "limitata",
        "ridotto", "ridotta",
        "economico", "economica",
        "stretto", "stretta",
        "modesto", "modesta",
        "risicato", "risicata",
    )
    _BUDGET_QUALIFIER_HIGH_PHRASES: tuple[str, ...] = (
        "molto alto", "molto alta",
        "molto elevato", "molto elevata",
        "alto", "alta",
        "elevato", "elevata",
        "importante",
        "generoso", "generosa",
        "ampio", "ampia",
        "sostanzioso", "sostanziosa",
        "consistente",
        "premium",
    )
    _BUDGET_QUALIFIER_LOW_ADJUSTMENT = 0.8
    _BUDGET_QUALIFIER_HIGH_ADJUSTMENT = 1.2

    @classmethod
    def _classify_budget_qualifier(cls, text: str) -> str | None:
        """Best-effort direction ('low'/'high') for a non-numeric budget phrase."""
        normalized = (text or "").strip().lower()
        if not normalized:
            return None
        for phrase in cls._BUDGET_QUALIFIER_LOW_PHRASES:
            if phrase in normalized:
                return "low"
        for phrase in cls._BUDGET_QUALIFIER_HIGH_PHRASES:
            if phrase in normalized:
                return "high"
        return None

    @classmethod
    def _normalize_role_text(cls, role: str) -> str:
        text = (role or "").strip().lower()
        if not text:
            return ""

        # Align common synonyms and naming variants seen in MCFlash.
        replacements = {
            "artificial intelligence": "ai",
            "machine learning": "ml",
            "deep learning": "dl",
            "cyber security": "cybersecurity",
            "full stack": "fullstack",
            "front end": "frontend",
            "back end": "backend",
            "front-end": "frontend",
            "back-end": "backend",
            "help desk": "helpdesk",
            "data base": "database",
            "phyton": "python",
            "developer": "dev",
            "sviluppatore": "dev",
            "sviluppatrice": "dev",
            "engineer": "eng",
            "ingegnere": "eng",
            "administrator": "admin",
            "specialist": "spec",
        }
        for old, new in replacements.items():
            text = text.replace(old, new)

        text = re.sub(r"\s+", " ", text).strip()
        return text

    @classmethod
    def _load_role_budget_placeholders(cls) -> dict[str, dict[str, float]]:
        if MCFlashCandidatesClient._ROLE_BUDGET_PLACEHOLDER_TABLE is not None:
            cls._ROLE_BUDGET_PLACEHOLDER_TABLE = MCFlashCandidatesClient._ROLE_BUDGET_PLACEHOLDER_TABLE
            return MCFlashCandidatesClient._ROLE_BUDGET_PLACEHOLDER_TABLE

        table_raw: dict[str, dict[str, float]] = {}
        file_path = os.path.join(os.path.dirname(__file__), "mcflash_budget_placeholders.json")
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                for role_key, values in raw.items():
                    if not isinstance(values, dict):
                        continue
                    role_norm = (str(role_key).strip().lower())
                    if not role_norm:
                        continue
                    normalized_row: dict[str, float] = {}
                    for s_key, cap in values.items():
                        s_norm = cls._normalize_seniority_key(str(s_key))
                        if not s_norm:
                            continue
                        try:
                            normalized_row[s_norm] = float(cap)
                        except (TypeError, ValueError):
                            continue
                    if normalized_row:
                        table_raw[role_norm] = normalized_row
        except Exception:
            table_raw = {}

        # Normalize composite roles while loading the table.
        # Example: "analista funzionale/ai strategy specialist" becomes
        # two roles with aggregated (median) caps per seniority.
        acc: dict[str, dict[str, list[float]]] = {}
        for role_label, row in table_raw.items():
            role_tokens = cls._explode_role_tokens(role_label)
            for token in role_tokens:
                token_row = acc.setdefault(token, {})
                for s_norm, cap in row.items():
                    token_row.setdefault(s_norm, []).append(float(cap))

        table: dict[str, dict[str, float]] = {}
        for token, values_by_seniority in acc.items():
            out_row: dict[str, float] = {}
            for s_norm, caps in values_by_seniority.items():
                if not caps:
                    continue
                out_row[s_norm] = float(statistics.median(caps))
            if out_row:
                table[token] = out_row

        MCFlashCandidatesClient._ROLE_BUDGET_PLACEHOLDER_TABLE = table
        MCFlashCandidatesClient._ROLE_BUDGET_PLACEHOLDER_REDUCED = None
        cls._ROLE_BUDGET_PLACEHOLDER_TABLE = table
        cls._ROLE_BUDGET_PLACEHOLDER_REDUCED = None
        return table

    @classmethod
    def _explode_role_tokens(cls, role: str) -> list[str]:
        text = cls._normalize_role_text(role)
        if not text:
            return []

        normalized = re.sub(r"\s+", " ", text)
        # Split composite labels: "role1/role2", "role1 & role2", "role1, role2", "role1 - role2".
        raw_parts = re.split(r"\s*(?:/|\||,|;|&|\+|\b e \b|\b and \b|\s-\s)\s*", normalized)
        is_composite = len([p for p in raw_parts if (p or "").strip()]) > 1
        tokens: list[str] = []
        for part in raw_parts:
            token = re.sub(r"\s+", " ", (part or "").strip())
            if len(token) < 3:
                continue
            tokens.append(token)

        # Keep full label only for non-composite roles.
        if not is_composite and normalized not in tokens:
            tokens.append(normalized)

        # Deduplicate while preserving order.
        out: list[str] = []
        seen: set[str] = set()
        for token in tokens:
            if token in seen:
                continue
            seen.add(token)
            out.append(token)
        return out

    @classmethod
    def _build_reduced_role_budget_placeholders(cls) -> dict[str, dict[str, float]]:
        if MCFlashCandidatesClient._ROLE_BUDGET_PLACEHOLDER_REDUCED is not None:
            cls._ROLE_BUDGET_PLACEHOLDER_REDUCED = MCFlashCandidatesClient._ROLE_BUDGET_PLACEHOLDER_REDUCED
            return MCFlashCandidatesClient._ROLE_BUDGET_PLACEHOLDER_REDUCED

        base = cls._load_role_budget_placeholders()
        acc: dict[str, dict[str, list[float]]] = {}

        for role_label, row in base.items():
            if not isinstance(row, dict):
                continue
            role_tokens = cls._explode_role_tokens(role_label)
            for token in role_tokens:
                token_row = acc.setdefault(token, {})
                for s_key, cap in row.items():
                    s_norm = cls._normalize_seniority_key(str(s_key))
                    if not s_norm:
                        continue
                    token_row.setdefault(s_norm, []).append(float(cap))

        reduced: dict[str, dict[str, float]] = {}
        for token, values_by_seniority in acc.items():
            out_row: dict[str, float] = {}
            for s_norm, caps in values_by_seniority.items():
                if not caps:
                    continue
                out_row[s_norm] = float(statistics.median(caps))
            if out_row:
                reduced[token] = out_row

        MCFlashCandidatesClient._ROLE_BUDGET_PLACEHOLDER_REDUCED = reduced
        cls._ROLE_BUDGET_PLACEHOLDER_REDUCED = reduced
        return reduced

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: int = 20,
    ) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.timeout_seconds = timeout_seconds

    def _request_json(self, params: dict[str, Any]) -> Any:
        if not self.base_url:
            raise MCFlashApiError("Missing MCFlash base URL")
        if not self.api_key:
            raise MCFlashApiError("Missing MCFlash API key")

        query = urlencode({k: v for k, v in params.items() if v is not None})
        url = self.base_url if not query else f"{self.base_url}?{query}"
        req = Request(
            url,
            method="GET",
            headers={
                "X-Api-Key": self.api_key,
                "Accept": "application/json",
            },
        )

        try:
            with urlopen(req, timeout=self.timeout_seconds) as resp:
                body = resp.read()
                payload = json.loads(body.decode("utf-8"))
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="ignore")[:300]
            except Exception:
                pass
            raise MCFlashApiError(
                f"MCFlash API HTTP {exc.code} while calling {self.base_url}. {detail}".strip()
            ) from exc
        except URLError as exc:
            raise MCFlashApiError(f"MCFlash API network error: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise MCFlashApiError("MCFlash API returned invalid JSON") from exc
        except Exception as exc:
            raise MCFlashApiError(f"MCFlash API call failed: {exc}") from exc

        return payload

    @staticmethod
    def _pick(candidate: dict[str, Any], *keys: str) -> str:
        """Return the first non-empty candidate value across key variants."""
        for key in keys:
            for probe in (key, key.lower(), key.upper()):
                value = candidate.get(probe)
                if value is not None and str(value).strip() != "":
                    return str(value)
        return ""

    @staticmethod
    def _work_mode_matches_sede(work_mode: str, sede_value: str) -> bool:
        mode = (work_mode or "").strip().lower()
        sede_l = (sede_value or "").strip().lower()
        if not mode:
            return True

        if mode in {"remote", "remoto"}:
            return "remot" in sede_l
        if mode in {"hybrid", "ibrido", "ibrida"}:
            return (
                "hybrid" in sede_l
                or "ibrid" in sede_l
                or "remot" in sede_l
                or "onsite" in sede_l
                or "in sede" in sede_l
            )
        if mode in {"onsite", "on_site", "in_sede", "insede"}:
            if "onsite" in sede_l or "in sede" in sede_l:
                return True
            # In MCFlash, onsite is often represented by city names only.
            has_remote_hint = any(token in sede_l for token in ("remot", "hybrid", "ibrid"))
            has_city_like_text = bool(re.search(r"[a-z\u00c0-\u017f]{3,}", sede_l))
            return has_city_like_text and not has_remote_hint

        return mode in sede_l

    @staticmethod
    def _location_matches_sede(location: str, sede_value: str) -> bool:
        location_l = (location or "").strip().lower()
        sede_l = (sede_value or "").strip().lower()
        if not location_l:
            return True
        if not sede_l:
            return False

        # Sede can be composite (e.g. "Torino/remoto").
        parts = [p.strip() for p in re.split(r"[/|,;]+", sede_l) if p.strip()]
        if sede_l not in parts:
            parts.append(sede_l)

        if len(location_l) < 3:
            return any(location_l == p for p in parts)

        return any(
            location_l == p
            or location_l in p
            or p in location_l
            for p in parts
        )

    @staticmethod
    def _language_matches(asked_language: str, candidate_languages: str) -> bool:
        def _normalize_language_text(text: str) -> str:
            normalized = (text or "").strip().lower()
            # Align common qualitative labels with the level taxonomy used by the bot.
            normalized = re.sub(r"\bbuon[oa]?\b", "intermedio", normalized)
            normalized = re.sub(r"\bgood\b", "intermedio", normalized)
            return normalized

        asked = _normalize_language_text(asked_language)
        value = _normalize_language_text(candidate_languages)
        if not asked:
            return True
        if not value:
            return False

        # Direct contains match first (works for full "inglese: intermedio").
        if asked in value:
            return True

        # If caller passes only language name (no level), match by language token.
        if ":" not in asked:
            language_token = asked.split("(", 1)[0].strip()
            if language_token and language_token in value:
                return True
            return False

        # If caller passed language + level, try matching both parts even with punctuation variations.
        lang_part, level_part = [p.strip() for p in asked.split(":", 1)]
        if lang_part and level_part:
            return lang_part in value and level_part in value

        return False

    @classmethod
    def _normalize_seniority_key(cls, seniority: str) -> str:
        value = (seniority or "").strip().lower()
        if not value:
            return ""
        if value in {"middle", "medium", "intermediate", "medior"}:
            return "mid"
        if value in {"staff", "expert"}:
            return "senior"
        if value.startswith("senior"):
            return "senior"
        if value.startswith("junior"):
            return "junior"
        if value.startswith("mid"):
            return "mid"
        return value

    @classmethod
    def _role_token_set(cls, role: str) -> set[str]:
        normalized = cls._normalize_role_text(role)
        if not normalized:
            return set()
        return {tok for tok in normalized.split(" ") if len(tok) >= 2}

    @classmethod
    def _role_matches(cls, requested_role: str, candidate_role: str) -> bool:
        req = cls._normalize_role_text(requested_role or "")
        cand = cls._normalize_role_text(candidate_role or "")
        if not req:
            return True
        if not cand:
            return False

        if req in cand or cand in req:
            return True

        req_tokens = cls._role_token_set(req)
        cand_tokens = cls._role_token_set(cand)
        if not req_tokens or not cand_tokens:
            return False

        role_noise = {
            "dev",
            "eng",
            "spec",
            "admin",
            "jr",
            "sr",
            "junior",
            "mid",
            "senior",
        }
        core_req_tokens = {t for t in req_tokens if t not in role_noise}
        if not core_req_tokens:
            core_req_tokens = req_tokens

        overlap = core_req_tokens & cand_tokens
        if not overlap:
            return False

        coverage = len(overlap) / max(1, len(core_req_tokens))
        return coverage >= 0.34

    @staticmethod
    def _parse_budget_numbers(text: str) -> list[float]:
        normalized = (text or "").strip().lower().replace(",", ".")
        if not normalized:
            return []
        return [float(value) for value in re.findall(r"\d+(?:\.\d+)?", normalized)]

    _AGE_UPPER_BOUND_MARKERS = (
        "under", "sotto", "meno di", "max", "massimo", "entro", "al massimo", "non oltre",
    )
    _AGE_LOWER_BOUND_MARKERS = (
        "over", "sopra", "oltre", "almeno", "min", "minimo", "non meno di", "piu' di", "più di",
    )
    # Tolleranza applicata a un'eta' "esatta"/"circa" (nessun marcatore di
    # direzione): un candidato di 30 anni per una richiesta "candidato di 30
    # anni" e' quello che serve, ma un'uguaglianza stretta escluderebbe quasi
    # chiunque - un range simmetrico e' l'interpretazione realistica.
    _AGE_EXACT_TOLERANCE_YEARS = 3

    @classmethod
    def _extract_requested_age_bounds(cls, raw_age: str) -> tuple[float | None, float | None]:
        """
        Interpreta un vincolo di eta' testuale in (min_age, max_age):
          - "under 30" / "massimo 28" ecc. -> (None, N): solo tetto massimo
          - "over 40" / "almeno 35" ecc. -> (N, None): solo soglia minima
          - un numero senza marcatore di direzione (eta' esatta/circa, es.
            "candidato di 30 anni") -> (N - tolleranza, N + tolleranza)
        """
        text = (raw_age or "").strip().lower()
        numbers = cls._parse_budget_numbers(text)
        if not numbers:
            return None, None
        value = numbers[0]

        if any(marker in text for marker in cls._AGE_LOWER_BOUND_MARKERS):
            return value, None
        if any(marker in text for marker in cls._AGE_UPPER_BOUND_MARKERS):
            return None, value
        return value - cls._AGE_EXACT_TOLERANCE_YEARS, value + cls._AGE_EXACT_TOLERANCE_YEARS

    @classmethod
    def _extract_candidate_age_value(cls, raw_eta: Any) -> float | None:
        numbers = cls._parse_budget_numbers(str(raw_eta) if raw_eta is not None else "")
        if not numbers:
            return None
        return numbers[0]

    @classmethod
    def _extract_requested_budget_cap_numeric(cls, raw_budget: str) -> float | None:
        numbers = cls._parse_budget_numbers(raw_budget)
        if not numbers:
            return None
        # Numeric user budget is interpreted as a max cap.
        return max(numbers)

    @classmethod
    def _extract_candidate_budget_value(cls, raw_budget: str) -> float | None:
        numbers = cls._parse_budget_numbers(raw_budget)
        if not numbers:
            return None
        # Candidate range is evaluated conservatively using upper bound.
        return max(numbers)

    @classmethod
    def _resolve_placeholder_budget_cap(
        cls,
        *,
        role: str,
        seniority: str,
    ) -> float | None:
        seniority_key = cls._normalize_seniority_key(seniority)
        if not seniority_key:
            return None

        role_raw = (role or "").strip().lower()
        role_raw = cls._normalize_role_text(role_raw)
        if role_raw:
            role_table_all = cls._load_role_budget_placeholders()
            direct = role_table_all.get(role_raw)
            if direct and seniority_key in direct:
                return direct[seniority_key]

            reduced = cls._build_reduced_role_budget_placeholders()

            # Try role fragments from request first.
            for probe in cls._explode_role_tokens(role_raw):
                row = reduced.get(probe)
                if row and seniority_key in row:
                    return row[seniority_key]

            # Fallback: containment match across reduced keys.
            candidates: list[tuple[int, float]] = []
            for token, row in reduced.items():
                if seniority_key not in row:
                    continue
                if token in role_raw or role_raw in token:
                    candidates.append((len(token), row[seniority_key]))
            if candidates:
                # Prefer the most specific (longest) role token.
                candidates.sort(key=lambda item: item[0], reverse=True)
                return candidates[0][1]

            # Last attempt: token overlap similarity against JSON roles only.
            req_tokens = cls._role_token_set(role_raw)
            if req_tokens:
                best_score = 0.0
                best_cap: float | None = None
                for token, row in reduced.items():
                    cap = row.get(seniority_key)
                    if cap is None:
                        continue
                    token_set = cls._role_token_set(token)
                    if not token_set:
                        continue
                    inter = len(req_tokens & token_set)
                    union = len(req_tokens | token_set)
                    if union == 0:
                        continue
                    score = inter / union
                    if inter > 0 and score > best_score:
                        best_score = score
                        best_cap = cap

                if best_cap is not None and best_score >= 0.34:
                    return best_cap

        return None

    @staticmethod
    def _days_until_weekday(target_weekday: int, now: datetime) -> int:
        delta = (target_weekday - now.weekday()) % 7
        return 7 if delta == 0 else delta

    @staticmethod
    def _month_token_to_number(token: str) -> int | None:
        month_map = {
            "gen": 1,
            "gennaio": 1,
            "feb": 2,
            "febbraio": 2,
            "mar": 3,
            "marzo": 3,
            "apr": 4,
            "aprile": 4,
            "mag": 5,
            "maggio": 5,
            "giu": 6,
            "giugno": 6,
            "lug": 7,
            "luglio": 7,
            "ago": 8,
            "agosto": 8,
            "set": 9,
            "settembre": 9,
            "ott": 10,
            "ottobre": 10,
            "nov": 11,
            "novembre": 11,
            "dic": 12,
            "dicembre": 12,
        }
        return month_map.get((token or "").strip().lower())

    @staticmethod
    def _days_from_day_month(day: int, month: int, year: int | None, *, now: datetime) -> int | None:
        if year is None:
            year = now.year
        elif year < 100:
            year += 2000
        try:
            dt = datetime(year, month, day, tzinfo=timezone.utc)
        except Exception:
            return None
        delta = (dt.date() - now.date()).days
        return max(0, delta)

    @staticmethod
    def _parse_requested_availability_days(raw: str) -> int | None:
        text = (raw or "").strip().lower()
        if not text:
            return None

        if any(token in text for token in ("immediat", "subito", "urgent")):
            return 0
        if "dopodomani" in text:
            return 2
        if "domani" in text:
            return 1

        m_days = re.search(r"(?:entro\s*)?(\d+)\s*(?:gg|giorn[oi])", text)
        if m_days:
            return max(0, int(m_days.group(1)))

        m_weeks = re.search(r"(?:entro\s*)?(\d+)\s*settiman", text)
        if m_weeks:
            return max(0, int(m_weeks.group(1)) * 7)

        m_date = re.search(r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?", text)
        if m_date:
            day = int(m_date.group(1))
            month = int(m_date.group(2))
            year_raw = m_date.group(3)
            now = datetime.now(timezone.utc)
            if year_raw is None:
                year = now.year
            else:
                year = int(year_raw)
                if year < 100:
                    year += 2000
            try:
                dt = datetime(year, month, day, tzinfo=timezone.utc)
            except Exception:
                return None
            delta = (dt.date() - now.date()).days
            return max(0, delta)

        m_month = re.search(r"\b(?:da|dal|dalla)\s+([a-z]+)\b", text)
        if m_month:
            month_token = m_month.group(1)
            month = MCFlashCandidatesClient._month_token_to_number(month_token)
            if month:
                now = datetime.now(timezone.utc)
                year = now.year
                if month < now.month:
                    year += 1
                dt = datetime(year, month, 1, tzinfo=timezone.utc)
                delta = (dt.date() - now.date()).days
                return max(0, delta)

        # e.g. "dal 7 gennaio"
        m_day_month = re.search(r"\b(?:da|dal|dalla)?\s*(\d{1,2})\s+([a-z]+)(?:\s+(\d{2,4}))?\b", text)
        if m_day_month:
            day = int(m_day_month.group(1))
            month = MCFlashCandidatesClient._month_token_to_number(m_day_month.group(2))
            if month:
                now = datetime.now(timezone.utc)
                year_raw = m_day_month.group(3)
                year = int(year_raw) if year_raw else None
                return MCFlashCandidatesClient._days_from_day_month(day, month, year, now=now)

        weekday_map = {
            "lunedi": 0,
            "luned\u00ec": 0,
            "luned": 0,
            "martedi": 1,
            "marted\u00ec": 1,
            "marted": 1,
            "mercoledi": 2,
            "mercoled\u00ec": 2,
            "mercoled": 2,
            "giovedi": 3,
            "gioved\u00ec": 3,
            "gioved": 3,
            "venerdi": 4,
            "venerd\u00ec": 4,
            "venerd": 4,
            "sabato": 5,
            "domenica": 6,
        }
        for token, day_idx in weekday_map.items():
            if token in text:
                now = datetime.now(timezone.utc)
                return MCFlashCandidatesClient._days_until_weekday(day_idx, now)

        return None

    @staticmethod
    def _parse_candidate_availability_days(raw: str) -> int | None:
        text = (raw or "").strip().lower()
        if not text:
            return None
        if "immediat" in text:
            return 0

        m_days = re.search(r"(\d+)\s*(?:gg|giorn[oi])", text)
        if m_days:
            return max(0, int(m_days.group(1)))

        # e.g. "dal 7 gennaio" / "7 gennaio" / "dal 7 gennaio 2024"
        # if the date is in the past, candidate is available now.
        m_day_month = re.search(r"\b(?:da|dal|dalla)?\s*(\d{1,2})\s+([a-z]+)(?:\s+(\d{2,4}))?\b", text)
        if m_day_month:
            day = int(m_day_month.group(1))
            month = MCFlashCandidatesClient._month_token_to_number(m_day_month.group(2))
            if month:
                now = datetime.now(timezone.utc)
                year_raw = m_day_month.group(3)
                year = int(year_raw) if year_raw else None
                return MCFlashCandidatesClient._days_from_day_month(day, month, year, now=now)

        # e.g. "da settembre" -> first day of that month in current year.
        m_month = re.search(r"\b(?:da|dal|dalla)\s+([a-z]+)\b", text)
        if m_month:
            month = MCFlashCandidatesClient._month_token_to_number(m_month.group(1))
            if month:
                now = datetime.now(timezone.utc)
                year = now.year
                dt = datetime(year, month, 1, tzinfo=timezone.utc)
                delta = (dt.date() - now.date()).days
                return max(0, delta)

        # e.g. "15/09" or "15/09/2026"
        m_date = re.search(r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?", text)
        if m_date:
            day = int(m_date.group(1))
            month = int(m_date.group(2))
            year_raw = m_date.group(3)
            now = datetime.now(timezone.utc)
            if year_raw is None:
                year = now.year
            else:
                year = int(year_raw)
                if year < 100:
                    year += 2000
            try:
                dt = datetime(year, month, day, tzinfo=timezone.utc)
            except Exception:
                return None
            delta = (dt.date() - now.date()).days
            return max(0, delta)

        return None

    @staticmethod
    def _availability_matches(requested: str, candidate_value: str) -> bool:
        req = (requested or "").strip().lower()
        cand = (candidate_value or "").strip().lower()
        if not req:
            return True
        if not cand:
            return False

        if req in cand:
            return True

        req_days = MCFlashCandidatesClient._parse_requested_availability_days(req)
        cand_days = MCFlashCandidatesClient._parse_candidate_availability_days(cand)
        if req_days is not None and cand_days is not None:
            return cand_days <= req_days
        if req_days is not None and cand_days is None:
            return False

        return False

    async def fetch_page(self, *, limit: int, offset: int) -> list[dict[str, Any]]:
        def _run() -> list[dict[str, Any]]:
            payload = self._request_json({"limit": limit, "offset": offset})
            if isinstance(payload, list):
                return [item for item in payload if isinstance(item, dict)]
            if isinstance(payload, dict):
                items = payload.get("items")
                if isinstance(items, list):
                    return [item for item in items if isinstance(item, dict)]
            raise MCFlashApiError("MCFlash API response format is not a JSON array")

        return await asyncio.to_thread(_run)

    async def fetch_candidates(
        self,
        *,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        # Endpoint supports max 1000 items per page.
        page_limit = max(1, min(limit, 1000))
        page_offset = max(0, offset)
        return await self.fetch_page(limit=page_limit, offset=page_offset)

    async def filter_candidates(
        self,
        *,
        limit: int,
        offset: int,
        query: str | None = None,
        role: str | None = None,
        seniority: str | None = None,
        language: str | None = None,
        sede: str | None = None,
        work_mode: str | None = None,
        lingue: str | None = None,
        budget: str | None = None,
        disponibilita: str | None = None,
        age: str | None = None,
        max_scan_records: int = 5000,
        return_all_matches: bool = False,
    ) -> list[dict[str, Any]]:
        requested_limit = max(1, int(limit))
        requested_offset = max(0, int(offset))

        query_l = (query or "").strip().lower()
        role_l = self._normalize_role_text(role or "")
        location_l = (sede or "").strip().lower()
        seniority_l = self._normalize_seniority_key(seniority or "")
        work_mode_l = (work_mode or "").strip().lower()
        language_l = (lingue or language or "").strip().lower()
        budget_l = (budget or "").strip().lower()
        disponibilita_l = (disponibilita or "").strip().lower()
        age_l = (age or "").strip().lower()
        age_min, age_max = self._extract_requested_age_bounds(age_l)

        budget_cap_numeric = self._extract_requested_budget_cap_numeric(budget_l)
        budget_cap_placeholder: float | None = None
        if budget_l and budget_cap_numeric is None and role_l and seniority_l:
            base_placeholder_cap = self._resolve_placeholder_budget_cap(
                role=role_l,
                seniority=seniority_l,
            )
            if base_placeholder_cap is not None:
                qualifier = self._classify_budget_qualifier(budget_l)
                if qualifier == "low":
                    budget_cap_placeholder = base_placeholder_cap * self._BUDGET_QUALIFIER_LOW_ADJUSTMENT
                elif qualifier == "high":
                    budget_cap_placeholder = base_placeholder_cap * self._BUDGET_QUALIFIER_HIGH_ADJUSTMENT
                else:
                    budget_cap_placeholder = base_placeholder_cap
        budget_cap = budget_cap_numeric if budget_cap_numeric is not None else budget_cap_placeholder

        def _candidate_matches(candidate: dict[str, Any]) -> bool:
            fields = (
                "Id",
                "Nome",
                "Nomi",
                "Ruolo",
                "Eta",
                "Seniority",
                "Sede",
                "Lingue",
                "site",
                "Disponibilita",
                "Budget",
            )
            text_blob = " ".join(self._pick(candidate, field) for field in fields).lower()

            if query_l and query_l not in text_blob:
                return False
            if role_l and not self._role_matches(role_l, self._pick(candidate, "Ruolo")):
                return False
            sede_value = self._pick(candidate, "Sede")
            location_match = self._location_matches_sede(location_l, sede_value)
            work_mode_match = (not work_mode_l) or self._work_mode_matches_sede(work_mode_l, sede_value)

            if location_l and work_mode_l:
                # If both are present, matching at least one is enough.
                if not (location_match or work_mode_match):
                    return False
            else:
                if not location_match:
                    return False
                if not work_mode_match:
                    return False
            candidate_seniority = self._normalize_seniority_key(self._pick(candidate, "Seniority"))
            if seniority_l and seniority_l != candidate_seniority:
                return False
            if language_l and not self._language_matches(language_l, self._pick(candidate, "Lingue")):
                return False
            if budget_cap is not None:
                candidate_budget = self._extract_candidate_budget_value(self._pick(candidate, "Budget"))
                if candidate_budget is None or candidate_budget > budget_cap:
                    return False
            if disponibilita_l and not self._availability_matches(disponibilita_l, self._pick(candidate, "Disponibilita")):
                return False
            if age_min is not None or age_max is not None:
                # Stesso criterio usato per budget/disponibilita': un vincolo hard
                # esplicito esclude anche i candidati con il dato mancante, invece
                # di farli passare per assenza di informazione.
                candidate_age = self._extract_candidate_age_value(self._pick(candidate, "Eta"))
                if candidate_age is None:
                    return False
                if age_min is not None and candidate_age < age_min:
                    return False
                if age_max is not None and candidate_age > age_max:
                    return False
            return True

        has_filters = any([
            query_l, role_l, location_l, work_mode_l, seniority_l, language_l, budget_l, disponibilita_l,
            age_min is not None, age_max is not None,
        ])
        page_size = 200
        cursor = 0
        scanned = 0
        matched: list[dict[str, Any]] = []

        while scanned < max_scan_records:
            page = await self.fetch_page(limit=page_size, offset=cursor)
            if not page:
                break

            scanned += len(page)
            cursor += len(page)

            if not has_filters:
                matched.extend(page)
                continue

            for candidate in page:
                if _candidate_matches(candidate):
                    matched.append(candidate)

        if return_all_matches:
            return matched

        return matched[requested_offset : requested_offset + requested_limit]

    async def find_candidates(self, key: str, *, max_scan_records: int = 20000) -> list[dict[str, Any]]:
        """
        Find every MCFlash record matching `key`.

        Id is a unique primary key, so a match on Id short-circuits and returns a
        single-item list immediately. A match on Nome/Nomi is not guaranteed unique
        (homonyms), so all matching records are collected across the full scan —
        callers that need to pick one among homonyms should disambiguate (e.g. by
        role) instead of blindly taking the first result.
        """
        key_l = (key or "").strip().lower()
        if not key_l:
            return []

        page_size = 200
        offset = 0
        scanned = 0
        name_matches: list[dict[str, Any]] = []

        while scanned < max_scan_records:
            page = await self.fetch_page(limit=page_size, offset=offset)
            if not page:
                break

            scanned += len(page)
            offset += len(page)

            for candidate in page:
                candidate_id = self._pick(candidate, "Id").strip().lower()
                if candidate_id and candidate_id == key_l:
                    return [candidate]

                for alias_field in ("Nome", "Nomi"):
                    alias_value = self._pick(candidate, alias_field).strip().lower()
                    if alias_value and alias_value == key_l:
                        name_matches.append(candidate)
                        break

        return name_matches

    async def find_candidate(self, key: str, *, max_scan_records: int = 20000) -> dict[str, Any] | None:
        """Backward-compatible single-result lookup: returns the first match, if any."""
        matches = await self.find_candidates(key, max_scan_records=max_scan_records)
        return matches[0] if matches else None

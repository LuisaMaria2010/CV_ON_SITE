"""
Mapper per la conversione da output LLM (RAW) a modelli dominio tipizzati.

Responsabilità:
- conversione tipi e parsing date
- pulizia minima di stringhe e liste
- nessuna business logic o deduplica avanzata

Tutte le funzioni sono pure e senza side effect.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Optional, Iterable

from core.schema import (
    LLMExtractionRaw,
    LLMExtractionMCFlashRaw,
    CVExtraction,
    WorkExperience,
    ExperienceEntry,
    EducationEntry,
    LanguageEntry,
    ProjectEntry,
    SkillEntry,
    TrainingEntry,
)
from domain.candidate_identity import split_full_name, trittico
from domain.mc_levels import normalize_language_level, normalize_skill_level
from domain.role_catalog import match_catalog_role

_TRITTICO_RE = re.compile(r"[A-Z]{3}")


# =========================================================
# Helpers
# =========================================================

def _clean(value: Optional[str]) -> Optional[str]:
    """
    Rimuove spazi iniziali/finali da una stringa e converte stringhe vuote in None.

    Args:
        value (Optional[str]): Stringa da pulire.

    Returns:
        Optional[str]: Stringa pulita o None se vuota.
    """
    if not value:
        return None
    value = value.strip()
    return value or None


def _parse_date(value: Optional[str]) -> Optional[date]:
    """
    Converte una stringa ISO (YYYY-MM-DD) in oggetto date.
    Restituisce None se la stringa non è valida o assente.

    Args:
        value (Optional[str]): Data in formato stringa.

    Returns:
        Optional[date]: Oggetto date o None.
    """
    if not value:
        return None
    value = value.strip()
    try:
        return date.fromisoformat(value)
    except ValueError:
        pass
    # I CV MC riportano spesso solo mese e anno: "2023-06" -> 2023-06-01.
    try:
        if len(value) == 7:
            return date.fromisoformat(f"{value}-01")
    except ValueError:
        pass
    return None


def _clean_skills(skills) -> list[SkillEntry]:
    """
    Pulisce una lista di SkillEntry:
    - rimuove elementi con nome vuoto
    - normalizza il nome in minuscolo
    - preserva il livello as-is

    Args:
        skills: Lista di SkillEntry (o dicts compatibili).

    Returns:
        list[SkillEntry]: Lista pulita.
    """
    result = []
    for s in skills:
        if isinstance(s, SkillEntry):
            name = (s.name or "").strip().lower()
            level = (s.level or "").strip() or None
        elif isinstance(s, dict):
            name = (s.get("name") or "").strip().lower()
            level = (s.get("level") or "").strip() or None
        else:
            name = str(s).strip().lower()
            level = None
        if name:
            result.append(SkillEntry(name=name, level=level))
    return result


def _clean_list(values: Iterable[str]) -> list[str]:
    """
    Pulisce una lista generica di stringhe:
    - rimuove spazi
    - elimina elementi vuoti

    Args:
        values (Iterable[str]): Lista di stringhe da pulire.

    Returns:
        list[str]: Lista pulita.
    """
    return [value.strip() for value in values if value and value.strip()]


def _clean_multiline(value: Optional[str]) -> Optional[str]:
    """Come _clean ma conserva gli a capo interni (descrizioni a punti)."""
    if not value:
        return None
    lines = [line.strip() for line in value.strip().splitlines()]
    return "\n".join(line for line in lines if line) or None


def _period_fields(start_raw: Optional[str], end_raw: Optional[str], ongoing: Optional[bool]) -> dict:
    """Date di un periodo; senza data di fine (e senza fine scritta) il periodo e' in corso."""
    start, end = _parse_date(start_raw), _parse_date(end_raw)
    return {
        "start_date": start,
        "end_date": end,
        "ongoing": bool(ongoing) or (start is not None and end is None and not end_raw),
    }


# Parole che l'LLM a volte mette tra le "Tecnologie utilizzate" ma non sono strumenti.
_GENERIC_TECH = frozenset({
    "ai", "ia", "llm", "llms", "ml", "machine learning", "deep learning", "artificial intelligence",
    "intelligenza artificiale", "frontend", "front-end", "backend", "back-end", "api", "apis", "rest",
    "cloud", "database", "databases", "db", "web", "software", "data analysis", "analisi dati",
})


def _technology_list(items: Iterable[str], casing: dict[str, str]) -> list[str]:
    """Tecnologie pulite: niente termini generici, maiuscole prese dalle skill del CV se note."""
    out: list[str] = []
    seen: set[str] = set()
    for item in _clean_list(items):
        key = item.casefold()
        if key in _GENERIC_TECH or key in seen:
            continue
        seen.add(key)
        out.append(casing.get(key, item))
    return out


def _mcflash_fields(raw: LLMExtractionMCFlashRaw) -> dict:
    """Campi del profilo mcflash: esperienze con progetti, formazione, training, lingue, nome/cognome."""
    casing = {(s.name or "").strip().casefold(): s.name.strip() for s in raw.cv_skills if s.name and s.name.strip()}

    experiences = []
    for e in raw.experiences:
        projects = [
            ProjectEntry(
                **_period_fields(p.start_date, p.end_date, p.ongoing),
                name=_clean(p.name),
                role=_clean(p.role),
                activities=_clean_multiline(p.activities),
                technologies=_technology_list(p.technologies, casing),
            )
            for p in e.projects
            if _clean(p.name) or _clean(p.activities)
        ]
        entry = ExperienceEntry(
            **_period_fields(e.start_date, e.end_date, e.ongoing),
            company=_clean(e.company),
            role=_clean(e.role),
            description=_clean_multiline(e.description),
            technologies=_technology_list(e.technologies, casing),
            projects=projects,
        )
        if any([entry.start_date, entry.end_date, entry.company, entry.role, entry.description, entry.projects]):
            experiences.append(entry)

    education = [
        EducationEntry(year=_clean(e.year), title=_clean(e.title), institution=_clean(e.institution))
        for e in raw.education
        if _clean(e.title) or _clean(e.institution)
    ]

    trainings = [
        TrainingEntry(year=_clean(t.year), title=title, provider=_clean(t.provider))
        for t in raw.trainings
        if (title := _clean(t.title))
    ]

    languages = [
        LanguageEntry(lang=lang, level=normalize_language_level(l.level))
        for l in raw.languages
        if (lang := _clean(l.lang))
    ]

    # Skill per il CV: maiuscole e ordine originali, livelli nella scala MC, niente duplicati.
    cv_skills: list[SkillEntry] = []
    seen_skills: set[str] = set()
    for s in raw.cv_skills:
        name = _clean(s.name)
        if name and name.casefold() not in seen_skills:
            seen_skills.add(name.casefold())
            cv_skills.append(SkillEntry(name=name, level=normalize_skill_level(s.level)))

    full_name = _clean(raw.name)
    first, last = _clean(raw.first_name), _clean(raw.last_name)

    # CV gia' nel modello MC: al posto del nome c'e' il trittico ("Nome  DCA").
    # Un nome vero di tre lettere ("UGO" + "ROSSI") ha il cognome: non e' un trittico.
    anonymized = None
    if not last:
        anonymized = next(
            (v for v in (full_name, first) if v and _TRITTICO_RE.fullmatch(v)), None
        )
    if anonymized:
        first, last, tri = None, None, anonymized
    else:
        if not (first and last):
            guess_first, guess_last = split_full_name(full_name)
            first, last = first or guess_first, last or guess_last
        tri = trittico(first, last)

    # Ruolo dal catalogo MC; se l'LLM ne scrive uno fuori elenco si prova a ricondurlo.
    catalog_role = match_catalog_role(raw.catalog_role) or match_catalog_role(raw.role)

    return {
        "first_name": first,
        "last_name": last,
        "catalog_role": catalog_role,
        "experiences": experiences,
        "education": education,
        "trainings": trainings,
        "languages": languages,
        "cv_skills": cv_skills,
        "trittico": tri,
    }


# =========================================================
# Public API
# =========================================================

def to_domain(raw: LLMExtractionRaw) -> CVExtraction:
    """
    Converte l'output LLM (LLMExtractionRaw) in un modello dominio tipizzato (CVExtraction).

    Esegue solo:
    - pulizia stringhe
    - parsing date
    - pulizia minima delle liste

    Args:
        raw (LLMExtractionRaw): Output grezzo prodotto dall'LLM.

    Returns:
        CVExtraction: Modello dominio pronto per business logic o API.
    """

    work_experiences = [
        WorkExperience(
            start_date=_parse_date(e.start_date),
            end_date=_parse_date(e.end_date),
        )
        for e in raw.employment_dates
        if e.start_date or e.end_date
    ]

    extra: dict = {}
    if isinstance(raw, LLMExtractionMCFlashRaw):
        extra = _mcflash_fields(raw)
        # Nel profilo mcflash le date stanno nelle esperienze: servono comunque
        # a calcolare anni di esperienza e seniority.
        if not work_experiences:
            work_experiences = [
                WorkExperience(start_date=e.start_date, end_date=e.end_date)
                for e in extra["experiences"]
                if e.start_date or e.end_date
            ]

    return CVExtraction(
        full_name=_clean(raw.name),
        role=_clean(raw.role),
        location=_clean(raw.location),
        email=_clean(raw.email),
        phone=_clean(raw.phone),
        language=_clean(raw.language),
        birth_date=_parse_date(raw.birth_date),
        skills=_clean_skills(raw.skills),
        education_titles=_clean_list(raw.education_titles),
        certifications=_clean_list(raw.certifications),
        employment_dates=work_experiences,
        **extra,
    )

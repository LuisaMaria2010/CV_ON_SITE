"""
Schemi di dominio e modelli dati per FlashCV.

Questo modulo definisce:
1) LLMExtractionRaw  → output diretto LLM (solo stringhe, LLM-friendly)
2) CVExtraction      → modello domain tipizzato e arricchito (business-ready)
3) ExtractHttpResponse → risposta API finale per endpoint

Regola fondamentale:
    LLM = solo estrazione testo
    Python = parsing date, calcoli, normalizzazioni
"""

from __future__ import annotations

from datetime import date
from typing import List, Optional

from pydantic import BaseModel, Field, EmailStr, ConfigDict


# =========================================================
# 0️⃣ SHARED VALUE OBJECTS
# =========================================================

class SkillEntry(BaseModel):
    """A technical skill with an optional proficiency level."""
    name: str = Field(..., description="Technical skill name (lowercase, normalized)")
    level: Optional[str] = Field(
        default=None,
        description="Proficiency level as written in the CV (e.g. 'Buona conoscenza', 'Expert', 'B2')"
    )

    model_config = ConfigDict(extra="ignore")


class LanguageEntry(BaseModel):
    lang: str = Field(..., description="Language name or code, e.g. italiano, english, it, en")
    level: Optional[str] = Field(default=None, description="Proficiency level, e.g. madrelingua, B2, C1")

    model_config = ConfigDict(extra="ignore")


# =========================================================
# 1️⃣ RAW LLM OUTPUT  (LLM-friendly, solo stringhe)
# =========================================================

class WorkExperienceRaw(BaseModel):
    """
    Periodo lavorativo grezzo prodotto dall’LLM.
    Tutti i campi sono stringhe per massima compatibilità e robustezza del parsing LLM.
    """

    start_date: Optional[str] = Field(
        default=None,
        description="Start date of the employment in YYYY-MM-DD format. Example: 2020-05-01"
    )

    end_date: Optional[str] = Field(
        default=None,
        description="End date of the employment in YYYY-MM-DD format. Use null if still ongoing"
    )

    model_config = ConfigDict(extra="ignore")


class LLMExtractionRaw(BaseModel):
    """
    Schema generato direttamente dall’LLM.

    Tutti i campi sono stringhe o liste semplici, senza calcoli o tipi complessi.
    Serve come output intermedio, facilmente serializzabile e robusto.
    """

    name: Optional[str] = Field(
        default=None,
        description="Full name of the candidate exactly as written in the CV"
    )

    role: Optional[str] = Field(
        default=None,
        description="Current or desired professional role or job title (e.g. Software Engineer, Data Analyst)"
    )

    location: Optional[str] = Field(
        default=None,
        description="City and/or country where the candidate lives or works. Example: 'Milan, Italy', 'Rome', or 'Remote'"
    )

    email: Optional[str] = Field(
        default=None,
        description="Primary email address of the candidate"
    )

    phone: Optional[str] = Field(
        default=None,
        description="Phone number including international prefix if available"
    )

    language: Optional[str] = Field(
        default=None,
        description="Main language of the CV content (e.g. Italian, English, Spanish)"
    )

    birth_date: Optional[str] = Field(
        default=None,
        description="Birth date of the candidate in YYYY-MM-DD format if explicitly mentioned"
    )

    skills: List[SkillEntry] = Field(
        default_factory=list,
        description=(
            "STRICTLY technical hard skills only. Each entry must be an object with 'name' (the technology, "
            "programming language, framework, cloud service, database or software tool) and 'level' "
            "(proficiency as written in the CV, e.g. 'Buona conoscenza', 'Expert', 'Conoscenza base'; "
            "null if not specified). Soft skills are forbidden."
        )
    )

    education_titles: List[str] = Field(
        default_factory=list,
        description=(
            "List of academic titles explicitly mentioned in the CV, such as bachelor's degree, master's degree, "
            "PhD, or equivalent titles"
        )
    )

    certifications: List[str] = Field(
        default_factory=list,
        description="List of professional certifications explicitly mentioned in the CV"
    )

    employment_dates: List[WorkExperienceRaw] = Field(
        default_factory=list,
        description="List of employment periods detected in the CV with start and end dates"
    )

    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------
# Profilo "mcflash": dettaglio in piu' per compilare il CV nel modello MC.
# Usato solo da POST /api/extract?profile=mcflash; l'ingestione resta sullo
# schema base, cosi' indice e costi non cambiano.
# ---------------------------------------------------------

class ProjectEntryRaw(BaseModel):
    """Progetto/commessa/cliente dentro un singolo posto di lavoro."""

    start_date: Optional[str] = Field(
        default=None,
        description="Project start in YYYY-MM-DD; day 01 when only month and year are given"
    )
    end_date: Optional[str] = Field(
        default=None,
        description="Project end in YYYY-MM-DD; null if ongoing"
    )
    ongoing: Optional[bool] = Field(default=None, description="true if the project is ongoing")
    name: Optional[str] = Field(
        default=None,
        description="Project or client name as written (e.g. 'Progetto Angelini', 'Cliente Mediobanca')"
    )
    role: Optional[str] = Field(
        default=None,
        description="Role in this project only if different from the role of the job"
    )
    activities: Optional[str] = Field(
        default=None,
        description="Activities of this project as written in the CV, without the 'Attività:' label"
    )
    technologies: List[str] = Field(
        default_factory=list,
        description="Technologies used in this project (e.g. from 'Tecnologie utilizzate:')"
    )

    model_config = ConfigDict(extra="ignore")


class TrainingEntryRaw(BaseModel):
    """Corso o certificazione con anno ed ente."""

    year: Optional[str] = Field(default=None, description="Year (YYYY) or period as written")
    title: Optional[str] = Field(default=None, description="Course or certification title")
    provider: Optional[str] = Field(default=None, description="Provider or school (e.g. 'Udemy', 'Microsoft')")

    model_config = ConfigDict(extra="ignore")


class ExperienceEntryRaw(BaseModel):
    """Esperienza lavorativa completa prodotta dall'LLM."""

    start_date: Optional[str] = Field(
        default=None,
        description="Start date in YYYY-MM-DD format; use day 01 when the CV gives only month and year"
    )
    end_date: Optional[str] = Field(
        default=None,
        description="End date in YYYY-MM-DD format; null if the job is ongoing"
    )
    ongoing: Optional[bool] = Field(
        default=None,
        description="true if the job is ongoing (e.g. 'ad oggi', 'present', 'in corso')"
    )
    company: Optional[str] = Field(
        default=None,
        description="Employer or client company as written in the CV (e.g. 'Lutech', 'Riservato')"
    )
    role: Optional[str] = Field(
        default=None,
        description="Job title held in this experience"
    )
    description: Optional[str] = Field(
        default=None,
        description=(
            "Activities of this job as written (no invention). When the job is split into projects, "
            "only the general activities not belonging to a single project (often null)"
        )
    )
    technologies: List[str] = Field(
        default_factory=list,
        description="Technologies used in this job outside the single projects"
    )
    projects: List[ProjectEntryRaw] = Field(
        default_factory=list,
        description="Projects/clients/assignments within this job, most recent first; empty if not distinguished"
    )

    model_config = ConfigDict(extra="ignore")


class EducationEntryRaw(BaseModel):
    """Titolo di studio con anno e istituto."""

    year: Optional[str] = Field(
        default=None,
        description="Year the title was obtained (YYYY), or the period as written if no single year"
    )
    title: Optional[str] = Field(
        default=None,
        description="Full title including level and field (e.g. 'Laurea Magistrale in Statistica')"
    )
    institution: Optional[str] = Field(
        default=None,
        description="School or university"
    )

    model_config = ConfigDict(extra="ignore")


class LLMExtractionMCFlashRaw(LLMExtractionRaw):
    """Schema base + campi necessari al modello CV MCFlash."""

    first_name: Optional[str] = Field(
        default=None,
        description="Candidate first name(s) only, without surname"
    )
    last_name: Optional[str] = Field(
        default=None,
        description="Candidate surname only, including particles such as 'De', 'Di', 'La'"
    )
    catalog_role: Optional[str] = Field(
        default=None,
        description="Exactly one role copied verbatim from the MC ROLE CATALOG in the instructions"
    )
    experiences: List[ExperienceEntryRaw] = Field(
        default_factory=list,
        description="All work experiences, most recent first, with dates, company, role and description"
    )
    education: List[EducationEntryRaw] = Field(
        default_factory=list,
        description="All academic titles with year and institution"
    )
    languages: List[LanguageEntry] = Field(
        default_factory=list,
        description="Spoken languages with level as written in the CV (e.g. Inglese / Intermedio)"
    )
    cv_skills: List[SkillEntry] = Field(
        default_factory=list,
        description=(
            "Same technical skills as 'skills' but with names in the ORIGINAL casing as written in the CV "
            "(e.g. 'Power BI', 'PostgreSQL') and in the CV order"
        )
    )
    trainings: List[TrainingEntryRaw] = Field(
        default_factory=list,
        description="Courses and certifications with year and provider"
    )


# =========================================================
# 2️⃣ DOMAIN MODEL  (tipizzato + business logic)
# =========================================================

class WorkExperience(BaseModel):
    """
    Periodo lavorativo convertito in oggetti date reali.
    Usato nel modello domain per validazione e calcoli temporali.
    """

    start_date: Optional[date] = Field(
        default=None,
        description="Parsed start date as Python date object"
    )

    end_date: Optional[date] = Field(
        default=None,
        description="Parsed end date as Python date object"
    )

    model_config = ConfigDict(extra="ignore")


class ProjectEntry(BaseModel):
    """Progetto dentro un posto di lavoro (profilo mcflash)."""

    start_date: Optional[date] = None
    end_date: Optional[date] = None
    ongoing: bool = False
    name: Optional[str] = None
    role: Optional[str] = None
    activities: Optional[str] = None
    technologies: List[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")


class TrainingEntry(BaseModel):
    """Corso o certificazione (profilo mcflash)."""

    year: Optional[str] = None
    title: Optional[str] = None
    provider: Optional[str] = None

    model_config = ConfigDict(extra="ignore")


class ExperienceEntry(BaseModel):
    """Esperienza lavorativa completa (profilo mcflash)."""

    start_date: Optional[date] = None
    end_date: Optional[date] = None
    ongoing: bool = False
    company: Optional[str] = None
    role: Optional[str] = None
    description: Optional[str] = None
    technologies: List[str] = Field(default_factory=list)
    projects: List[ProjectEntry] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")


class EducationEntry(BaseModel):
    """Titolo di studio con anno e istituto (profilo mcflash)."""

    year: Optional[str] = None
    title: Optional[str] = None
    institution: Optional[str] = None

    model_config = ConfigDict(extra="ignore")


class CVExtraction(BaseModel):
    """
    Modello finale usato dall'applicazione e per la risposta API.

    Caratteristiche:
    - tipizzato e validato
    - pronto per DB/API
    - indipendente dall’LLM
    - contiene anche campi calcolati (età, anni esperienza, seniority)
    """

    full_name: Optional[str] = Field(
        default=None,
        description="Candidate full name"
    )

    role: Optional[str] = Field(
        default=None,
        description="Primary professional role"
    )

    location: Optional[str] = Field(
        default=None,
        description="Normalized work or residence location of the candidate"
    )

    email: Optional[EmailStr] = Field(
        default=None,
        description="Validated email address"
    )

    phone: Optional[str] = Field(
        default=None,
        description="Phone number"
    )

    language: Optional[str] = Field(
        default=None,
        description="Language of the CV"
    )

    birth_date: Optional[date] = Field(
        default=None,
        description="Birth date parsed as Python date object"
    )

    skills: List[SkillEntry] = Field(
        default_factory=list,
        description="Deduplicated list of tech skills with optional proficiency level"
    )

    education_titles: List[str] = Field(
        default_factory=list,
        description="List of academic titles extracted from the CV"
    )

    certifications: List[str] = Field(
        default_factory=list,
        description="List of professional certifications extracted from the CV"
    )

    employment_dates: List[WorkExperience] = Field(
        default_factory=list,
        description="List of employment periods converted to date objects"
    )

    # ---------------------------
    # profilo mcflash (vuoti nel profilo base)
    # ---------------------------

    first_name: Optional[str] = Field(default=None, description="Candidate first name(s)")
    last_name: Optional[str] = Field(default=None, description="Candidate surname")
    catalog_role: Optional[str] = Field(
        default=None,
        description="Role from the MC role catalog (infra/mcflash_budget_placeholders.json), shown in the MC CV"
    )
    experiences: List[ExperienceEntry] = Field(
        default_factory=list,
        description="Work experiences with company, role and description, most recent first"
    )
    education: List[EducationEntry] = Field(
        default_factory=list,
        description="Academic titles with year and institution"
    )
    languages: List[LanguageEntry] = Field(
        default_factory=list,
        description="Spoken languages with level"
    )
    cv_skills: List[SkillEntry] = Field(
        default_factory=list,
        description="Technical skills as written in the CV (original casing and order), levels on the MC scale"
    )
    trainings: List[TrainingEntry] = Field(
        default_factory=list,
        description="Courses and certifications with year and provider"
    )

    # ---------------------------
    # campi calcolati (NO LLM)
    # ---------------------------

    trittico: Optional[str] = Field(
        default=None,
        description="MC anonymized code: first letter of first name + first two letters of surname"
    )

    age: Optional[int] = Field(
        default=None,
        description="Computed age derived from birth_date"
    )

    experience_years: Optional[float] = Field(
        default=None,
        description="Total years of experience computed from employment dates"
    )

    seniority: Optional[str] = Field(
        default=None,
        description="Derived seniority level based on experience years (junior, mid, senior)"
    )

    model_config = ConfigDict(
        extra="ignore",
        str_strip_whitespace=True
    )

    # ---------------------------
    # helpers business
    # ---------------------------

    def normalize_skills(self) -> None:
        """
        Normalizza la lista delle skills:
        - converte il nome in minuscolo
        - rimuove spazi
        - elimina duplicati per nome (mantiene primo livello trovato)
        """
        seen: set[str] = set()
        normalized: list[SkillEntry] = []
        for s in self.skills:
            key = (s.name or "").lower().strip()
            if key and key not in seen:
                seen.add(key)
                normalized.append(SkillEntry(name=key, level=s.level or None))
        self.skills = sorted(normalized, key=lambda x: x.name)


# =========================================================
# Normalized front-matter / metadata models (Phase A)
# =========================================================


class CandidateInfo(BaseModel):
    full_name: Optional[str] = Field(default=None, description="Candidate full name")
    role: Optional[str] = Field(default=None, description="Primary professional role")
    location: Optional[str] = Field(default=None, description="Work or residence location")
    email: Optional[str] = Field(default=None, description="Primary email address")
    phone: Optional[str] = Field(default=None, description="Phone number")
    birth_date: Optional[str] = Field(default=None, description="Birth date in ISO format")
    age: Optional[int] = Field(default=None, description="Computed age if available")
    availability: Optional[str] = Field(default=None, description="Availability notes, e.g. immediate, 30 days")
    seniority: Optional[str] = Field(default=None, description="Derived seniority: junior/mid/senior")

    model_config = ConfigDict(extra="ignore")


class ImageRef(BaseModel):
    blob_path: str = Field(..., description="Blob path where the extracted image is stored (container/name)")
    description: Optional[str] = Field(default=None, description="Optional caption/description (AI-generated)")

    model_config = ConfigDict(extra="ignore")


class NormalizedCVMetadata(BaseModel):
    document_id: str = Field(..., description="Stable document identifier used by registry and indexer")
    source_paths: List[str] = Field(default_factory=list, description="List of source paths / aliases that reference the same content hash")
    version: int = Field(1, description="Version integer incremented on content change")
    hash: str = Field(..., description="Content hash (sha256)")
    processed_at: str = Field(..., description="ISO datetime when processing finished")
    language: Optional[str] = Field(default=None, description="Detected language of the CV")
    candidate: CandidateInfo = Field(default_factory=CandidateInfo, description="Candidate basic info")
    skills: List[SkillEntry] = Field(default_factory=list, description="Normalized list of skills with optional proficiency level")
    certifications: List[str] = Field(default_factory=list, description="Certifications list")
    education_titles: List[str] = Field(default_factory=list, description="Education titles list")
    languages_spoken: List[LanguageEntry] = Field(default_factory=list, description="Languages spoken with levels")
    experience_years: Optional[float] = Field(default=None, description="Computed total experience years")
    employment_dates: List[WorkExperience] = Field(default_factory=list, description="Employment periods converted to dates")
    images: List[ImageRef] = Field(default_factory=list, description="References to extracted images")
    element_count: int = Field(0, description="Number of document elements in markdown")
    image_count: int = Field(0, description="Number of images extracted")
    metadata: dict = Field(default_factory=dict, description="Free-form additional metadata")

    model_config = ConfigDict(extra="ignore")


# =========================================================
# 3️⃣ HTTP RESPONSE WRAPPER
# =========================================================

class ExtractHttpResponse(BaseModel):
    """
    Modello della risposta finale dell'endpoint di estrazione.
    Incapsula il risultato arricchito, info prompt e modello LLM usato.
    """

    data: CVExtraction = Field(
        description="Final enriched extraction result"
    )

    prompt_id: str = Field(
        description="Identifier of the prompt used"
    )

    prompt_version: str = Field(
        description="Version of the prompt used"
    )

    prompt_hash: str = Field(
        description="Hash of the prompt for reproducibility"
    )

    model: str = Field(
        description="LLM model name used for the extraction (e.g. gpt-4o-mini)"
    )

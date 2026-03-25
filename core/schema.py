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

    skills: List[str] = Field(
        default_factory=list,
        description=(
        "STRICTLY technical hard skills only. "
        "Must be technologies, programming languages, frameworks, cloud services, databases or software tools. "
        "Soft skills are forbidden and must not appear."
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

    skills: List[str] = Field(
        default_factory=list,
        description="Normalized and deduplicated list of tech skills"
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
    # campi calcolati (NO LLM)
    # ---------------------------

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
        description="Derived seniority level based on experience years (junior, mid, senior, lead, principal)"
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
        - converte in minuscolo
        - rimuove spazi
        - elimina duplicati
        """
        self.skills = sorted({s.lower().strip() for s in self.skills if s})


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

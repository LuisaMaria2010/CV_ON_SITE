"""Profilo di estrazione "mcflash": mapper, prompt e separazione dal profilo base."""
from __future__ import annotations

from datetime import date

from langchain.prompts import PromptTemplate

from core.config import settings
from core.llm_chain import _load_prompt
from core.schema import LLMExtractionMCFlashRaw, LLMExtractionRaw
from db_data.mapper import to_domain
from db_data.postprocess import enrich


def _raw_mcflash(**overrides) -> LLMExtractionMCFlashRaw:
    data = {
        "name": "Davide Carbone",
        "first_name": "Davide",
        "last_name": "Carbone",
        "role": "Business Intelligence Developer",
        "location": "Roma",
        "email": "davide@example.com",
        "phone": "+39 333 0000000",
        "birth_date": "1990-09-01",
        "experiences": [
            {"start_date": "2023-06-01", "end_date": None, "ongoing": True, "company": "Riservato",
             "role": "BI Developer", "description": "Report Power BI\n  Migrazione Qlik  \n"},
            {"start_date": "2021-12", "end_date": "2023-06", "company": "Lutech", "role": "Data Analyst"},
            {"company": None, "role": None, "description": None},
        ],
        "education": [
            {"year": "2021", "title": "Laurea Magistrale in Statistica", "institution": "Università della Calabria"},
            {"year": "2015", "title": None, "institution": None},
        ],
        "languages": [{"lang": "Inglese", "level": "Intermedio"}, {"lang": " ", "level": "B2"}],
        "skills": [{"name": "power bi", "level": "Conoscenza base"}],
        "cv_skills": [
            {"name": "Power BI", "level": "Conoscenza base"},
            {"name": "power bi", "level": "Buona conoscenza"},
            {"name": "PostgreSQL", "level": None},
        ],
    }
    data.update(overrides)
    return LLMExtractionMCFlashRaw(**data)


def test_mapper_mcflash_fields():
    cv = to_domain(_raw_mcflash())

    assert cv.trittico == "DCA"
    assert (cv.first_name, cv.last_name) == ("Davide", "Carbone")

    assert len(cv.experiences) == 2  # la voce vuota viene scartata
    first, second = cv.experiences
    assert first.ongoing is True and first.end_date is None
    assert first.company == "Riservato"
    assert first.description == "Report Power BI\nMigrazione Qlik"
    assert second.start_date == date(2021, 12, 1) and second.end_date == date(2023, 6, 1)
    assert second.ongoing is False

    assert [e.title for e in cv.education] == ["Laurea Magistrale in Statistica"]
    assert [(l.lang, l.level) for l in cv.languages] == [("Inglese", "Intermedio")]
    # Per il CV: maiuscole e ordine originali, senza duplicati; per l'indice resta minuscolo.
    assert [(s.name, s.level) for s in cv.cv_skills] == [("Power BI", "Conoscenza base"), ("PostgreSQL", None)]
    assert [s.name for s in cv.skills] == ["power bi"]


def test_mapper_mcflash_projects_trainings_and_levels():
    cv = to_domain(_raw_mcflash(
        experiences=[{
            "start_date": "2023-09", "ongoing": True, "company": "MC Engineering", "role": "Data Scientist",
            "projects": [
                {"start_date": "2024-09-01", "name": "Progetto Angelini", "activities": "Chatbot ASKAI",
                 "technologies": ["Azure OpenAI", " ", "LLM", "langchain", "Python", "machine learning"]},
                {"name": None, "activities": None},  # vuoto: scartato
            ],
        }],
        trainings=[{"year": "2024", "title": "Tableau", "provider": "Udemy"}, {"title": " "}],
        languages=[{"lang": "Italiano", "level": "LINGUA MADRE"}, {"lang": "Inglese", "level": "C2"}],
        cv_skills=[{"name": "LangChain", "level": "Advanced"}, {"name": "SQL", "level": None}],
    ))
    (job,) = cv.experiences
    (project,) = job.projects
    assert project.ongoing is True and project.start_date == date(2024, 9, 1)
    # Termini generici tolti, maiuscole riprese dalle skill del CV ("langchain" -> "LangChain").
    assert project.technologies == ["Azure OpenAI", "LangChain", "Python"]
    assert [(t.year, t.title, t.provider) for t in cv.trainings] == [("2024", "Tableau", "Udemy")]
    assert [(l.lang, l.level) for l in cv.languages] == [("Italiano", "Madrelingua"), ("Inglese", "Avanzato")]
    assert [(s.name, s.level) for s in cv.cv_skills] == [("LangChain", "Ottima conoscenza"), ("SQL", None)]


def test_mapper_mcflash_derives_employment_dates_for_seniority():
    cv = enrich(to_domain(_raw_mcflash()))
    assert len(cv.employment_dates) == 2
    assert cv.experience_years and cv.experience_years > 3
    assert cv.age is not None


def test_mapper_mcflash_falls_back_to_full_name_split():
    cv = to_domain(_raw_mcflash(first_name=None, last_name=None, name="Mario De Luca"))
    assert (cv.first_name, cv.last_name, cv.trittico) == ("Mario", "De Luca", "MDE")


def test_mapper_mcflash_reads_trittico_from_mc_cv():
    # CV gia' nel modello MC: "Nome DCA" (l'LLM a volte lo mette anche in first_name).
    cv = to_domain(_raw_mcflash(name="DCA", first_name="DCA", last_name=None))
    assert (cv.first_name, cv.last_name, cv.trittico) == (None, None, "DCA")


def test_mapper_mcflash_three_letter_real_name_is_not_trittico():
    cv = to_domain(_raw_mcflash(name="UGO ROSSI", first_name="UGO", last_name="ROSSI"))
    assert (cv.first_name, cv.last_name, cv.trittico) == ("UGO", "ROSSI", "URO")


def test_mapper_base_profile_unchanged():
    cv = to_domain(LLMExtractionRaw(name="Mario Rossi", employment_dates=[{"start_date": "2020-01-01"}]))
    assert cv.trittico is None
    assert cv.experiences == [] and cv.education == [] and cv.languages == []
    assert len(cv.employment_dates) == 1


def test_mcflash_prompt_composes_and_formats():
    base = _load_prompt()
    mc = _load_prompt(settings.prompt_mcflash_addendum_file)
    assert mc != base
    # Le istruzioni del profilo stanno prima del testo del CV.
    assert mc.index("MCFLASH PROFILE") < mc.index("Resume text:")
    # Nessuna graffa singola che rompa il PromptTemplate.
    rendered = PromptTemplate(template=mc, input_variables=["content", "format_instructions"]).format(
        content="CV TEXT", format_instructions="FMT"
    )
    assert "CV TEXT" in rendered and "FMT" in rendered

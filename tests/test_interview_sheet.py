"""Tests della scheda colloquio del recruiter (domain/interview_sheet.py, POST /api/extract/scheda).

La scheda e le persone sono inventate: niente dati di candidati reali nei test.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json

import azure.functions as func

import functions.cv_extract as api
from domain.interview_sheet import merge_sheet, parse_languages, parse_sheet, parse_skills, split_sections

SHEET = """TARIFFA: 325

NOME/COGNOME/TRIGRAMMA: STEFANO PRANDI - SPR
RUOLO: Data Engineer
ETA’: 31
SENIORITY: Medium
STANDARD MC: SI
CITTÀ: Milano

RECRUITER: TST
DATA COLLOQUIO: 20/10/2026
CATEGORIA PROTETTA:  no
EVENTUALE SPECIFICA CITTADINANZA/PERMESSO DI SOGGIORNO: cittadino italiano
TITOLO DI STUDIO+VOTAZIONE:
Laurea Magistrale in Ingegneria Meccanica, voto: 104/110  anno: 2021
Master in Big Data Analytics & Artificial Intelligence,  anno: 2025
ESPERIENZA LAVORATIVA ATTUALE:
A luglio 2025 inizia a lavorare come Data Engineer presso Datavia Consulting. Ruolo: consulente.
SKILL: AWS (S3, Athena, Step Functions, Lambda, AppFlow), Terraform, Microsoft Fabric Data, dbt, SQL, Git Python, Power BI
LINGUE: inglese livello avanzato.
SEDE + DISPONIBILITA' TRASFERTE/TRASFERIMENTI: Milano, anche ibrido.
ULTIMO CONTRATTO: CTD Apprendistato presso Datavia Consulting S.p.A. da luglio 2025
CCNL: Commercio
PRECEDENTI CONTRATTI TI: SI
PREAVVISO: 15 giorni dal 1 o dal 15 del mese
ASPETTATIVA ECONOMICA E PROFESSIONALE:
Cerca progetti cloud.
RAL attesa 30/32.000 €.
IMPRESSIONI:
Profilo inventato per i test.
"""

# Senza esperienze ne' titoli di studio descritti: con il CV non serve l'LLM.
SHORT_SHEET = "TARIFFA: 300\nNOME/COGNOME/TRIGRAMMA: STEFANO PRANDI - SPR\nSENIORITY: Junior\nIMPRESSIONI: ok"

TODAY = dt.date(2026, 10, 21)


def test_sections_follow_labels_and_continuation_lines():
    s = split_sections(SHEET)
    assert s["tariffa"] == "325" and s["eta"] == "31" and s["citta"] == "Milano"
    assert s["titolo_studio"].startswith("Laurea Magistrale") and "Master in Big Data" in s["titolo_studio"]
    # "Ruolo: consulente." dentro il testo non e' un'etichetta (non e' in maiuscolo)
    assert "Ruolo: consulente" in s["esperienza"] and s["ruolo"] == "Data Engineer"
    assert s["aspettativa"].endswith("RAL attesa 30/32.000 €.")


def test_parse_sheet_fields():
    out = parse_sheet(SHEET)
    assert (out["first_name"], out["last_name"], out["full_name"], out["trittico"]) == \
        ("Stefano", "Prandi", "Stefano Prandi", "SPR")
    assert (out["role"], out["age"], out["location"]) == ("Data Engineer", 31, "Milano")
    assert out["mcflash"] == {"budget": 325, "seniority": "Medium", "disponibilita": "15 gg"}
    assert out["languages"] == [{"lang": "inglese", "level": "Avanzato"}]
    assert out["cv_skills"][0] == {"name": "AWS (S3, Athena, Step Functions, Lambda, AppFlow)", "level": None}
    assert [s["name"] for s in out["cv_skills"]][-2:] == ["Git Python", "Power BI"]
    c = out["colloquio"]
    assert (c["recruiter"], c["data_colloquio"], c["ccnl"], c["ral_attesa"]) == ("TST", "20/10/2026", "Commercio", "30/32.000 €")
    assert c["impressioni"] == "Profilo inventato per i test."
    assert c["testo"].startswith("TARIFFA: 325")


def test_languages_and_skills_helpers():
    assert parse_languages("Inglese B2, francese madrelingua; tedesco") == [
        {"lang": "inglese", "level": "Intermedio"}, {"lang": "francese", "level": "Madrelingua"},
        {"lang": "tedesco", "level": None}]
    assert [s["name"] for s in parse_skills("SQL, sql, Python.")] == ["SQL", "Python"]


def test_merge_sheet_wins_on_identity_and_mixes_sections():
    cv = {"first_name": "SPR", "last_name": None, "trittico": None, "role": "Ingegnere", "age": None,
          "location": "Pisa", "cv_skills": [{"name": "SQL", "level": "Ottima conoscenza"}],
          "languages": [{"lang": "inglese", "level": "Base"}, {"lang": "spagnolo", "level": "Base"}],
          "experiences": [{"company": "Datavia Consulting", "role": "Data Engineer"}], "education": []}
    from_text = {"education": [{"year": "2021", "title": "Laurea Magistrale in Ingegneria Meccanica"}],
                 "experiences": [{"company": "Officine Lario", "role": "Progettista"}]}
    out = merge_sheet(cv, parse_sheet(SHEET), from_text, today=TODAY)
    assert (out["first_name"], out["last_name"], out["trittico"], out["age"], out["location"]) == \
        ("Stefano", "Prandi", "SPR", 31, "Milano")
    # esperienze di tutte e due le fonti
    assert [e["company"] for e in out["experiences"]] == ["Datavia Consulting", "Officine Lario"]
    # titolo dalla scheda (il CV non ne aveva), con il voto scritto in TITOLO DI STUDIO
    assert out["education"] == [{"year": "2021", "title": "Laurea Magistrale in Ingegneria Meccanica – 104/110"}]
    assert out["cv_skills"][0] == {"name": "SQL", "level": "Ottima conoscenza"}  # nessun doppione
    assert {"lang": "inglese", "level": "Avanzato"} in out["languages"]
    assert {"lang": "spagnolo", "level": "Base"} in out["languages"]
    assert out["mcflash"]["budget"] == 325 and out["colloquio"]["recruiter"] == "TST"


def _exp(start, end, company, role, **kw):
    return {"start_date": start, "end_date": end, "company": company, "role": role, **kw}


def test_richer_cv_keeps_its_entries_and_takes_what_only_the_sheet_has():
    cv = {"experiences": [
        _exp("2025-07-01", None, "Datavia Consulting S.p.A.", "Data Engineer", ongoing=True,
             description="Pipeline ETL su AWS", projects=[{"name": "Termoclima"}], technologies=["dbt"]),
        _exp("2021-10-01", "2022-03-01", "Officine Meccaniche Lario S.r.l.", "Progettista meccanico"),
    ], "education": [{"year": "2025", "title": "Master in Big Data Analytics & Artificial Intelligence",
                      "institution": "Università di Pisa"}]}
    # la scheda chiama l'azienda in modo generico e ha un'esperienza che il CV non ha
    sheet_text = {"experiences": [
        _exp("2025-07", None, "una societa' di consulenza", "Data Engineer", ongoing=True,
             description="Altro testo", technologies=["Terraform", "dbt"]),
        _exp("2022-04", "2023-08", "Officine Meccaniche Lario", "Junior Project Manager",
             description="Coordinamento commesse"),
    ], "education": [{"year": "2025", "title": "Master in Big Data Analytics and Artificial Intelligence"},
                     {"year": "2021", "title": "Laurea Magistrale in Ingegneria Meccanica"}]}
    out = merge_sheet(cv, parse_sheet(SHEET), sheet_text, today=TODAY)
    roles = [(e["company"], e["role"]) for e in out["experiences"]]
    assert roles == [("Datavia Consulting S.p.A.", "Data Engineer"),
                     ("Officine Meccaniche Lario", "Junior Project Manager"),
                     ("Officine Meccaniche Lario S.r.l.", "Progettista meccanico")]  # dalla piu' recente
    current = out["experiences"][0]
    assert current["description"] == "Pipeline ETL su AWS"  # vince il CV
    assert current["technologies"] == ["dbt", "Terraform"]  # unite
    assert current["projects"] == [{"name": "Termoclima"}]
    titles = [e["title"] for e in out["education"]]
    assert titles == ["Master in Big Data Analytics & Artificial Intelligence",
                      "Laurea Magistrale in Ingegneria Meccanica – 104/110"]
    assert out["education"][0]["institution"] == "Università di Pisa"


def test_richer_sheet_fills_a_poor_cv():
    cv = {"experiences": [_exp("2025-07-01", None, "Datavia Consulting", None, ongoing=True)],
          "education": [{"year": None, "title": "Laurea Magistrale in Ingegneria Meccanica"}]}
    sheet_text = {"experiences": [
        _exp("2025-07", None, "Datavia Consulting", "Data Engineer", ongoing=True, description="ETL su AWS",
             projects=[{"name": "Fabric"}]),
        _exp("2021-10", "2023-08", "Officine Lario", "Progettista meccanico"),
    ], "education": [{"year": "2021", "title": "Laurea Magistrale in Ingegneria Meccanica", "institution": "PoliMi"},
                     {"year": "2018", "title": "Laurea Triennale in Ingegneria Meccanica"}],
        "trainings": [{"year": "2025", "title": "Terraform on AWS"}], "certifications": ["AWS Cloud Practitioner"]}
    out = merge_sheet(cv, parse_sheet(SHEET), sheet_text, today=TODAY)
    first = out["experiences"][0]
    assert (first["role"], first["description"], first["projects"]) == ("Data Engineer", "ETL su AWS", [{"name": "Fabric"}])
    assert len(out["experiences"]) == 2
    # Magistrale e Triennale sono titoli diversi; la Magistrale prende anno, ente e voto
    assert out["education"] == [
        {"year": "2021", "title": "Laurea Magistrale in Ingegneria Meccanica – 104/110", "institution": "PoliMi"},
        {"year": "2018", "title": "Laurea Triennale in Ingegneria Meccanica"}]
    assert out["trainings"] == [{"year": "2025", "title": "Terraform on AWS"}]
    assert out["certifications"] == ["AWS Cloud Practitioner"]


def test_merge_is_idempotent():
    cv = {"experiences": [_exp("2025-07-01", None, "Datavia", "Data Engineer", ongoing=True)], "education": []}
    sheet_text = {"experiences": [_exp("2022-04", "2023-08", "Officine Lario", "Junior Project Manager")],
                  "education": [{"year": "2021", "title": "Laurea Magistrale in Ingegneria Meccanica"}]}
    sheet = parse_sheet(SHEET)
    once = merge_sheet(cv, sheet, sheet_text, today=TODAY)
    twice = merge_sheet(once, sheet, sheet_text, today=TODAY)
    assert twice["experiences"] == once["experiences"] and twice["education"] == once["education"]
    assert once["education"][0]["title"].endswith("– 104/110") and "104/110 – " not in twice["education"][0]["title"]


def test_entries_without_dates_match_on_company_and_role():
    cv = {"experiences": [{"company": "Datavia Consulting", "role": "Data Engineer"}], "education": []}
    sheet_text = {"experiences": [{"company": "Datavia", "role": "Data Engineer", "description": "ETL"},
                                  {"company": "Datavia", "role": "Data Analyst"}]}
    out = merge_sheet(cv, parse_sheet(SHEET), sheet_text, today=TODAY)
    assert out["experiences"] == [{"company": "Datavia Consulting", "role": "Data Engineer", "description": "ETL"},
                                  {"company": "Datavia", "role": "Data Analyst"}]


class _FakeExtraction:
    def __init__(self, data):
        self.data = data

    def model_dump(self):
        return dict(self.data)


class _FakePipeline:
    def __init__(self):
        self.calls = []

    async def process(self, data, mime=None, profile="default"):
        self.calls.append((data, mime, profile))
        return _FakeExtraction({"first_name": "Stefano", "experiences": [{"company": "Datavia Consulting"}],
                                "education": [], "cv_skills": [], "languages": []})


def _call(body):
    req = func.HttpRequest(method="POST", url="/api/extract/scheda", body=json.dumps(body).encode(),
                           headers={"content-type": "application/json"})
    resp = asyncio.run(api.extract_sheet(req))
    return resp.status_code, json.loads(resp.get_body())


def test_route_without_cv_reads_the_sheet_with_the_llm(monkeypatch):
    fake = _FakePipeline()
    monkeypatch.setattr(api, "pipeline", fake)
    status, env = _call({"testo": SHEET})
    assert status == 200, env
    data = env["data"]
    assert fake.calls[0][1:] == ("text/plain", "mcflash")
    assert data["experiences"] == [{"company": "Datavia Consulting"}] and data["trittico"] == "SPR"


def test_route_with_cv_mixes_what_the_sheet_describes(monkeypatch):
    fake = _FakePipeline()
    monkeypatch.setattr(api, "pipeline", fake)
    status, env = _call({"testo": SHEET, "base": {"experiences": [{"company": "ACME", "role": "Tester"}],
                                                 "education": []}})
    assert status == 200 and len(fake.calls) == 1
    assert [e["company"] for e in env["data"]["experiences"]] == ["ACME", "Datavia Consulting"]


def test_route_with_cv_and_a_sheet_without_narrative_skips_the_llm(monkeypatch):
    fake = _FakePipeline()
    monkeypatch.setattr(api, "pipeline", fake)
    status, env = _call({"testo": SHORT_SHEET, "base": {"experiences": [{"company": "ACME"}], "education": []}})
    assert status == 200 and fake.calls == []
    assert env["data"]["experiences"] == [{"company": "ACME"}]


def test_route_validation(monkeypatch):
    monkeypatch.setattr(api, "pipeline", _FakePipeline())
    assert _call({"testo": "  "})[0] == 400


def test_one_sheet_entry_for_two_roles_in_the_same_company_is_not_lost():
    """L'LLM riassume "prima progettista, poi Junior PM" in un'unica esperienza 2021-2023."""
    cv = {"experiences": [_exp("2021-10-01", "2022-03-01", "Officine Meccaniche Lario S.r.l.", "Progettista meccanico")],
          "education": []}
    sheet_text = {"experiences": [_exp("2021-01", "2023-08", "Officine Meccaniche Lario", "Junior Project Manager")]}
    out = merge_sheet(cv, parse_sheet(SHEET), sheet_text, today=TODAY)
    assert [e["role"] for e in out["experiences"]] == ["Progettista meccanico", "Junior Project Manager"]


def test_skills_already_covered_are_not_added_again():
    cv = {"cv_skills": [{"name": "AWS S3"}, {"name": "Apache Spark / PySpark"}, {"name": "Git"}],
          "experiences": [{"company": "X"}], "education": []}
    sheet_text = {"cv_skills": [{"name": "S3"}, {"name": "PySpark"}, {"name": "Terraform"}, {"name": "git"}]}
    out = merge_sheet(cv, parse_sheet("NOME/COGNOME/TRIGRAMMA: STEFANO PRANDI - SPR"), sheet_text, today=TODAY)
    assert [s["name"] for s in out["cv_skills"]] == ["AWS S3", "Apache Spark / PySpark", "Git", "Terraform"]

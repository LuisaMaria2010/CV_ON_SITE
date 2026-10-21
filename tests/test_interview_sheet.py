"""Tests della scheda colloquio del recruiter (domain/interview_sheet.py, POST /api/extract/scheda)."""
from __future__ import annotations

import asyncio
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


def test_merge_sheet_wins_on_identity_and_keeps_cv_sections():
    cv = {"first_name": "SPR", "last_name": None, "trittico": None, "role": "Ingegnere", "age": None,
          "location": "Pisa", "cv_skills": [{"name": "SQL", "level": "Ottima conoscenza"}],
          "languages": [{"lang": "inglese", "level": "Base"}, {"lang": "spagnolo", "level": "Base"}],
          "experiences": [{"company": "Datavia Consulting", "role": "Data Engineer"}], "education": []}
    from_text = {"education": [{"year": "2021", "title": "Laurea Magistrale"}], "experiences": [{"company": "X"}]}
    out = merge_sheet(cv, parse_sheet(SHEET), from_text)
    assert (out["first_name"], out["last_name"], out["trittico"], out["age"], out["location"]) == \
        ("Stefano", "Prandi", "SPR", 31, "Milano")
    assert out["experiences"] == [{"company": "Datavia Consulting", "role": "Data Engineer"}]  # quelle del CV
    assert out["education"] == [{"year": "2021", "title": "Laurea Magistrale"}]  # il CV non le aveva
    assert out["cv_skills"][0] == {"name": "SQL", "level": "Ottima conoscenza"}  # nessun doppione
    assert {"lang": "inglese", "level": "Avanzato"} in out["languages"]
    assert {"lang": "spagnolo", "level": "Base"} in out["languages"]
    assert out["mcflash"]["budget"] == 325 and out["colloquio"]["recruiter"] == "TST"


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


def test_route_with_cv_skips_the_llm(monkeypatch):
    fake = _FakePipeline()
    monkeypatch.setattr(api, "pipeline", fake)
    status, env = _call({"testo": SHEET, "base": {"experiences": [{"company": "ACME"}], "education": []}})
    assert status == 200 and fake.calls == []
    assert env["data"]["experiences"] == [{"company": "ACME"}]


def test_route_validation(monkeypatch):
    monkeypatch.setattr(api, "pipeline", _FakePipeline())
    assert _call({"testo": "  "})[0] == 400

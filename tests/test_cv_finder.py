"""Tests per le regole comuni CV -> MCFlash (domain/cv_matching.py), la ricerca dei
CV della dashboard (services/cv_finder.py) e gli endpoint POST /api/cv-finder,
GET /api/cv/file, POST /api/cv-finder/link."""
from __future__ import annotations

import asyncio
import datetime as dt
import json

import azure.functions as func
import pytest

import functions.dashboard_api as api
from domain import cv_matching as cm
from domain.cv_profile import code_similarity, role_overlap
from services import cv_finder

TODAY = dt.date(2026, 10, 19)
D = dt.date


# ---------------------------------------------------------------- regole comuni

def test_header_from_mc_template_text():
    text = "Candidate Profile\nInformation\nNome LSC 02/05/1983\nCittà\nTorino\nRuolo\n.NET Developer\nOverview\n..."
    head = cm.header_from_text(text, "Skill_tecnico_LSC_MC.pdf")
    assert (head["code"], head["dob"], head["city"], head["role"]) == ("LSC", D(1983, 5, 2), "Torino", ".NET Developer")
    head = cm.header_from_text("Data di nascita: 20/10/1991\nqualcosa", "Skill_Tecnico_LSE_MC.pdf")
    assert head["dob"] == D(1991, 10, 20) and head["code"] == "LSE"  # codice dal nome del file


def test_codes_and_cities():
    assert cm.codes("FDE", "Skill_Tecnico_FDB.pdf") == {"fde", "fdb"}
    assert cm.file_code("Skill Tecnico_SSH_MC Engineering.pdf") == "SSH"
    assert cm.cities("Milano/remoto") == {"milano"} and cm.cities("Turin") == {"torino"}


def _opt(tier, delta=0, city=False, strict=False, key="k"):
    return {"key": key, "tier": tier, "delta": delta, "city_ok": city, "strict": strict, "role_ok": tier in (1, 2, 4)}


def test_choose_same_thresholds_as_rebuild():
    assert cm.choose([], 0)[0] == "nessun CV"
    assert cm.choose([_opt(1)], 1)[:2] == ("assegnato", _opt(1))
    assert cm.choose([_opt(2, None)], 1)[0] == "assegnato"
    assert cm.choose([_opt(1, key="a"), _opt(1, key="b")], 2)[0] == "dubbio"          # due persone uguali
    assert cm.choose([_opt(1, city=True, key="a"), _opt(1, key="b")], 2)[1]["key"] == "a"  # la citta' decide
    assert cm.choose([_opt(3)], 1) == ("dubbio", None, cm.DOUBT_REASONS[3])
    assert cm.choose([_opt(4, 11)], 2)[0] == "assegnato"                              # unica col mestiere, 11 anni
    assert cm.choose([_opt(4, 13)], 2)[0] == "dubbio"
    assert cm.choose([_opt(4, 20)], 1)[0] == "assegnato"                              # unico CV con quel codice


def test_evaluate_tiers():
    rec = {"eta": 30, "ruolo": "Data Analyst", "sede": "Milano"}
    group = [{"document_id": "a", "dob": D(1995, 1, 1), "role": "Data Analyst", "city": "Milano"}]
    assert cm.evaluate(rec, "k", group, lambda d: True, TODAY)["tier"] == 1
    assert cm.evaluate(rec, "k", [{**group[0], "role": "Chef"}], lambda d: False, TODAY)["tier"] == 3
    assert cm.evaluate(rec, "k", [{**group[0], "dob": None}], lambda d: True, TODAY)["tier"] == 2
    assert cm.evaluate(rec, "k", [{**group[0], "dob": D(1980, 1, 1)}], lambda d: True, TODAY)["tier"] == 4


def test_code_similarity_and_role_overlap():
    assert code_similarity("ASA", "ASE") == 2 and code_similarity("MDE", "DME") == 2
    assert code_similarity("LCA", "LCO") == 2 and code_similarity("ABC", "XYZ") == 0
    assert role_overlap("Data Scientist", ["Python"], "Data Scientist/Machine Learning") == 2


# ---------------------------------------------------------------- ricerca

def _doc(doc_id, codes, dob=None, city=None, role=None, text="testo del cv"):
    return {"document_id": doc_id, "file": f"{doc_id}.pdf", "source_path": f"/incoming-cv/{doc_id}.pdf",
            "codes": set(codes), "dob": dob, "city": city, "role": role, "skills": [], "text": text,
            "processed_at": "2026-10-01", "version": "1"}


DOCS = [
    _doc("lca_dati", ["lca"], D(1997, 9, 19), "Milano", "Data Analyst"),
    _doc("lca_net", ["lca"], D(1998, 6, 23), "Torino", ".NET Developer"),
    _doc("lca_altro_dati", ["lca"], D(1996, 4, 11), "Cosenza", "Data Engineer"),
    _doc("lco_dati", ["lco"], D(1998, 3, 1), "Milano", "Data Scientist"),
    _doc("lco_vecchio", ["lco"], D(1970, 1, 1), "Napoli", "COBOL Developer"),
]
PERSON = {"id_mcflash": "2594", "nome": "Lorenzo Carriero", "trittico": "LCA",
          "ruolo": "Data Scientist/Machine Learning Specialist", "eta": 27, "sede": "remoto"}


class _FakeLLM:
    """Mestiere coerente se il ruolo del CV e' di area dati."""
    def __init__(self):
        self.calls = 0
        self.chat = self
        self.completions = self

    def create(self, *, messages, **_):
        self.calls += 1
        line = next(l for l in messages[-1]["content"].splitlines() if l.startswith("Ruolo indicato nel CV"))
        content = json.dumps({"coerente": "Data" in line, "motivo": "test"})
        return type("R", (), {"choices": [type("C", (), {"message": type("M", (), {"content": content})()})()]})()


async def _no_header(doc):
    return None


def _find(person=PERSON, **kw):
    kw.setdefault("llm", _FakeLLM())
    kw.setdefault("docs", DOCS)
    kw.setdefault("headers", _no_header)
    kw.setdefault("overrides", {})
    return asyncio.run(cv_finder.find(person, today=TODAY, **kw))


def test_find_by_code_applies_rebuild_choice():
    out = _find()
    # lca_dati: 29 anni (+2), area dati -> livello 1; lca_altro_dati: 30 anni (+3), area dati -> livello 1;
    # a parita' di livello e di citta' decide la differenza d'eta': +2 batte +3.
    assert out["esito"]["decisione"] == "assegnato" and out["esito"]["file"] == "lca_dati.pdf"
    first = out["risultati"][0]
    assert first["scelto"] and first["livello"] == 1 and first["mestiere_ok"] and first["eta_delta"] == 2
    assert {r["document_id"] for r in out["risultati"]} == {"lca_dati", "lca_net", "lca_altro_dati"}
    net = next(r for r in out["risultati"] if r["document_id"] == "lca_net")
    assert net["livello"] == 3 and not net["mestiere_ok"]


def test_linked_and_reserved_cvs_are_not_proposed():
    linked = {"lca_dati": {"id": "99", "nome": "Altra Persona"}}
    out = _find(linked=linked, overrides={42: {"file": "lca_altro_dati.pdf", "nota": "di un altro"}})
    # restano solo CV di un altro mestiere: caso dubbio, nessuna assegnazione
    assert out["esito"]["decisione"] == "dubbio" and out["esito"]["file"] is None
    by_id = {r["document_id"]: r for r in out["risultati"]}
    assert by_id["lca_dati"]["collegato_a"]["nome"] == "Altra Persona"
    assert by_id["lca_altro_dati"]["riservato"] == "riservato a mano al candidato 42"


def test_manual_decision_wins():
    out = _find(overrides={2594: {"file": "lca_net.pdf", "nota": "deciso a mano"}})
    assert out["esito"] == {"decisione": "manuale", "motivo": "deciso a mano", "document_id": "lca_net",
                            "file": "lca_net.pdf"}
    no_code = _find(person={**PERSON, "trittico": "QQQ"}, overrides={2594: {"nota": "nessun CV"}})
    assert no_code["esito"]["decisione"] == "manuale"


def test_extended_search_similar_codes_without_choice():
    llm = _FakeLLM()
    out = _find(mode="esteso", llm=llm, exclude={"lca_dati"})
    assert out["esito"] is None
    assert [r["document_id"] for r in out["risultati"]] == ["lco_dati"]  # non il 1970, non gli LCA
    assert llm.calls == 1


def test_file_header_wins_over_index():
    async def header(doc):
        return {"code": "LSE", "dob": D(1991, 10, 20), "city": "Torino", "role": "Data Analyst/Data Scientist",
                "text": "intestazione"}
    luisa = {"nome": "Luisa Maria Serrano", "trittico": "LSE", "ruolo": "IA Engineer", "eta": None, "sede": "Torino"}
    docs = [_doc("skill_tecnico_lse_mc", ["lse"], None, None, None, text="")]
    out = _find(person=luisa, docs=docs, headers=header)
    r = out["risultati"][0]
    assert (r["nato"], r["citta"], r["ruolo_cv"]) == ("1991-10-20", "Torino", "Data Analyst/Data Scientist")
    assert r["livello"] == 2 and out["esito"]["decisione"] == "assegnato"  # eta' MCFlash assente: mestiere basta


def test_merge_documents_registry_is_the_truth():
    registry = {
        "skill_tecnico_lse_mc": {"source_path": "/incoming-cv/Skill_Tecnico_LSE_MC.pdf", "version": 1},
        "skill-tecnico_ssh_mc-engineering": {"source_path": "/incoming-cv/Skill Tecnico_SSH_MC Engineering.pdf"},
        "cv_vecchio": {"source_path": "/altro-container/cv.pdf"},
    }
    indexed = [_doc("Skill-Tecnico_SSH_MC-Engineering", ["ssh"], D(1991, 3, 30), "Roma", "Data Scientist")]
    entries = [{"document_id": "skill_tecnico_lse_mc", "id_mcflash": "5", "file": "x.pdf", "codice": "LSE"}]
    docs = {d["document_id"]: d for d in cv_finder.merge_documents(registry, indexed, entries)}
    assert set(docs) == {"skill_tecnico_lse_mc", "skill-tecnico_ssh_mc-engineering"}
    assert docs["skill_tecnico_lse_mc"]["codes"] == {"lse"} and docs["skill_tecnico_lse_mc"]["version"] == "1"
    assert docs["skill-tecnico_ssh_mc-engineering"]["role"] == "Data Scientist"


def test_find_rejects_unknown_mode():
    with pytest.raises(ValueError):
        _find(mode="tutto")


# ---------------------------------------------------------------- endpoint

ROWS = [
    {"id": "2594", "trittico": "LCA", "nome_completo": "Lorenzo Carriero", "ruolo": PERSON["ruolo"], "eta": 27,
     "sede": "remoto", "cv": None, "idoneo": True},
    {"id": "99", "trittico": "LCA", "nome_completo": "Altra Persona", "ruolo": ".NET", "eta": 28, "sede": "Torino",
     "cv": {"document_id": "lca_net"}, "idoneo": True},
]


@pytest.fixture
def endpoint_fakes(monkeypatch):
    async def rows(*, refresh=False):
        return [dict(r) for r in ROWS], []

    async def documents(entries):
        return DOCS

    upserts = []
    monkeypatch.setattr(api, "_candidate_rows", rows)
    monkeypatch.setattr(cv_finder, "documents", documents)
    monkeypatch.setattr(cv_finder, "file_header", _no_header)
    monkeypatch.setattr(cm, "load_overrides", lambda path=None: {})
    monkeypatch.setattr(api.cv_mcflash_map, "read_file_entries",
                        lambda: [{"document_id": "lca_net", "id_mcflash": "99", "file": "lca_net.pdf", "codice": "LCA"}])
    monkeypatch.setattr(api.cv_mcflash_map, "upsert_entry",
                        lambda doc, idm, **kw: upserts.append((doc, idm, kw)) or {"RowKey": doc, "file": kw["file"]})
    monkeypatch.setattr("services.coherence_evaluator.get_judge_client", lambda: _FakeLLM())
    return upserts


def _req(method, url, body=None, params=None):
    return func.HttpRequest(method=method, url=url, body=json.dumps(body or {}).encode(),
                            headers={"content-type": "application/json"}, params=params or {}, route_params={})


def _call(handler, req):
    resp = asyncio.run(handler(req))
    return resp.status_code, json.loads(resp.get_body())


def test_search_endpoint(endpoint_fakes):
    status, env = _call(api.cv_finder_search, _req("POST", "/api/cv-finder", {"id_mcflash": "2594"}))
    assert status == 200
    data = env["data"]
    assert data["esito"]["file"] == "lca_dati.pdf"
    assert next(r for r in data["risultati"] if r["document_id"] == "lca_net")["collegato_a"]["nome"] == "Altra Persona"
    status, env = _call(api.cv_finder_search, _req("POST", "/api/cv-finder",
                                                    {"nome": "Lorenzo", "cognome": "Carriero", "ruolo": "Data Scientist",
                                                     "modo": "esteso", "escludi": ["lca_dati"]}))
    assert status == 200 and env["data"]["persona"]["trittico"] == "LCA"
    assert [r["document_id"] for r in env["data"]["risultati"]] == ["lco_dati"]


def test_search_validation(endpoint_fakes):
    assert _call(api.cv_finder_search, _req("POST", "/api/cv-finder", {"modo": "boh", "id_mcflash": "2594"}))[0] == 400
    assert _call(api.cv_finder_search, _req("POST", "/api/cv-finder", {}))[0] == 400
    assert _call(api.cv_finder_search, _req("POST", "/api/cv-finder", {"id_mcflash": "1"}))[0] == 404


def test_link(endpoint_fakes):
    status, _ = _call(api.cv_finder_link, _req("POST", "/", {"id_mcflash": "2594", "document_id": "lca_dati"}))
    assert status == 200 and endpoint_fakes[0][:2] == ("lca_dati", "2594")
    assert endpoint_fakes[0][2]["criterio"] == "dashboard-ricerca"
    assert _call(api.cv_finder_link, _req("POST", "/", {"id_mcflash": "2594", "document_id": "lca_net"}))[0] == 400
    assert _call(api.cv_finder_link, _req("POST", "/", {"id_mcflash": "99", "document_id": "lco_dati"}))[0] == 400
    assert _call(api.cv_finder_link, _req("POST", "/", {"id_mcflash": "2594", "document_id": "nessuno"}))[0] == 404
    assert len(endpoint_fakes) == 1


def test_cv_file(endpoint_fakes, monkeypatch):
    class _Storage:
        async def download_bytes(self, blob, container=None):
            assert (container, blob) == ("incoming-cv", "lco_dati.pdf")
            return b"%PDF"
    monkeypatch.setattr(api, "storage", _Storage())
    resp = asyncio.run(api.cv_file(_req("GET", "/api/cv/file", params={"document_id": "LCO_DATI"})))
    assert resp.status_code == 200 and resp.get_body() == b"%PDF"
    assert _call(api.cv_file, _req("GET", "/api/cv/file", params={"document_id": "x"}))[0] == 404


def test_possible_tritticos_for_compound_names():
    from domain.candidate_identity import possible_tritticos
    assert possible_tritticos("Luisa Maria Serrano") == ["LMA", "MLU", "LSE", "SLU"]
    assert possible_tritticos("Mario Rossi") == ["MRO", "RMA"] and possible_tritticos("Cher") == []


def test_search_tries_every_code_of_a_typed_name(endpoint_fakes, monkeypatch):
    docs = [_doc("skill_tecnico_lse_mc", ["lse"], D(1991, 10, 20), "Torino", "Data Analyst/Data Scientist")]

    async def documents(entries):
        return docs
    monkeypatch.setattr(cv_finder, "documents", documents)
    # nome e cognome scritti "al contrario" rispetto al CV: Maria finisce nel cognome
    status, env = _call(api.cv_finder_search, _req("POST", "/api/cv-finder",
                                                    {"nome": "Luisa", "cognome": "Maria Serrano", "ruolo": "AI Engineer"}))
    assert status == 200 and env["data"]["persona"]["codici"][0] == "LMA" and "LSE" in env["data"]["persona"]["codici"]
    assert [r["document_id"] for r in env["data"]["risultati"]] == ["skill_tecnico_lse_mc"]


def test_code_only_search_lists_cvs_without_judging():
    llm = _FakeLLM()
    out = _find(person={"trittico": "LCA", "ruolo": "", "eta": None}, llm=llm)
    assert out["esito"]["decisione"] == "solo codice" and llm.calls == 0
    assert {r["document_id"] for r in out["risultati"]} == {"lca_dati", "lca_net", "lca_altro_dati"}
    assert all(r["livello"] is None for r in out["risultati"])

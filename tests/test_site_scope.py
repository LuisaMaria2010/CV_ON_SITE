"""
Tests per il perimetro `site` della ricerca candidati.

`site` arriva dal frontend nel body di /api/ai-matcher-wrapper
('mcflashcandidati' o 'mcflashrisorse'), viaggia all'agente dentro
CONTEXT_JSON e torna al server come campo di invoke_searcher_wrapper. La
ricerca seleziona solo i record MCFlash con quel site: nel DB il valore puo'
essere un host ('mcflashcandidati.mcengineering.eu') o un nome secco.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import pytest
import azure.functions as func

import functions.matcher_api as matcher_api
import functions.search_api as search_api
from core.errors import InvalidInputError
from infra.mcflash_candidates import MCFlashCandidatesClient
from utils.site import record_visible, resolve_site, site_key


# =========================================================
# 1. Normalizzazione e validazione
# =========================================================

@pytest.mark.parametrize("raw,expected", [
    ("mcflashcandidati", "mcflashcandidati"),
    ("mcflashcandidati.mcengineering.eu", "mcflashcandidati"),
    ("https://McFlashCandidati.mcengineering.eu/", "mcflashcandidati"),
    ("  MCFLASHRISORSE ", "mcflashrisorse"),
    (None, ""),
])
def test_site_key(raw, expected):
    assert site_key(raw) == expected


@pytest.mark.parametrize("value", ["mcflashcandidati", "mcflashrisorse", "McFlashRisorse"])
def test_resolve_site_accepts_known_values(value):
    assert resolve_site({"site": value}) == value.lower()


@pytest.mark.parametrize("payload", [{}, {"site": ""}, {"site": None}, {"site": "mcflash"}, None])
def test_resolve_site_rejects_missing_or_unknown(payload):
    with pytest.raises(InvalidInputError):
        resolve_site(payload)


def test_candidati_sees_only_candidati():
    assert record_visible({"site": "mcflashcandidati.mcengineering.eu"}, "mcflashcandidati")
    assert not record_visible({"site": "mcflashrisorse"}, "mcflashcandidati")
    assert not record_visible({"Nome": "senza site"}, "mcflashcandidati")


def test_risorse_sees_risorse_and_candidati():
    assert record_visible({"Site": "mcflashrisorse"}, "mcflashrisorse")
    assert record_visible({"site": "mcflashcandidati.mcengineering.eu"}, "mcflashrisorse")
    assert not record_visible({"Nome": "senza site"}, "mcflashrisorse")


@pytest.mark.parametrize("record,eligible", [
    ({"abilitato": True, "isTalentUp": False}, True),
    ({"Abilitato": "true", "istalentup": "false"}, True),
    ({"abilitato": False, "isTalentUp": False}, False),
    ({"abilitato": "false", "isTalentUp": False}, False),
    ({}, False),                      # campo mancante: escluso
    ({"abilitato": None, "isTalentUp": False}, False),
    ({"abilitato": True, "isTalentUp": True}, False),   # TalentUp: mai ricercabile
    ({"abilitato": True}, False),                       # isTalentUp mancante: escluso
])
def test_eligibility(record, eligible):
    assert MCFlashCandidatesClient.is_eligible(record) is eligible


# =========================================================
# 2. Filtro nel client MCFlash
# =========================================================

class _DummyMCFlashClient(MCFlashCandidatesClient):
    def __init__(self, records: list[dict]):
        super().__init__(base_url="https://example.test", api_key="test")
        self._records = records

    async def fetch_page(self, *, limit: int, offset: int) -> list[dict]:
        return self._records[offset:offset + limit]


_RECORDS = [
    {"Id": "1", "Ruolo": "Java Developer", "site": "mcflashcandidati.mcengineering.eu", "abilitato": True, "isTalentUp": False},
    {"Id": "2", "Ruolo": "Java Developer", "site": "mcflashrisorse", "abilitato": True, "isTalentUp": False},
    {"Id": "3", "Ruolo": "Data Analyst", "site": "mcflashcandidati.mcengineering.eu", "abilitato": True, "isTalentUp": False},
    {"Id": "4", "Ruolo": "Java Developer", "abilitato": True, "isTalentUp": False},
    {"Id": "5", "Ruolo": "Java Developer", "site": "mcflashcandidati.mcengineering.eu", "abilitato": False, "isTalentUp": False},
    {"Id": "6", "Ruolo": "Java Developer", "site": "mcflashrisorse", "abilitato": False, "isTalentUp": False},
    {"Id": "7", "Ruolo": "Java Developer", "site": "mcflashcandidati.mcengineering.eu", "abilitato": True, "isTalentUp": True},
    {"Id": "8", "Ruolo": "Java Developer", "site": "mcflashrisorse", "abilitato": True, "isTalentUp": True},
]


@pytest.mark.asyncio
async def test_candidati_site_sees_only_candidati():
    client = _DummyMCFlashClient(_RECORDS)
    items = await client.filter_candidates(limit=50, offset=0, site="mcflashcandidati", return_all_matches=True)
    assert [c["Id"] for c in items] == ["1", "3"]


@pytest.mark.asyncio
async def test_risorse_site_sees_both_sites():
    client = _DummyMCFlashClient(_RECORDS)
    items = await client.filter_candidates(limit=50, offset=0, site="mcflashrisorse", return_all_matches=True)
    assert [c["Id"] for c in items] == ["1", "2", "3"]


@pytest.mark.asyncio
async def test_site_is_an_additional_filter():
    client = _DummyMCFlashClient(_RECORDS)
    items = await client.filter_candidates(
        limit=50, offset=0, role="java developer", site="mcflashrisorse", return_all_matches=True,
    )
    assert [c["Id"] for c in items] == ["1", "2"]


@pytest.mark.asyncio
async def test_disabled_records_never_come_back():
    """abilitato=false e isTalentUp=true sono esclusi da ogni lettura del DB, anche senza filtri."""
    client = _DummyMCFlashClient(_RECORDS)
    everything = await client.filter_candidates(limit=50, offset=0, return_all_matches=True)
    assert {"5", "6", "7", "8"}.isdisjoint(c["Id"] for c in everything)
    assert await client.find_candidates("5") == []
    assert await client.find_candidates("7") == []
    assert {"5", "6", "7", "8"}.isdisjoint(c["Id"] for c in await client.fetch_candidates(limit=50, offset=0))


# =========================================================
# 3. Inoltro da /api/searcher-wrapper al pipeline
# =========================================================

def _search(body: dict) -> dict:
    req = func.HttpRequest(
        method="POST", url="/api/searcher-wrapper", body=json.dumps(body).encode(),
        headers={"content-type": "application/json"}, params={}, route_params={},
    )
    out = asyncio.run(search_api.searcher_wrapper(req))
    env = out if isinstance(out, dict) else json.loads(out.get_body().decode())
    return env.get("data") if isinstance(env.get("data"), dict) else env


def test_searcher_wrapper_forwards_site_to_pipeline(monkeypatch):
    seen: dict[str, Any] = {}

    async def _fake(payload):
        seen["payload"] = payload
        return {"hits": [], "meta": {}, "suggestions": []}

    monkeypatch.setattr(search_api, "_run_search_pipeline", _fake)
    _search({"site": "mcflashrisorse", "search_request": {"role": "java developer"}})
    assert seen["payload"]["site"] == "mcflashrisorse"


# =========================================================
# 4. Propagazione da /api/ai-matcher-wrapper
# =========================================================

class _Resp:
    output_text = "ok"
    id = "r1"


@pytest.fixture
def agent(monkeypatch):
    box: dict[str, Any] = {}

    def _fake(*, agent_name, message, model_name, previous_response_id, attempts=None):
        box["message"] = message
        return _Resp()

    monkeypatch.setattr(matcher_api, "_run_foundry_agent", _fake)
    monkeypatch.setattr(matcher_api, "_response_to_plain_dict", lambda r: {"id": r.id})
    monkeypatch.setattr(matcher_api, "_chat_history_load_thread", lambda u, c: None)
    monkeypatch.setattr(matcher_api, "_chat_history_append_turn", lambda **kw: {"turn_count": 1})
    return box


def _matcher(body: dict):
    req = func.HttpRequest(
        method="POST", url="/api/ai-matcher-wrapper", body=json.dumps(body).encode(),
        headers={"content-type": "application/json"}, params={}, route_params={},
    )
    return asyncio.run(matcher_api.ai_matcher_wrapper(req))


def _data(out) -> dict:
    env = out if isinstance(out, dict) else json.loads(out.get_body().decode())
    return env.get("data") if isinstance(env.get("data"), dict) else env


def _context(message: str) -> dict:
    return json.loads(message.split("CONTEXT_JSON:", 1)[1].strip())


_BASE = {
    "user_id": "u-test-001",
    "chat_id": "chat-test-001",
    "user_request": "quali sono i migliori esperti in python?",
}


@pytest.mark.parametrize("site", ["mcflashcandidati", "mcflashrisorse"])
def test_site_reaches_the_agent(agent, site):
    data = _data(_matcher({**_BASE, "site": site}))
    session = _context(agent["message"])["session"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", session.pop("today"))
    assert session == {
        "user_id": "u-test-001", "chat_id": "chat-test-001", "site": site,
    }
    assert data["session"]["site"] == site


def test_caller_cannot_change_site_via_context(agent):
    _matcher({**_BASE, "site": "mcflashcandidati", "context": {"session": {"site": "mcflashrisorse"}}})
    assert _context(agent["message"])["session"]["site"] == "mcflashcandidati"


@pytest.mark.parametrize("extra", [{}, {"site": "altro"}])
def test_missing_or_unknown_site_is_rejected(agent, extra):
    out = _matcher({**_BASE, **extra})
    assert out.status_code == 400
    assert "message" not in agent

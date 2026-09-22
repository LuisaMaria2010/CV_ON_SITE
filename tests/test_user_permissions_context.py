"""
Tests per la propagazione dei permessi utente all'agente ai_matcher.

`/api/ai-matcher-wrapper` risolve lo scope dell'utente e lo trasporta dentro
CONTEXT_JSON, che viene concatenato al messaggio inviato all'agente. Cosa
l'agente ne faccia e' deciso dalle sue istruzioni: qui verifichiamo solo che
l'informazione arrivi, sia corretta e non sia sovrascrivibile dal chiamante.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import azure.functions as func

import functions.matcher_api as matcher_api
import utils.visibility as visibility
from utils.visibility import SCOPE_EXTERNAL, SCOPE_INTERNAL, resolve_user_scope


class _FakeResponse:
    output_text = "Ecco i candidati."
    id = "resp-1"


class _EmptyResponse:
    """Turno dell'agente senza testo: possibile con tool_choice='required',
    o quando la risposta viene filtrata o va in timeout."""
    output_text = ""
    id = "resp-empty"


@pytest.fixture
def captured(monkeypatch):
    """Intercetta il messaggio passato all'agente, senza toccare Foundry/Azure."""
    box: dict[str, Any] = {}

    def _fake_run_agent(*, agent_name, message, model_name, previous_response_id):
        box["message"] = message
        box["agent_name"] = agent_name
        return _FakeResponse()

    monkeypatch.setattr(matcher_api, "_run_foundry_agent", _fake_run_agent)
    monkeypatch.setattr(matcher_api, "_response_to_plain_dict", lambda r: {"id": r.id})
    monkeypatch.setattr(matcher_api, "_split_ai_matcher_answer", lambda t: (t, []))
    # La persistenza su Azure Table non deve girare nei test.
    monkeypatch.setattr(matcher_api, "_chat_history_load_thread", lambda u, c: None)
    monkeypatch.setattr(
        matcher_api, "_chat_history_append_turn",
        lambda **kwargs: {"turn_count": 1},
    )
    # Nessuna impostazione di ambiente: solo i default del resolver.
    monkeypatch.setattr(visibility, "settings_value", lambda *k, default="": default)
    return box


def _call(body: dict) -> dict:
    req = func.HttpRequest(
        method="POST",
        url="/api/ai-matcher-wrapper",
        body=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        params={},
        route_params={},
    )
    response = asyncio.run(matcher_api.ai_matcher_wrapper(req))
    if not isinstance(response, dict):
        envelope = json.loads(response.get_body().decode())
        return envelope.get("data") if isinstance(envelope.get("data"), dict) else envelope
    return response


def _context_sent_to_agent(message: str) -> dict:
    """Riestrae il CONTEXT_JSON concatenato al messaggio dell'agente."""
    assert "CONTEXT_JSON:" in message
    return json.loads(message.split("CONTEXT_JSON:", 1)[1].strip())


_BASE = {"user_id": "tizio@example.com", "chat_id": "c1", "user_request": "cerco un java dev"}


def test_external_user_permission_reaches_the_agent(captured):
    _call(_BASE)
    session = _context_sent_to_agent(captured["message"])["session"]
    assert session["scope"] == SCOPE_EXTERNAL
    assert session["permissions"]["can_view_non_mcflash"] is False


def test_internal_user_permission_reaches_the_agent(captured):
    _call({**_BASE, "user_scope": "interno"})
    session = _context_sent_to_agent(captured["message"])["session"]
    assert session["scope"] == SCOPE_INTERNAL
    assert session["permissions"]["can_view_non_mcflash"] is True


def test_permissions_survive_a_caller_supplied_context(captured):
    """Un `context` del chiamante non deve cancellare il blocco permessi."""
    _call({**_BASE, "context": {"search_request": {"role": "java"}}})
    context = _context_sent_to_agent(captured["message"])
    assert context["search_request"] == {"role": "java"}
    assert context["session"]["permissions"]["can_view_non_mcflash"] is False


def test_caller_cannot_raise_its_own_permissions(captured):
    """Il blocco permessi calcolato vince su quello iniettato in context.session."""
    _call({
        **_BASE,
        "context": {"session": {"permissions": {"can_view_non_mcflash": True}, "scope": "internal"}},
    })
    session = _context_sent_to_agent(captured["message"])["session"]
    assert session["scope"] == SCOPE_EXTERNAL
    assert session["permissions"]["can_view_non_mcflash"] is False


def test_permissions_echoed_in_the_http_response(captured):
    data = _call(_BASE)
    assert data["session"]["permissions"]["can_view_non_mcflash"] is False
    assert data["session"]["scope"] == SCOPE_EXTERNAL


# =========================================================
# Risoluzione dello scope
# =========================================================

def test_scope_defaults_to_external(monkeypatch):
    """Fail-closed: senza informazioni si dichiara il permesso piu' ristretto."""
    monkeypatch.setattr(visibility, "settings_value", lambda *k, default="": default)
    assert resolve_user_scope({"user_id": "chi.sei@ignoto.it"}) == SCOPE_EXTERNAL
    assert resolve_user_scope({}) == SCOPE_EXTERNAL
    assert resolve_user_scope(None) == SCOPE_EXTERNAL


def test_unrecognised_scope_value_does_not_count_as_internal(monkeypatch):
    monkeypatch.setattr(visibility, "settings_value", lambda *k, default="": default)
    assert resolve_user_scope({"user_scope": "intrno", "user_id": "x@y.it"}) == SCOPE_EXTERNAL


def test_scope_from_internal_user_ids(monkeypatch):
    monkeypatch.setattr(
        visibility, "settings_value",
        lambda *keys, default="": "anna@mc.it, luca@mc.it" if "INTERNAL_USER_IDS" in keys else default,
    )
    assert resolve_user_scope({"user_id": "ANNA@mc.it"}) == SCOPE_INTERNAL
    assert resolve_user_scope({"user_id": "ospite@altro.it"}) == SCOPE_EXTERNAL


def test_scope_from_internal_email_domain(monkeypatch):
    monkeypatch.setattr(
        visibility, "settings_value",
        lambda *keys, default="": "mcdirectory.it" if "INTERNAL_EMAIL_DOMAINS" in keys else default,
    )
    assert resolve_user_scope({"user_id": "chiunque@mcdirectory.it"}) == SCOPE_INTERNAL
    assert resolve_user_scope({"user_id": "tizio@gmail.com"}) == SCOPE_EXTERNAL


# =========================================================
# Regressione: risposta vuota dell'agente
# =========================================================

def test_empty_agent_output_does_not_raise(captured, monkeypatch):
    """`_truncate_text` non era importato: questo ramo alzava NameError, cioe'
    un 500 al frontend invece di una risposta degradata."""
    monkeypatch.setattr(
        matcher_api, "_run_foundry_agent",
        lambda **kwargs: _EmptyResponse(),
    )
    monkeypatch.setattr(matcher_api, "_response_to_plain_dict", lambda r: {"id": r.id})

    data = _call(_BASE)

    assert isinstance(data.get("answer"), str)
    assert data["agent"]["response_id"] == "resp-empty"
    # I permessi restano corretti anche sul percorso degradato.
    assert data["session"]["permissions"]["can_view_non_mcflash"] is False

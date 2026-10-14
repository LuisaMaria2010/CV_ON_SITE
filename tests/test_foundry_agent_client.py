"""Client Foundry: timeout e retry espliciti, cosi' una richiesta appesa non porta al 504."""
import infra.foundry_agent as fa


class _FakeOpenAI:
    created: list[dict] = []

    def __init__(self, **kwargs):
        _FakeOpenAI.created.append(kwargs)


def _client(monkeypatch, env=None):
    _FakeOpenAI.created = []
    monkeypatch.setattr(fa, "_mc_matcher_client", None)
    monkeypatch.setattr(fa, "_OpenAI", _FakeOpenAI)
    monkeypatch.setattr(fa, "_openai_available", True)
    monkeypatch.setenv("AZURE_OPENAI_KEY", "k")
    monkeypatch.setenv("AZURE_AI_PROJECT_ENDPOINT", "https://x.services.ai.azure.com/api/projects/p")
    for key in ("FOUNDRY_TIMEOUT_SECONDS", "FOUNDRY_SDK_MAX_RETRIES"):
        monkeypatch.delenv(key, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    assert fa.get_mc_matcher_client() is not None
    return _FakeOpenAI.created[-1]


def test_client_has_timeout_and_retries_below_gateway_limit(monkeypatch):
    kwargs = _client(monkeypatch)
    assert kwargs["timeout"] == 70.0 and kwargs["max_retries"] == 2
    # caso peggiore: tutti i tentativi vanno in timeout, ma prima dei 230 s del gateway
    assert kwargs["timeout"] * (kwargs["max_retries"] + 1) < 230


def test_client_timeout_and_retries_from_settings(monkeypatch):
    kwargs = _client(monkeypatch, {"FOUNDRY_TIMEOUT_SECONDS": "45", "FOUNDRY_SDK_MAX_RETRIES": "1"})
    assert kwargs["timeout"] == 45.0 and kwargs["max_retries"] == 1


def test_invalid_settings_fall_back_to_defaults(monkeypatch):
    kwargs = _client(monkeypatch, {"FOUNDRY_TIMEOUT_SECONDS": "abc", "FOUNDRY_SDK_MAX_RETRIES": "x"})
    assert kwargs["timeout"] == 70.0 and kwargs["max_retries"] == 2


def test_attempts_are_collected_for_the_caller(monkeypatch):
    # run_foundry_agent gira in un thread: i tentativi tornano al chiamante, che li logga.
    class _Responses:
        def create(self, **kwargs):
            return "ok"

    class _Client:
        responses = _Responses()

    monkeypatch.setattr(fa, "get_mc_matcher_client", lambda: _Client())
    attempts: list = []
    assert fa.run_foundry_agent(agent_name="a", message="m", attempts=attempts) == "ok"
    assert len(attempts) == 1 and attempts[0]["attempt"] == 1 and attempts[0]["error"] is None


def test_matcher_logs_foundry_metric_from_the_caller(monkeypatch, caplog):
    import asyncio
    import json
    import logging

    import azure.functions as func

    import functions.matcher_api as matcher_api

    class _Resp:
        output_text = "ok"
        id = "r1"

    def _fake(**kwargs):
        kwargs["attempts"].append({"attempt": 1, "ms": 12.0, "error": None})
        return _Resp()

    monkeypatch.setattr(matcher_api, "_run_foundry_agent", _fake)
    monkeypatch.setattr(matcher_api, "_response_to_plain_dict", lambda r: {"id": r.id})
    monkeypatch.setattr(matcher_api, "_chat_history_load_thread", lambda u, c: None)
    monkeypatch.setattr(matcher_api, "_chat_history_append_turn", lambda **kw: {"turn_count": 1})
    req = func.HttpRequest(method="POST", url="/api/ai-matcher-wrapper", headers={},
                           body=json.dumps({"user_id": "u", "chat_id": "c", "user_request": "java", "site": "mcflashrisorse"}).encode())
    with caplog.at_level(logging.INFO, logger=matcher_api.logger.name):
        asyncio.run(matcher_api.ai_matcher_wrapper(req))
    assert any("METRIC::foundry_agent_ms=" in r.getMessage() and "attempts=1" in r.getMessage()
               for r in caplog.records)

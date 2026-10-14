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

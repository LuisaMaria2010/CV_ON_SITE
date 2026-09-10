"""
Crea / aggiorna l'agente Foundry `mc-matcher` (kind=prompt) per il sistema MC Flash.

Questo script riflette lo stato REALE dell'agente deployato nel progetto Foundry
`test-project` (letto da Azure AI Foundry, non piu' una bozza teorica):

- un solo agente, `mc-matcher`, model `gpt-4.1-mini`, temperature 0, top_p 1,
  tool_choice "required";
- istruzioni caricate da `scripts/mc_matcher_agent_instructions.md` (copia
  verbatim della versione attiva);
- due tool: `web_search` (built-in) e un tool OpenAPI `invoke_searcher_wrapper`
  che chiama `POST /api/searcher-wrapper` (auth anonima: la route Function e'
  `AuthLevel.ANONYMOUS`).

Nota: l'agente in produzione viene gestito a mano dal portale Foundry. Questo
script serve a ricrearlo / riportarlo a uno stato noto, non e' il canale di
deploy abituale.

Uso:
    python scripts/create_foundry_agents.py --dry-run
    python scripts/create_foundry_agents.py \
        --project-endpoint https://<account>.services.ai.azure.com/api/projects/<project> \
        --searcher-wrapper-url https://<functionapp>.azurewebsites.net/api/searcher-wrapper
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:  # readable UTF-8 output on Windows consoles (instructions contain →, ≠, …)
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


_INSTRUCTIONS_FILE = Path(__file__).with_name("mc_matcher_agent_instructions.md")

DEFAULT_PROJECT_ENDPOINT = "https://foundry-ai-mc-dev.services.ai.azure.com/api/projects/test-project"
DEFAULT_MODEL = "gpt-4.1-mini"
DEFAULT_AGENT_NAME = "mc-matcher"
DEFAULT_SEARCHER_WRAPPER_URL = "https://dev-function-ai-mc.azurewebsites.net/api/searcher-wrapper"


def _env_first(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value.strip()
    return default


def _require(value: str | None, message: str) -> str:
    if value:
        return value
    raise SystemExit(message)


def _load_matcher_instructions() -> str:
    if not _INSTRUCTIONS_FILE.exists():
        raise SystemExit(f"Instructions file mancante: {_INSTRUCTIONS_FILE}")
    return _INSTRUCTIONS_FILE.read_text(encoding="utf-8")


def _server_url(raw_url: str) -> str:
    """scheme://host dell'URL del wrapper, senza path e senza query (?code=...).
    Il tool OpenAPI deployato usa il server nudo + il path dentro `paths`."""
    parsed = urlparse(raw_url)
    if not parsed.scheme or not parsed.netloc:
        raise SystemExit(
            "L'URL del searcher-wrapper deve essere assoluto, es. "
            "https://<functionapp>.azurewebsites.net/api/searcher-wrapper"
        )
    return f"{parsed.scheme}://{parsed.netloc}"


def _searcher_wrapper_openapi_spec(wrapper_url: str) -> dict[str, Any]:
    """Spec OpenAPI del tool `invoke_searcher_wrapper`, identica a quella
    registrata sull'agente `mc-matcher` in Foundry."""
    return {
        "openapi": "3.0.1",
        "info": {
            "title": "MC Flash Searcher Wrapper",
            "version": "1.0.0",
            "description": (
                "Wrapper API che inoltra il payload classificato al searcher e "
                "restituisce i risultati, gia' valutati per coerenza."
            ),
        },
        "servers": [{"url": _server_url(wrapper_url)}],
        "paths": {
            "/api/searcher-wrapper": {
                "post": {
                    "operationId": "invokeSearcherWrapper",
                    "summary": "Invoke searcher wrapper",
                    "description": (
                        "Invoca il wrapper /api/searcher-wrapper con payload JSON nel body POST. "
                        "Esegue ricerca + valutazione di coerenza in un'unica chiamata."
                    ),
                    "parameters": [],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/SearcherWrapperRequest"}
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": (
                                "search_response.hits (candidati gia' ordinati per coerenza), "
                                "verdict, clarifying_questions."
                            )
                        }
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "SearcherWrapperRequest": {
                    "type": "object",
                    "required": ["search_request"],
                    "additionalProperties": True,
                    "properties": {
                        "search_request": {
                            "type": "object",
                            "description": "Richiesta strutturata da inoltrare al motore search.",
                            "additionalProperties": True,
                        },
                        "original_request": {
                            "type": "string",
                            "description": (
                                "Testo libero della richiesta originale dell'utente. Necessario "
                                "perche' il valutatore di coerenza interno (riordino candidati + "
                                "clarifying_questions) giri: se assente o vuoto, la valutazione "
                                "viene saltata."
                            ),
                        },
                    },
                }
            }
        },
    }


def _build_tools(wrapper_url: str) -> list[dict[str, Any]]:
    return [
        {"type": "web_search"},
        {
            "type": "openapi",
            "openapi": {
                "name": "invoke_searcher_wrapper",
                "description": "Invoca il wrapper /api/searcher-wrapper con payload classificato.",
                "spec": _searcher_wrapper_openapi_spec(wrapper_url),
                "auth": {"type": "anonymous", "security_scheme": {}},
            },
        },
    ]


def _build_definition_payload(model: str, instructions: str, tools: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "kind": "prompt",
        "model": model,
        "instructions": instructions,
        "temperature": 0,
        "top_p": 1,
        "tool_choice": "required",
        "text": {"format": {"type": "text"}},
        "tools": tools,
    }


def create_agent(
    project_endpoint: str,
    model: str,
    searcher_wrapper_url: str,
    agent_name: str,
    dry_run: bool,
) -> None:
    instructions = _load_matcher_instructions()
    tools = _build_tools(searcher_wrapper_url)
    definition_payload = _build_definition_payload(model, instructions, tools)

    if dry_run:
        preview = {
            "project_endpoint": project_endpoint,
            "agent_name": agent_name,
            "definition": {
                **{k: v for k, v in definition_payload.items() if k != "instructions"},
                "instructions_preview": instructions[:280],
                "instructions_chars": len(instructions),
            },
        }
        print(json.dumps(preview, indent=2, ensure_ascii=False))
        return

    try:
        from azure.ai.projects import AIProjectClient
        from azure.ai.projects.models import PromptAgentDefinition
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:
        raise SystemExit(
            "Serve azure-ai-projects + azure-identity in un ambiente dedicato "
            "(qui il repo usa il venv .venv_agents). Rilancia con --dry-run per "
            "il solo preview del payload."
        ) from exc

    definition = PromptAgentDefinition(
        model=model,
        instructions=instructions,
        tools=tools,
    )
    # Campi non sempre esposti come kwarg dal costruttore, ma accettati come attributi.
    definition.temperature = 0
    definition.top_p = 1
    definition.tool_choice = "required"

    with DefaultAzureCredential() as credential, AIProjectClient(
        endpoint=project_endpoint,
        credential=credential,
    ) as project_client:
        agent = project_client.agents.create_version(
            agent_name=agent_name,
            definition=definition,
        )

    print(json.dumps(
        {"name": agent.name, "id": agent.id, "version": agent.version},
        indent=2,
        ensure_ascii=True,
    ))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crea/aggiorna l'agente Foundry prompt `mc-matcher` per MC Flash.",
    )
    parser.add_argument(
        "--project-endpoint",
        default=_env_first(
            "AZURE_AI_PROJECT_ENDPOINT",
            "FOUNDRY_PROJECT_ENDPOINT",
            "AZURE_FOUNDRY_PROJECT_ENDPOINT",
            default=DEFAULT_PROJECT_ENDPOINT,
        ),
        help="Endpoint del progetto Foundry, es. https://<account>.services.ai.azure.com/api/projects/<project>",
    )
    parser.add_argument(
        "--model",
        default=_env_first(
            "AZURE_AI_MODEL_DEPLOYMENT_NAME",
            "FOUNDRY_MODEL_DEPLOYMENT_NAME",
            "FOUNDRY_MODEL",
            default=DEFAULT_MODEL,
        ),
        help="Nome del deployment/model nel progetto Foundry.",
    )
    parser.add_argument(
        "--searcher-wrapper-url",
        default=_env_first(
            "FOUNDRY_SEARCHER_WRAPPER_URL",
            "SEARCHER_WRAPPER_URL",
            default=DEFAULT_SEARCHER_WRAPPER_URL,
        ),
        help="URL di POST /api/searcher-wrapper (l'eventuale ?code= viene ignorato: la route e' anonima).",
    )
    parser.add_argument(
        "--agent-name",
        default=_env_first("MC_MATCHER_AGENT_NAME", "AI_MATCHER_AGENT_NAME", default=DEFAULT_AGENT_NAME),
        help="Nome logico dell'agente (default: mc-matcher).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Non crea nulla: stampa il payload che verrebbe inviato.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    project_endpoint = _require(
        args.project_endpoint,
        "Manca il project endpoint. Imposta AZURE_AI_PROJECT_ENDPOINT o passa --project-endpoint.",
    )
    searcher_wrapper_url = _require(
        args.searcher_wrapper_url,
        "Manca la searcher-wrapper URL. Imposta FOUNDRY_SEARCHER_WRAPPER_URL o passa --searcher-wrapper-url.",
    )
    create_agent(
        project_endpoint=project_endpoint,
        model=args.model,
        searcher_wrapper_url=searcher_wrapper_url,
        agent_name=args.agent_name,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()

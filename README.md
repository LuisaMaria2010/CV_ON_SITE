# Autocompilazione CV_ON_SITE

Breve descrizione: pipeline di ingestione CV serverless (Azure Functions) che estrae, normalizza e indicizza CV in formato markdown e chunk per Azure AI Search.

## Variabili d'ambiente principali

La pipeline legge le configurazioni tramite `core.config.Settings`. Le principali variabili d'ambiente utilizzate sono:

- `AZURE_OPENAI_ENDPOINT` (o `azure_openai_endpoint` in Settings): endpoint Azure OpenAI o Azure AI Foundry.
- `AZURE_OPENAI_KEY` (alias popolato in Settings): chiave/API key per Azure OpenAI (opzionale se si usa Managed Identity).
- `AZURE_OPENAI_MODEL` / `azure_openai_model`: nome deployment/model.
- `AZURE_SEARCH_SERVICE_ENDPOINT` (alias `search_endpoint`): endpoint del servizio Azure Cognitive Search.
- `AZURE_SEARCH_API_KEY` (alias `azure_search_api_key`): API key per Search.
- `AzureWebJobsStorage` (alias principale per `storage_account_connection_string`): connection string per lo storage account (usato in locale e dalle Function App).
- `STORAGE_ACCOUNT_CONNECTION_STRING` / `STORAGE_CONNECTION_STRING`: alias alternativi per la connection string di storage.
- `STORAGE_ACCOUNT_URL` (alias `storage_account_url`): URL base dell'account blob (es. `https://<account>.blob.core.windows.net`).

## Alias supportati

Per compatibilità con `local.settings.json` e nomi legacy, il progetto popola i campi `Settings` usando sia `pydantic-settings` `env_names` sia una routine di fallback che legge questi alias:

- `storage_account_connection_string`: `AzureWebJobsStorage`, `STORAGE_ACCOUNT_CONNECTION_STRING`, `STORAGE_CONNECTION_STRING`
- `storage_account_url`: `STORAGE_ACCOUNT_URL`
- `search_endpoint`: `AZURE_SEARCH_SERVICE_ENDPOINT`
- `azure_openai_key`: `AZURE_OPENAI_KEY`
- `azure_search_api_key`: `AZURE_SEARCH_API_KEY`

Questa copertura evita warning di compatibilità e mantiene il comportamento precedente.

## Valori di default importanti

- `storage_container_incoming`: `incoming-cv`
- `storage_container_original_uploads`: `incoming-cv-originals`
- `storage_container_normalized_markdown`: `normalized-cv-md`
- `document_processing_queue_name`: `document-processing`
- `document_indexing_queue_name`: `document-indexing`
- `document_registry_table_name`: `DocumentRegistry`

## Esegui i test

Per lanciare i test in ambiente di sviluppo:

```powershell
$env:PYTHONPATH='.'; .venv\Scripts\python -m pytest -q
```

## Export DB -> Table Storage automatico (giornaliero, locale)

Per eseguire export candidati senza passare dalla runtime Function App, sono disponibili due script locali:

- `scripts/run_daily_candidates_export.ps1`
	- esegue l'export una tantum
	- legge i settings DB/Storage dalla Function App (solo config management via Azure CLI)
	- scrive log in `logs_extracted/`
- `scripts/register_daily_candidates_export_task.ps1`
	- registra un task schedulato Windows giornaliero

Esecuzione manuale (una tantum):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_daily_candidates_export.ps1 -MaxRows 0
```

Registrazione task giornaliero (es. ore 02:00):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_daily_candidates_export_task.ps1 -StartTime 02:00
```

Test run immediato del task:

```powershell
schtasks /Run /TN "DailyCandidatesExport"
```

Note:

- `-MaxRows 0` significa export completo (nessun limite).
- `-IncludePayload` aggiunge `payload_json` nella tabella di destinazione.
- Nome tabella di default: `CandidatesSnapshot`.

## Foundry agent

L'agente di produzione e' `mc-matcher` (kind=prompt) nel progetto Foundry, gestito
a mano dal portale. `scripts/create_foundry_agents.py` serve solo a ricrearlo /
riportarlo a uno stato noto:

- istruzioni caricate verbatim da `scripts/mc_matcher_agent_instructions.md`
- model `gpt-4.1-mini`, temperature 0, tool_choice `required`
- tool: `web_search` (built-in) + OpenAPI `invoke_searcher_wrapper` -> `POST /api/searcher-wrapper` (route anonima)

Variabili richieste:

- `AZURE_AI_PROJECT_ENDPOINT`: endpoint progetto Foundry, formato `https://<account>.services.ai.azure.com/api/projects/<project>`
- `FOUNDRY_MODEL` (o `AZURE_AI_MODEL_DEPLOYMENT_NAME`): deployment model del progetto Foundry
- `FOUNDRY_SEARCHER_WRAPPER_URL`: URL della `POST /api/searcher-wrapper` (l'eventuale `?code=` viene ignorato: la route e' anonima)

Comando:

```powershell
.venv\Scripts\python scripts\create_foundry_agents.py --dry-run
.venv\Scripts\python scripts\create_foundry_agents.py
```

Note operative:

- Lo script gira in `--dry-run` anche nel venv corrente. Per creare davvero gli agenti serve un helper venv separato, perche' `azure-ai-projects` richiede `openai>=2.8` mentre questa Function app usa `langchain-openai` con `openai<2`.
- Helper venv consigliato:

```powershell
py -3.11 -m venv .foundry-agent-venv
.foundry-agent-venv\Scripts\python -m pip install --upgrade pip
.foundry-agent-venv\Scripts\python -m pip install --pre azure-ai-projects azure-identity
.foundry-agent-venv\Scripts\python scripts\create_foundry_agents.py
```

- Ogni esecuzione crea una nuova versione dell'agente con lo stesso nome logico (`mc-matcher`).

### Permessi Foundry -> Function App

Per concedere l'accesso operativo a Foundry verso la Function App (key-based), usa lo script:

```powershell
scripts\grant_foundry_function_permissions.ps1 \
	-SubscriptionId <subscription-id> \
	-ResourceGroupName <resource-group> \
	-FunctionAppName <function-app-name> \
	-FoundryAccountName <foundry-account-name>
```

Lo script:
- verifica l'identita' managed identity della risorsa Foundry
- crea/ruota una function key dedicata (`foundry-wrapper-key`) per `searcher-wrapper`
- stampa la `FOUNDRY_SEARCHER_WRAPPER_URL` pronta da usare nello script di creazione agenti

Nota: in questo setup i permessi runtime sono basati su function key. Se vuoi enforcement Microsoft Entra ID (EasyAuth), va configurato un passaggio aggiuntivo di auth `authsettingsV2` sulla Function App.

## Note

- Le modifiche recenti hanno rimosso l'uso diretto di `Field(..., env=...)` per compatibilità con Pydantic v2 e adottato `env_names` + fallback esplicito.
- Se desideri aggiungere altre variabili d'ambiente o alias, posso aggiornare questa tabella.

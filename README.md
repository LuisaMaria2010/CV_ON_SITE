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

## Foundry agents

Per creare i tre agenti Foundry per MC Flash:

- `mc-classifier`: classifica la query utente in un JSON strutturato e chiama il wrapper API del searcher per ottenere le risposte.
- `mc-profile-search-agent`: usa la vostra `POST /api/search` tramite tool OpenAPI e applica la logica di rilassamento coerente con l'email di Kim.
- `mc-search-evaluator-agent`: valuta la qualita' del `search_response` e restituisce un verdetto strutturato riutilizzabile dal classifier/orchestratore.

Variabili richieste:

- `AZURE_AI_PROJECT_ENDPOINT`: endpoint progetto Foundry, formato `https://<account>.services.ai.azure.com/api/projects/<project>`
- `AZURE_AI_MODEL_DEPLOYMENT_NAME`: deployment model del progetto Foundry
- `FOUNDRY_SEARCH_API_URL`: URL completo della vostra `POST /api/search`; puo' includere `?code=<function-key>`
- `FOUNDRY_SEARCHER_WRAPPER_URL`: URL completo della `POST /api/searcher-wrapper`; puo' includere `?code=<function-key>`
- `FOUNDRY_EVALUATOR_WRAPPER_URL`: URL completo della `POST /api/match-evaluator-wrapper`; puo' includere `?code=<function-key>`

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

- Se `FOUNDRY_SEARCH_API_URL` include la function key, lo script la inserisce nello schema OpenAPI del tool.
- Se `FOUNDRY_SEARCHER_WRAPPER_URL` include la function key, lo script la inserisce nello schema OpenAPI del classifier.
- Ogni esecuzione crea una nuova versione degli agenti con lo stesso nome logico.

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

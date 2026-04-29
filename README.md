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

## Note

- Le modifiche recenti hanno rimosso l'uso diretto di `Field(..., env=...)` per compatibilità con Pydantic v2 e adottato `env_names` + fallback esplicito.
- Se desideri aggiungere altre variabili d'ambiente o alias, posso aggiornare questa tabella.

"""
Reset completo ambiente di sviluppo:
  1. Svuota la tabella DocumentRegistry (Azure Table Storage)
  2. Elimina e ricrea l'indice chunk su Azure AI Search

Uso:
    python scripts/reset_dev_environment.py
    python scripts/reset_dev_environment.py --index-name cv-doc-chunks
"""

import asyncio
import argparse
import json
import os
import sys
from pathlib import Path

# --- assicura PYTHONPATH ---
root = Path(__file__).parent.parent
sys.path.insert(0, str(root))

# carica local.settings.json se presente
settings_file = root / "local.settings.json"
if settings_file.exists():
    with settings_file.open() as f:
        data = json.load(f)
    for k, v in data.get("Values", {}).items():
        os.environ.setdefault(k, str(v))

from azure.data.tables import TableServiceClient
from azure.search.documents.indexes.aio import SearchIndexClient
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import ResourceNotFoundError

from core.config import settings
from infra.search_service import SearchService


# ── 1. Svuota registry ────────────────────────────────────────────────────────
def clear_registry(table_name: str, connection_string: str) -> None:
    service = TableServiceClient.from_connection_string(connection_string)
    try:
        table = service.get_table_client(table_name)
        entities = list(table.list_entities())
        if not entities:
            print(f"Registry '{table_name}': già vuoto.")
            return
        for entity in entities:
            table.delete_entity(
                partition_key=entity["PartitionKey"],
                row_key=entity["RowKey"],
            )
        print(f"Registry '{table_name}': eliminati {len(entities)} record.")
    except ResourceNotFoundError:
        print(f"Registry '{table_name}': tabella non trovata (nessuna azione).")


# ── 2. Ricrea indice ──────────────────────────────────────────────────────────
async def recreate_index(index_name: str) -> None:
    # elimina se esiste
    idx_client = SearchIndexClient(
        endpoint=settings.search_endpoint,
        credential=AzureKeyCredential(settings.azure_search_api_key),
    )
    try:
        await idx_client.delete_index(index_name)
        print(f"Indice '{index_name}': eliminato.")
    except ResourceNotFoundError:
        print(f"Indice '{index_name}': non esisteva.")
    finally:
        await idx_client.close()

    # ricrea
    svc = SearchService()
    try:
        await svc.create_or_update_index(index_name)
        print(f"Indice '{index_name}': creato con il nuovo schema.")
    finally:
        await svc.close()


# ── main ──────────────────────────────────────────────────────────────────────
async def main(index_name: str) -> None:
    connection_string = os.environ.get("AzureWebJobsStorage", "")
    table_name = settings.document_registry_table_name

    print("=" * 55)
    print("RESET AMBIENTE DI SVILUPPO")
    print("=" * 55)
    print(f"Search endpoint : {settings.search_endpoint}")
    print(f"Indice          : {index_name}")
    print(f"Registry table  : {table_name}")
    print("-" * 55)

    # 1. svuota registry
    if connection_string:
        clear_registry(table_name, connection_string)
    else:
        print("ATTENZIONE: AzureWebJobsStorage non trovata — registry non svuotato.")

    # 2. ricrea indice
    await recreate_index(index_name)

    print("-" * 55)
    print("Reset completato.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reset dev environment (registry + index)")
    parser.add_argument(
        "--index-name",
        default=None,
        help="Nome indice (default: DOCUMENT_SEARCH_INDEX_NAME)",
    )
    args = parser.parse_args()
    target = args.index_name or settings.document_search_index_name
    asyncio.run(main(target))

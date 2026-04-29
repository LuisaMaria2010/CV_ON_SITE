"""
Crea o aggiorna l'indice chunk su Azure AI Search.

Uso:
    python scripts/create_index.py
    python scripts/create_index.py --index-name my-custom-index

Lo script carica le variabili da local.settings.json (se eseguito dalla root
del progetto) e chiama SearchService.create_or_update_index().
"""

import asyncio
import argparse
import json
import os
import sys
from pathlib import Path

# --- assicura che il progetto sia in PYTHONPATH ---
root = Path(__file__).parent.parent
sys.path.insert(0, str(root))

# carica local.settings.json se presente (solo in locale)
settings_file = root / "local.settings.json"
if settings_file.exists():
    with settings_file.open() as f:
        data = json.load(f)
    for k, v in data.get("Values", {}).items():
        os.environ.setdefault(k, str(v))

from infra.search_service import SearchService
from core.config import settings


async def main(index_name: str | None = None) -> None:
    target = index_name or settings.document_search_index_name
    print(f"Endpoint  : {settings.search_endpoint}")
    print(f"Index     : {target}")
    print("Creazione/aggiornamento indice in corso...")

    svc = SearchService()
    try:
        await svc.create_or_update_index(target)
        print(f"OK — indice '{target}' creato/aggiornato con successo.")
    finally:
        await svc.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create or update Azure AI Search index")
    parser.add_argument("--index-name", default=None, help="Nome indice (default: DOCUMENT_SEARCH_INDEX_NAME)")
    args = parser.parse_args()
    asyncio.run(main(args.index_name))

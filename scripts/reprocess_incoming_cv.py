"""
Rielabora DA LOCALE alcuni CV gia' presenti in incoming-cv, con il codice di
questo repo, senza ricaricarli: la Function App pubblicata non entra in gioco.

Esegue la stessa funzione della coda document-processing
(ingestion_triggers.process_incoming_cv) con lo stesso messaggio che produrrebbe
il blob trigger: estrazione del testo, arricchimento LLM, registro (versione +1
se il testo e' cambiato, cancellazione dei chunk vecchi), markdown normalizzato
e indicizzazione con embeddings.

Uso:
    python scripts/reprocess_incoming_cv.py "<blob>" ["<blob>" ...]          # simulazione
    python scripts/reprocess_incoming_cv.py "<blob>" ["<blob>" ...] --yes
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

root = Path(__file__).parent.parent
sys.path.insert(0, str(root))
settings_file = root / "local.settings.json"
if settings_file.exists():
    for k, v in json.loads(settings_file.read_text(encoding="utf-8")).get("Values", {}).items():
        os.environ.setdefault(k, str(v))

import azure.functions as func  # noqa: E402
from azure.search.documents.aio import SearchClient  # noqa: E402

import ingestion_triggers  # noqa: E402
from core.config import settings  # noqa: E402
from domain.normalizer import TextNormalizer  # noqa: E402
from functions.cv_ingest import _build_processing_message  # noqa: E402
from infra.search_service import SearchService  # noqa: E402


async def _chunks(document_id: str) -> dict[int, int]:
    service = SearchService()
    client = SearchClient(endpoint=settings.search_endpoint, index_name=service.chunk_index_name,
                          credential=service._credential)
    try:
        res = await client.search(search_text="*", filter=f"document_id eq '{document_id}'",
                                  select=["version"], top=1000)
        counts: dict[int, int] = {}
        async for r in res:
            v = int(r.get("version") or 0)
            counts[v] = counts.get(v, 0) + 1
        return counts
    finally:
        await client.close()


def _state(blob: str) -> str:
    registry = ingestion_triggers._get_document_registry()
    doc_id = registry._normalize_document_id(TextNormalizer.normalize_document_id(blob))
    rows = list(registry.table_client.query_entities(query_filter=f"RowKey eq '{doc_id}'"))
    reg = [(r.get("version"), r.get("status")) for r in rows]
    return f"document_id={doc_id} registro={reg or 'nessuna riga'} indice={asyncio.run(_chunks(doc_id))}"


def main(blobs: list[str], apply: bool) -> int:
    container = settings.storage_container_incoming
    for blob in blobs:
        data = ingestion_triggers._download_blob_sync(container, blob)
        mime = ingestion_triggers._detect_mime_type(blob)
        result = ingestion_triggers.document_processor.process(
            data, mime_type=mime, filename=blob, source_path=f"/{container}/{blob}")
        registry = ingestion_triggers._get_document_registry()
        decision = registry.should_process(
            f"/{container}/{blob}", result["content_hash"],
            document_id=TextNormalizer.normalize_document_id(blob),
            text_chars=len(result["extracted_text"] or ""))
        print(f"{blob}\n  prima:   {_state(blob)}\n  testo:   {len(result['extracted_text'])} caratteri"
              f" | il registro direbbe: elabora={decision[0]} versione={decision[1]}")
        if not apply:
            continue
        message = _build_processing_message(blob_name=blob)
        ingestion_triggers.process_incoming_cv(func.QueueMessage(body=json.dumps(message).encode("utf-8")))
        print(f"  dopo:    {_state(blob)}")
    if not apply:
        print("\nSimulazione: niente e' stato elaborato. Aggiungi --yes per procedere.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("blobs", nargs="+", help="nomi dei file in incoming-cv")
    parser.add_argument("--yes", action="store_true", help="elabora davvero (senza: simulazione)")
    args = parser.parse_args()
    sys.exit(main(args.blobs, args.yes))

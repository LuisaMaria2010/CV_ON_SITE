"""
Cancella tutto cio' che una conferma "crea" della dashboard ha prodotto: serve
per togliere i CV di prova dall'ambiente reale.

Per l'operazione indicata (tabella CvOperations) toglie:
- i chunk del documento dall'indice AI Search e le righe del DocumentRegistry;
- il markdown normalizzato (normalized-cv-md/<document_id>/...);
- il CV MC in incoming-cv e la riga in CvMcflashMap (solo se punta a questo candidato);
- il candidato dal MCFlash finto (mai da MCFlash vero);
- la bozza e, con --with-original, il CV caricato in incoming-cv-originals;
- infine l'operazione stessa.

Le operazioni "aggiorna" si accettano solo per i candidati del MCFlash finto
(Id da 900001): per un candidato vero toglierebbero il suo CV (il vecchio e'
in cv-history e andrebbe ripristinato a mano). Per quelle si toglie anche lo
storico cv-history/<id>/.

Uso:
    python scripts/delete_dashboard_cv.py <operation_id>                 # simulazione
    python scripts/delete_dashboard_cv.py <operation_id> --yes [--with-original]
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

from azure.search.documents.aio import SearchClient  # noqa: E402

from core.config import settings  # noqa: E402
from core.dependencies import storage  # noqa: E402
from functions.drafts_api import drafts_container  # noqa: E402
from infra import cv_mcflash_map  # noqa: E402
from infra.document_registry import DocumentRegistry  # noqa: E402
from infra.mcflash_writer import FAKE_ID_START, FakeMCFlashWriter  # noqa: E402
from infra.search_service import SearchService  # noqa: E402
from services.cv_commit import OperationStore, history_container  # noqa: E402


async def _chunk_count(document_id: str) -> int:
    service = SearchService()
    client = SearchClient(endpoint=settings.search_endpoint, index_name=service.chunk_index_name,
                          credential=service._credential)
    try:
        results = await client.search(search_text="*", filter=f"document_id eq '{document_id}'", top=1000)
        return len([r async for r in results])
    finally:
        await client.close()


async def main(op_id: str, apply: bool, with_original: bool) -> int:
    store = OperationStore()
    op = store.get(op_id)
    if op is None:
        print(f"Operazione {op_id} non trovata")
        return 1
    doc_id = op.get("document_id") or ""
    id_mcflash = str(op.get("id_mcflash") or "")
    fake_id = id_mcflash.isdigit() and int(id_mcflash) > FAKE_ID_START
    if op.get("kind") != "crea" and not fake_id:
        print("Operazione 'aggiorna' di un candidato vero: non si cancella (toglierebbe il suo CV).")
        return 1
    registry = DocumentRegistry.from_settings()
    reg_rows = list(registry.table_client.query_entities(query_filter=f"RowKey eq '{doc_id}'")) if doc_id else []
    md_blobs = await storage.list_names(container=settings.storage_container_normalized_markdown,
                                        prefix=f"{doc_id}/") if doc_id else []
    map_rows = [e for e in cv_mcflash_map.read_file_entries() if e["document_id"] == doc_id]
    map_ok = all(e["id_mcflash"] == id_mcflash for e in map_rows)
    fake = fake_id and op.get("mcflash_mode") == "finto"
    history = await storage.list_names(container=history_container(), prefix=f"{id_mcflash}/") if fake_id else []
    chunks = await _chunk_count(doc_id) if doc_id else 0

    print(f"Operazione {op_id}: {op.get('nome')} ({op.get('trittico')}) stato={op.get('status')}")
    print(f"  indice:      {chunks} chunk con document_id={doc_id!r}")
    print(f"  registro:    {len(reg_rows)} righe")
    print(f"  markdown:    {md_blobs}")
    print(f"  incoming-cv: {op.get('incoming_blob')!r}")
    print(f"  mappa:       {[(e['document_id'], e['id_mcflash']) for e in map_rows]}"
          + ("" if map_ok else "  <- punta a un altro candidato: NON si tocca"))
    print(f"  MCFlash:     Id {id_mcflash} " + ("(finto: si cancella)" if fake else "(non finto: NON si tocca)"))
    print(f"  storico:     {history}")
    print(f"  bozza:       {op.get('draft_id')}")
    print(f"  originale:   {op.get('original_blob')!r}" + ("" if with_original else " (resta: manca --with-original)"))
    if not apply:
        print("\nSimulazione: niente e' stato cancellato. Aggiungi --yes per procedere.")
        return 0

    if doc_id:
        await SearchService().delete_chunks(doc_id)
        for row in reg_rows:
            registry.table_client.delete_entity(row["PartitionKey"], row["RowKey"])
        for blob in md_blobs:
            await storage.delete(blob, container=settings.storage_container_normalized_markdown)
    if op.get("incoming_blob"):
        await storage.delete(op["incoming_blob"], container=settings.storage_container_incoming)
    if map_rows and map_ok:
        cv_mcflash_map.delete_entry(doc_id)
    if fake:
        try:
            FakeMCFlashWriter().delete(id_mcflash)
        except Exception:
            pass  # gia' tolto da un'altra operazione dello stesso candidato
    for blob in history:
        await storage.delete(blob, container=history_container())
    if op.get("draft_id"):
        await storage.delete(f"{op['draft_id']}.json", container=drafts_container())
    if with_original and op.get("original_blob"):
        await storage.delete(op["original_blob"], container=settings.storage_container_original_uploads)
    store.table.delete_entity("op", op_id)
    print("\nCancellato.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("operation_id")
    parser.add_argument("--yes", action="store_true", help="cancella davvero (senza: simulazione)")
    parser.add_argument("--with-original", action="store_true", help="cancella anche il CV caricato")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.operation_id.strip().lower(), args.yes, args.with_original)))

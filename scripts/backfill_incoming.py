"""
Backfill: mette in coda i blob già presenti in incoming-cv senza toccarli.

Uso:
    python scripts/backfill_incoming.py
    python scripts/backfill_incoming.py --dry-run          # mostra cosa farebbe
    python scripts/backfill_incoming.py --prefix "2026/"   # solo un prefisso
    python scripts/backfill_incoming.py --max 50           # limite blob

La function 'process_incoming_cv' (queue trigger) processa poi ogni messaggio
esattamente come se il blob fosse appena arrivato.
"""

import asyncio
import argparse
import json
import os
import sys
from pathlib import Path

# --- PYTHONPATH e local.settings ---
root = Path(__file__).parent.parent
sys.path.insert(0, str(root))

settings_file = root / "local.settings.json"
if settings_file.exists():
    with settings_file.open() as f:
        data = json.load(f)
    for k, v in data.get("Values", {}).items():
        os.environ.setdefault(k, str(v))

from core.config import settings
from infra.backfill_enqueuer import BackfillEnqueuer


async def main(dry_run: bool, prefix: str | None, max_items: int, only_pdf: bool) -> None:
    connection_string = os.environ.get("AzureWebJobsStorage", "")
    if not connection_string:
        print("ERRORE: AzureWebJobsStorage non configurata.")
        sys.exit(1)

    container = os.environ.get("STORAGE_CONTAINER_INCOMING", "incoming-cv")
    queue = os.environ.get("DOCUMENT_PROCESSING_QUEUE_NAME", "document-processing")

    print("=" * 55)
    print("BACKFILL INCOMING CV")
    print("=" * 55)
    print(f"Container : {container}")
    print(f"Coda      : {queue}")
    print(f"Prefisso  : {prefix or '(tutti)'}")
    print(f"Max blob  : {max_items}")
    print(f"Solo PDF  : {only_pdf}")
    print(f"Dry-run   : {dry_run}")
    print("-" * 55)

    enqueuer = BackfillEnqueuer(
        connection_string=connection_string,
        container_name=container,
        queue_name=queue,
    )

    result = await enqueuer.enqueue_existing(
        prefix=prefix,
        max_items=max_items,
        dry_run=dry_run,
        only_pdf=only_pdf,
    )

    print(f"Scansionati : {result.get('scanned', 0)}")
    print(f"Accodati    : {result.get('selected', 0)}")
    if result.get('skipped_non_pdf'):
        print(f"Saltati (non PDF): {result.get('skipped_non_pdf', 0)}")
    if dry_run:
        print("\n[DRY-RUN] Nessun messaggio inviato.")
    else:
        print("\nFatto. Controlla la function app per il processing.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill: re-enqueue existing blobs")
    parser.add_argument("--dry-run", action="store_true", help="Mostra senza accodare")
    parser.add_argument("--prefix", default=None, help="Filtra per prefisso nome blob")
    parser.add_argument("--max", type=int, default=10000, dest="max_items", help="Max blob da processare (default: 10000)")
    parser.add_argument("--only-pdf", action="store_true", default=True, help="Solo file PDF (default: True)")
    parser.add_argument("--all-types", action="store_true", help="Includi tutti i tipi di file")
    args = parser.parse_args()

    asyncio.run(main(
        dry_run=args.dry_run,
        prefix=args.prefix,
        max_items=args.max_items,
        only_pdf=not args.all_types,
    ))

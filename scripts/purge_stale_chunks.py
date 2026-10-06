"""
Toglie dall'indice i chunk delle versioni vecchie dei CV.

Fino al 05/10/2026 SearchService.delete_chunks non cancellava nulla (mancava
un await): a ogni nuova versione di un CV i chunk della precedente restavano
nell'indice accanto a quelli nuovi (gli id contengono la versione), e la
ricerca, che non filtra per versione, li usava ancora.

Criterio: per ogni document_id si guarda quali versioni ci sono NELL'INDICE e
si cancellano i chunk delle versioni inferiori alla piu' alta presente. La
versione piu' recente non si tocca mai, quindi nessun CV resta senza chunk.

Uso:
    python scripts/purge_stale_chunks.py            # simulazione: conta e basta
    python scripts/purge_stale_chunks.py --yes      # cancella
"""
from __future__ import annotations

import argparse
import asyncio
import collections
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
from infra.search_service import SearchService  # noqa: E402

BATCH = 500


async def _all_chunks(client: SearchClient) -> list[dict]:
    """Id, document_id e versione di tutti i chunk (paginazione gestita dall'SDK)."""
    results = await client.search(search_text="*", select=["id", "document_id", "version"], top=100000)
    return [{"id": r["id"], "document_id": r.get("document_id"), "version": int(r.get("version") or 0)}
            async for r in results]


def stale_chunks(chunks: list[dict]) -> tuple[list[dict], dict[str, dict[int, int]]]:
    """Chunk da cancellare (versione < massima presente per quel documento) e riepilogo per documento."""
    by_doc: dict[str, list[dict]] = collections.defaultdict(list)
    for c in chunks:
        by_doc[c["document_id"]].append(c)
    stale: list[dict] = []
    summary: dict[str, dict[int, int]] = {}
    for doc_id, items in by_doc.items():
        latest = max(c["version"] for c in items)
        old = [c for c in items if c["version"] < latest]
        if old:
            stale += old
            summary[doc_id] = dict(collections.Counter(c["version"] for c in items))
    return stale, summary


async def main(apply: bool) -> int:
    service = SearchService()
    client = SearchClient(endpoint=settings.search_endpoint, index_name=service.chunk_index_name,
                          credential=service._credential)
    try:
        chunks = await _all_chunks(client)
        stale, summary = stale_chunks(chunks)
        docs = {c["document_id"] for c in chunks}
        print(f"Indice {service.chunk_index_name}: {len(chunks)} chunk, {len(docs)} documenti")
        print(f"Documenti con versioni vecchie: {len(summary)} | chunk vecchi da cancellare: {len(stale)}")
        print(f"Chunk che restano: {len(chunks) - len(stale)}")
        for doc_id, versions in list(summary.items())[:5]:
            print(f"  es. {doc_id}: {versions} -> resta solo v{max(versions)}")
        if not apply:
            print("\nSimulazione: niente e' stato cancellato. Aggiungi --yes per procedere.")
            return 0
        deleted = 0
        for start in range(0, len(stale), BATCH):
            batch = [{"id": c["id"]} for c in stale[start:start + BATCH]]
            await client.delete_documents(batch)
            deleted += len(batch)
        print(f"\nCancellati {deleted} chunk.")
        return 0
    finally:
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="cancella davvero (senza: simulazione)")
    sys.exit(asyncio.run(main(parser.parse_args().yes)))

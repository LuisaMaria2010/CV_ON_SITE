"""
Allinea la tabella CV -> MCFlash su Azure Table storage al file JSON.

La tabella (vedi infra/cv_mcflash_map.py) e' quella letta dal pipeline di
ricerca. Il JSON (scripts/cv_mcflash_map.json) e' la sorgente versionata:
questo script inserisce/aggiorna ogni voce e cancella le righe che nel JSON
non ci sono piu', cosi' la tabella e' sempre una copia esatta del file.

Uso:
    python scripts/upload_cv_mcflash_map.py --dry-run   # mostra cosa cambierebbe
    python scripts/upload_cv_mcflash_map.py
    python scripts/upload_cv_mcflash_map.py --file altro.json

Connessione: AzureWebJobsStorage (env o local.settings.json).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

root = Path(__file__).parent.parent
sys.path.insert(0, str(root))

settings_file = root / "local.settings.json"
if settings_file.exists():
    with settings_file.open(encoding="utf-8") as f:
        for k, v in json.load(f).get("Values", {}).items():
            os.environ.setdefault(k, str(v))

from azure.data.tables import TableServiceClient, UpdateMode  # noqa: E402

from infra.cv_mcflash_map import PARTITION_KEY, _connection_string, cv_mcflash_map_table_name  # noqa: E402

DEFAULT_FILE = root / "scripts" / "cv_mcflash_map.json"
_FORBIDDEN_KEY_CHARS = set("/\\#?")


def _entities(path: Path) -> dict[str, dict]:
    documents = json.loads(path.read_text(encoding="utf-8"))["documents"]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = {}
    for doc_id, entry in documents.items():
        entry = entry if isinstance(entry, dict) else {"id_mcflash": entry}
        if not doc_id or _FORBIDDEN_KEY_CHARS & set(doc_id):
            raise ValueError(f"document_id non valido come RowKey: {doc_id!r}")
        out[doc_id] = {
            "PartitionKey": PARTITION_KEY,
            "RowKey": doc_id,
            "id_mcflash": int(entry["id_mcflash"]),
            "file": str(entry.get("file") or ""),
            "codice": str(entry.get("codice") or ""),
            "criterio": str(entry.get("criterio") or ""),
            "updated_at": now,
        }
    return out


def main(path: Path, dry_run: bool) -> None:
    wanted = _entities(path)
    service = TableServiceClient.from_connection_string(_connection_string())
    table_name = cv_mcflash_map_table_name()
    if not dry_run:
        service.create_table_if_not_exists(table_name)
    table = service.get_table_client(table_name)

    try:
        current = {e["RowKey"]: e for e in table.query_entities(f"PartitionKey eq '{PARTITION_KEY}'")}
    except Exception:
        if not dry_run:
            raise
        current = {}  # tabella non ancora creata

    def differs(doc_id: str) -> bool:
        cur = current.get(doc_id)
        new = wanted[doc_id]
        return cur is None or any(str(cur.get(k)) != str(new[k]) for k in ("id_mcflash", "file", "codice", "criterio"))

    to_upsert = [d for d in wanted if differs(d)]
    to_delete = [d for d in current if d not in wanted]
    print(f"Tabella {table_name}: {len(current)} righe | JSON: {len(wanted)} voci | "
          f"da scrivere: {len(to_upsert)} | da cancellare: {len(to_delete)}")
    for d in to_delete:
        print(f"  cancella {d} (id {current[d].get('id_mcflash')})")
    if dry_run:
        print("[DRY-RUN] Nessuna modifica.")
        return

    for d in to_upsert:
        table.upsert_entity(wanted[d], mode=UpdateMode.REPLACE)
    for d in to_delete:
        table.delete_entity(PARTITION_KEY, d)
    print("Fatto.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(args.file, args.dry_run)

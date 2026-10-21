"""
Carica su Azure Table storage la tabella CV -> MCFlash ricostruita in locale.

La tabella (vedi infra/cv_mcflash_map.py) e' la sorgente: la leggono la ricerca
e la dashboard, e la dashboard ci scrive i CV confermati o collegati
(criterio "dashboard" / "dashboard-ricerca"). Il JSON (scripts/cv_mcflash_map.json,
da build_cv_mcflash_map.py, solo in locale) porta le righe ricostruite: questo
script le inserisce/aggiorna e cancella le righe ricostruite che nel JSON non
ci sono piu'. Le righe della dashboard non si toccano mai: una voce del JSON
che le contraddice (stesso CV, o stesso candidato con un altro CV) si salta.

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

from infra.cv_mcflash_map import (  # noqa: E402
    PARTITION_KEY, _connection_string, cv_mcflash_map_table_name, is_dashboard_row,
)

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


def plan_map(wanted: dict[str, dict], current: dict[str, dict]) -> tuple[list[str], list[str], list[str]]:
    """(da scrivere, da cancellare, saltate) per la partizione "cv".

    Le righe della dashboard restano: non si cancellano, non si sovrascrivono, e
    un candidato collegato dalla dashboard non riceve un altro CV dal JSON.
    """
    dashboard = {d: e for d, e in current.items() if is_dashboard_row(e)}
    dashboard_ids = {str(e.get("id_mcflash")) for e in dashboard.values()}
    skipped = [d for d, e in wanted.items() if d in dashboard or str(e["id_mcflash"]) in dashboard_ids]
    keep = {d: e for d, e in wanted.items() if d not in skipped}

    def differs(doc_id: str) -> bool:
        cur = current.get(doc_id)
        new = keep[doc_id]
        return cur is None or any(str(cur.get(k)) != str(new[k]) for k in ("id_mcflash", "file", "codice", "criterio"))

    to_upsert = [d for d in keep if differs(d)]
    to_delete = [d for d in current if d not in keep and d not in dashboard]
    return to_upsert, to_delete, skipped


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

    to_upsert, to_delete, skipped = plan_map(wanted, current)
    n_dashboard = sum(1 for e in current.values() if is_dashboard_row(e))
    print(f"Tabella {table_name}: {len(current)} righe (dalla dashboard: {n_dashboard}) | JSON: {len(wanted)} voci | "
          f"da scrivere: {len(to_upsert)} | da cancellare: {len(to_delete)} | saltate: {len(skipped)}")
    for d in skipped:
        print(f"  salta {d} (id {wanted[d]['id_mcflash']}): in conflitto con un collegamento della dashboard")
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

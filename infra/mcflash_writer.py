"""Scrittura su MCFlash: endpoint reale oppure MCFlash finto.

Quale si usa:
- MCFLASH_CANDIDATI_WRITE_URL valorizzato -> HttpMCFlashWriter (endpoint reale);
- vuoto -> FakeMCFlashWriter: le scritture finiscono nella tabella
  MCFLASH_FAKE_TABLE_NAME (default "McflashFake") dello storage della Function,
  e su MCFlash vero non si scrive nulla.

Il finto si comporta come MCFlash per la dashboard: overlay_records() sovrappone
le sue righe ai record letti dall'API (i candidati creati compaiono nella lista,
gli aggiornamenti si vedono subito). La ricerca non lo usa: lavora solo su MCFlash.

Righe della tabella finta: PartitionKey "candidati", RowKey = Id MCFlash;
campi: op ("crea" | "aggiorna"), record_json, created_at, updated_at, user.
Gli Id creati partono da FAKE_ID_START, cosi' non si confondono con quelli veri.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from azure.core.exceptions import ResourceExistsError

from infra.cv_mcflash_map import _connection_string
from utils.app_settings import settings_value

try:
    from azure.data.tables import TableClient
except ImportError:  # pragma: no cover - dipende dall'ambiente di deploy
    TableClient = None

logger = logging.getLogger(__name__)

PARTITION_KEY = "candidati"
FAKE_ID_START = 900000


class MCFlashWriteError(RuntimeError):
    """Scrittura su MCFlash non riuscita."""


class MCFlashWriter(Protocol):
    mode: str

    def create(self, record: dict[str, Any], *, user: str | None = None) -> str: ...

    def update(self, id_mcflash: str, record: dict[str, Any], *, user: str | None = None) -> None: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _compact(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if v not in (None, "")}


# ---------------------------------------------------------------- finto

class FakeMCFlashWriter:
    mode = "finto"

    def __init__(self, table: Any | None = None):
        self._table = table

    @property
    def table(self) -> Any:
        if self._table is None:
            if TableClient is None:
                raise MCFlashWriteError("MCFlash finto non disponibile: installare azure-data-tables")
            name = settings_value("MCFLASH_FAKE_TABLE_NAME", default="McflashFake")
            self._table = TableClient.from_connection_string(_connection_string(), table_name=name)
            try:
                self._table.create_table()
            except ResourceExistsError:
                pass
        return self._table

    def rows(self) -> list[dict[str, Any]]:
        return list(self.table.query_entities(f"PartitionKey eq '{PARTITION_KEY}'"))

    def create(self, record: dict[str, Any], *, user: str | None = None) -> str:
        used = [int(r["RowKey"]) for r in self.rows() if str(r.get("RowKey", "")).isdigit()]
        next_id = max([FAKE_ID_START, *used]) + 1
        for candidate in range(next_id, next_id + 20):
            now = _now()
            try:
                self.table.create_entity({
                    "PartitionKey": PARTITION_KEY, "RowKey": str(candidate), "op": "crea",
                    "record_json": json.dumps(_compact(record), ensure_ascii=False),
                    "created_at": now, "updated_at": now, "user": user or "",
                })
                logger.info("MCFlash finto: creato candidato id=%s", candidate)
                return str(candidate)
            except ResourceExistsError:
                continue  # creato nel frattempo da un'altra richiesta: si prova il successivo
        raise MCFlashWriteError("MCFlash finto: nessun Id libero")

    def update(self, id_mcflash: str, record: dict[str, Any], *, user: str | None = None) -> None:
        existing = None
        try:
            existing = self.table.get_entity(PARTITION_KEY, str(id_mcflash))
        except Exception:
            existing = None
        merged = {**json.loads((existing or {}).get("record_json") or "{}"), **_compact(record)}
        now = _now()
        self.table.upsert_entity({
            "PartitionKey": PARTITION_KEY, "RowKey": str(id_mcflash),
            "op": (existing or {}).get("op") or "aggiorna",
            "record_json": json.dumps(merged, ensure_ascii=False),
            "created_at": (existing or {}).get("created_at") or now, "updated_at": now, "user": user or "",
        })
        logger.info("MCFlash finto: aggiornato candidato id=%s", id_mcflash)

    def delete(self, id_mcflash: str) -> None:
        self.table.delete_entity(PARTITION_KEY, str(id_mcflash))


def overlay_records(records: list[dict[str, Any]], fake_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Record MCFlash con sopra le scritture del finto: aggiornamenti fusi, creati aggiunti."""
    by_id = {str(r.get("id") or r.get("Id")): r for r in records}
    out = list(records)
    for row in fake_rows:
        rid = str(row.get("RowKey"))
        data = json.loads(row.get("record_json") or "{}")
        data.pop("email", None)
        data.pop("telefono", None)
        if rid in by_id:
            target = by_id[rid]
            idx = out.index(target)
            out[idx] = {**target, **data}
        elif row.get("op") == "crea":
            out.append({"id": int(rid) if rid.isdigit() else rid, **data,
                        "abilitato": True, "isTalentUp": False, "finto": True})
    return out


# ---------------------------------------------------------------- reale

class HttpMCFlashWriter:
    """Endpoint di scrittura MCFlash. Payload e risposta sono un'ipotesi da
    verificare quando arriva la specifica: POST <url> crea e restituisce
    {"id": ...}; PUT <url>/<id> aggiorna. Stessa autenticazione della lettura."""

    mode = "reale"

    def __init__(self, url: str, api_key: str, timeout_seconds: int = 30):
        self.url, self.api_key, self.timeout = url.rstrip("/"), api_key, timeout_seconds

    def _send(self, method: str, url: str, record: dict[str, Any]) -> Any:
        req = Request(url, method=method, data=json.dumps(_compact(record)).encode("utf-8"),
                      headers={"X-Api-Key": self.api_key, "Content-Type": "application/json",
                               "Accept": "application/json"})
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                body = resp.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")[:300]
            raise MCFlashWriteError(f"MCFlash HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise MCFlashWriteError(f"MCFlash non raggiungibile: {exc}") from exc
        return json.loads(body.decode("utf-8")) if body else None

    def create(self, record: dict[str, Any], *, user: str | None = None) -> str:
        payload = self._send("POST", self.url, record)
        new_id = (payload.get("id") or payload.get("Id")) if isinstance(payload, dict) else None
        if not new_id:
            raise MCFlashWriteError("MCFlash non ha restituito l'Id del candidato creato")
        return str(new_id)

    def update(self, id_mcflash: str, record: dict[str, Any], *, user: str | None = None) -> None:
        self._send("PUT", f"{self.url}/{id_mcflash}", record)


def get_writer() -> MCFlashWriter:
    url = settings_value("MCFLASH_CANDIDATI_WRITE_URL", default="")
    if url:
        return HttpMCFlashWriter(url, settings_value("MCFLASH_CANDIDATI_WRITE_API_KEY", default=""))
    return FakeMCFlashWriter()

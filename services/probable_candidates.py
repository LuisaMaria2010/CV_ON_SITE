"""Candidati probabili: nuovi candidati proposti dai recruiter, in attesa dell'amministrazione.

Ogni proposta e' una riga di CANDIDATI_PROBABILI_TABLE_NAME (default
"CandidatiProbabili"), RowKey = draft_id: una bozza si propone una volta sola.
La riga ha gli stessi campi di una riga MCFlash (domain/mcflash_record.build_record)
piu' lo stato della revisione. Proporre un candidato NON scrive su MCFlash e NON
tocca l'indice: il CV resta in incoming-cv-originals (nessun trigger) e la bozza
in cv-drafts. Solo quando l'amministrazione conferma la bozza (POST /api/cv/commit)
il candidato va su MCFlash e il CV viene indicizzato; a operazione completata la
riga passa ad "approvato" con l'Id MCFlash.

Stati: proposto -> approvato | scartato (scartato si puo' riproporre).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

from domain.mcflash_record import build_record
from utils.app_settings import settings_value

PARTITION_KEY = "prob"
PROPOSED, APPROVED, DISCARDED = "proposto", "approvato", "scartato"
STATES = (PROPOSED, APPROVED, DISCARDED)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProbableStore:
    def __init__(self, table: Any | None = None):
        self._table = table

    @property
    def table(self) -> Any:
        if self._table is None:
            from azure.data.tables import TableClient

            from infra.cv_mcflash_map import _connection_string

            name = settings_value("CANDIDATI_PROBABILI_TABLE_NAME", default="CandidatiProbabili")
            self._table = TableClient.from_connection_string(_connection_string(), table_name=name)
            try:
                self._table.create_table()
            except ResourceExistsError:
                pass
        return self._table

    def get(self, draft_id: str) -> dict[str, Any] | None:
        try:
            entity = self.table.get_entity(PARTITION_KEY, draft_id)
        except ResourceNotFoundError:
            return None
        return _from_entity(entity)

    def save(self, row: dict[str, Any]) -> None:
        row["updated_at"] = _now()
        entity = {k: v for k, v in row.items() if k != "draft_id" and v is not None}
        self.table.upsert_entity({**entity, "PartitionKey": PARTITION_KEY, "RowKey": row["draft_id"]})

    def list(self) -> list[dict[str, Any]]:
        return [_from_entity(e) for e in self.table.query_entities(f"PartitionKey eq '{PARTITION_KEY}'")]


def _from_entity(entity: dict[str, Any]) -> dict[str, Any]:
    row = {k: v for k, v in dict(entity).items() if k not in ("PartitionKey", "RowKey", "Timestamp", "etag")}
    row["draft_id"] = entity["RowKey"]
    return row


def missing_for_proposal(draft: dict[str, Any]) -> list[str]:
    """Il minimo per proporre: nome e cognome (quindi il trittico) e almeno una
    fonte, il CV caricato o la scheda colloquio. Seniority, site e il resto li
    puo' completare l'amministrazione."""
    edited = draft.get("edited") or {}
    missing = []
    if not edited.get("trittico") or not edited.get("full_name"):
        missing.append("nome e cognome")
    if not draft.get("original_blob") and not (edited.get("colloquio") or {}).get("testo"):
        missing.append("CV o scheda colloquio")
    return missing


def _interview(edited: dict[str, Any]) -> dict[str, Any]:
    """Note del colloquio per la riga: senza il testo intero della scheda, che
    resta nella bozza (una proprieta' della tabella arriva al massimo a 64 KB)."""
    return {k: v for k, v in (edited.get("colloquio") or {}).items() if k != "testo" and v}


def proposal_row(draft: dict[str, Any], user: str | None, existing: dict[str, Any] | None) -> dict[str, Any]:
    """Riga della tabella da una bozza: campi MCFlash + revisione."""
    now = _now()
    edited = draft.get("edited") or {}
    return {
        "draft_id": draft["draft_id"],
        **build_record(edited),
        "colloquio": json.dumps(_interview(edited), ensure_ascii=False),
        "stato": PROPOSED,
        "recruiter": user or draft.get("user") or "",
        "proposto_il": now,
        "created_at": (existing or {}).get("created_at") or now,
        "original_blob": draft.get("original_blob") or "",
        "original_filename": draft.get("original_filename") or "",
        "id_mcflash": "", "operation_id": "", "revisore": "", "revisionato_il": "",
    }


def approved_row(row: dict[str, Any], draft: dict[str, Any], op: dict[str, Any]) -> dict[str, Any]:
    """A conferma completata: i dati diventano quelli salvati dall'amministrazione."""
    edited = draft.get("edited") or {}
    return {
        **row, **build_record(edited), "colloquio": json.dumps(_interview(edited), ensure_ascii=False),
        "stato": APPROVED, "id_mcflash": str(op.get("id_mcflash") or ""), "operation_id": op.get("id") or "",
        "revisore": op.get("user") or "", "revisionato_il": op.get("completed_at") or _now(),
    }


def discarded_row(row: dict[str, Any], user: str | None) -> dict[str, Any]:
    return {**row, "stato": DISCARDED, "revisore": user or "", "revisionato_il": _now()}


def dashboard_row(row: dict[str, Any]) -> dict[str, Any]:
    """Riga per la dashboard, con i nomi della lista candidati MCFlash
    (domain/candidate_match.candidate_row): cosi' la tabella e i filtri sono gli stessi.
    In piu' le note del colloquio, se il recruiter ha incollato la scheda."""
    try:
        interview = json.loads(row.get("colloquio") or "{}")
    except ValueError:
        interview = {}
    return {
        "id": row["draft_id"],
        "draft_id": row["draft_id"],
        "trittico": row.get("nome") or "",
        "nome_completo": row.get("nomi") or "",
        "ruolo": row.get("ruolo"),
        "seniority": row.get("seniority"),
        "sede": row.get("sede"),
        "eta": row.get("eta"),
        "lingue": row.get("lingue"),
        "disponibilita": row.get("disponibilita"),
        "budget": row.get("budget"),
        "site": row.get("site"),
        "email": row.get("email"),
        "telefono": row.get("telefono"),
        "stato": row.get("stato") or PROPOSED,
        "recruiter": row.get("recruiter") or "",
        "proposto_il": row.get("proposto_il") or "",
        "revisore": row.get("revisore") or "",
        "revisionato_il": row.get("revisionato_il") or "",
        "id_mcflash": row.get("id_mcflash") or "",
        "operation_id": row.get("operation_id") or "",
        "colloquio": interview,
        "data_colloquio": interview.get("data_colloquio") or "",
        "ral_attesa": interview.get("ral_attesa") or "",
        # Il CV caricato dal recruiter: NON indicizzato, sta solo tra gli originali.
        "cv": {"file": row["original_filename"]} if row.get("original_filename") else None,
    }

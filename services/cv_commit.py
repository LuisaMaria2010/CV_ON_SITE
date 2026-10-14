"""Conferma di una bozza della dashboard: storage, MCFlash, indice, mappa.

Ogni conferma e' un'operazione (riga di CV_OPERATIONS_TABLE_NAME, default
"CvOperations") con un passo per colonna. advance() esegue i passi in ordine e
si ferma al primo che fallisce o che deve aspettare; rieseguirla riparte da
li' ("Riprova"), senza ripetere i passi gia' fatti:

1. blob    - registra il CV originale (gia' in incoming-cv-originals). Per un
             aggiornamento copia il CV MC attuale in cv-history/<id>/<data>_<file>.
2. mcflash - crea o aggiorna la riga (endpoint reale o MCFlash finto). L'Id
             creato si salva subito nell'operazione: un nuovo tentativo non crea
             un secondo candidato.
3. indice  - genera il CV MC (.docx, per ora: il PDF arrivera' con la
             conversione) e lo deposita in incoming-cv: la pipeline esistente lo
             indicizza. Stesso nome del CV attuale -> stesso document_id e
             versione +1, l'indice sostituisce il vecchio CV. Il vecchio file,
             gia' copiato nello storico, si toglie da incoming-cv.
4. mappa   - quando DocumentRegistry segna il nuovo file come indicizzato,
             aggiorna CvMcflashMap (document_id -> Id MCFlash). Fino ad allora
             il passo resta "in_attesa" e l'operazione "in_corso". "Nuovo" vuol
             dire versione piu' alta di quella registrata prima del caricamento:
             con lo stesso nome file la riga della versione vecchia ha lo stesso
             percorso ed e' gia' "processed", e non va scambiata per la nuova.

Se MCFlash fallisce l'indice non viene toccato.

Log: ogni passo emette l'evento "cv_commit_step" con operation_id, passo ed
esito, e l'operazione conserva l'ultimo request_id. Dal monitor si cercano
nei log della Function prima per operation_id, poi per request_id.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Callable

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

from core.config import settings
from domain.mcflash_record import build_record, missing_for_create
from domain.normalizer import TextNormalizer
from services.cv_mcflash_renderer import context_from_form, render_docx
from utils.app_settings import settings_value
from utils.observability import track_event
from utils.request_context import get_request_id

logger = logging.getLogger(__name__)

STEPS = ("blob", "mcflash", "indice", "mappa")
OK, TODO, WAITING, ERROR = "ok", "da_fare", "in_attesa", "errore"
RUNNING, DONE, FAILED = "in_corso", "completata", "fallita"
PARTITION_KEY = "op"


def history_container() -> str:
    return settings_value("STORAGE_CONTAINER_CV_HISTORY", default="cv-history")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stem(name: str | None) -> str:
    base = PurePosixPath((name or "").replace("\\", "/")).name
    return (base.rsplit(".", 1)[0] if "." in base else base).strip().casefold()


# ---------------------------------------------------------------- dipendenze

@dataclass
class CommitDeps:
    """Tutto cio' che la conferma tocca: i test passano dei finti."""
    storage: Any                                   # StorageService (async)
    writer: Any                                    # MCFlashWriter
    registry_docs: Callable[[], dict[str, dict]]   # DocumentRegistry.list_documents
    normalize_doc_id: Callable[[str], str]         # DocumentRegistry._normalize_document_id
    map_entries: Callable[[], list[dict]]          # cv_mcflash_map.read_file_entries
    map_upsert: Callable[..., Any]                 # cv_mcflash_map.upsert_entry
    map_delete: Callable[[str], None]              # cv_mcflash_map.delete_entry
    save_op: Callable[[dict], None]                # OperationStore.save


def default_deps(store: "OperationStore") -> CommitDeps:
    from core.dependencies import storage
    from infra import cv_mcflash_map
    from infra.document_registry import DocumentRegistry
    from infra.mcflash_writer import get_writer

    registry = DocumentRegistry.from_settings()
    return CommitDeps(
        storage=storage, writer=get_writer(), registry_docs=registry.list_documents,
        normalize_doc_id=registry._normalize_document_id, map_entries=cv_mcflash_map.read_file_entries,
        map_upsert=cv_mcflash_map.upsert_entry, map_delete=cv_mcflash_map.delete_entry, save_op=store.save,
    )


# ---------------------------------------------------------------- operazioni

class OperationStore:
    def __init__(self, table: Any | None = None):
        self._table = table

    @property
    def table(self) -> Any:
        if self._table is None:
            from azure.data.tables import TableClient

            from infra.cv_mcflash_map import _connection_string

            name = settings_value("CV_OPERATIONS_TABLE_NAME", default="CvOperations")
            self._table = TableClient.from_connection_string(_connection_string(), table_name=name)
            try:
                self._table.create_table()
            except ResourceExistsError:
                pass
        return self._table

    def get(self, op_id: str) -> dict[str, Any] | None:
        try:
            entity = self.table.get_entity(PARTITION_KEY, op_id)
        except ResourceNotFoundError:
            return None
        return _from_entity(entity)

    def save(self, op: dict[str, Any]) -> None:
        op["updated_at"] = _now()
        entity = {k: v for k, v in op.items() if k != "id" and v is not None}
        self.table.upsert_entity({**entity, "PartitionKey": PARTITION_KEY, "RowKey": op["id"]})

    def list(self) -> list[dict[str, Any]]:
        return [_from_entity(e) for e in self.table.query_entities(f"PartitionKey eq '{PARTITION_KEY}'")]


def _from_entity(entity: dict[str, Any]) -> dict[str, Any]:
    op = {k: v for k, v in dict(entity).items() if k not in ("PartitionKey", "RowKey", "Timestamp", "etag")}
    op["id"] = entity["RowKey"]
    return op


def new_operation(op_id: str, draft: dict[str, Any], user: str | None) -> dict[str, Any]:
    edited = draft.get("edited") or {}
    now = _now()
    return {
        "id": op_id, "draft_id": draft["draft_id"], "kind": draft["kind"],
        "id_mcflash": draft.get("id_mcflash") or "", "user": user or draft.get("user") or "",
        "trittico": edited.get("trittico") or "", "nome": edited.get("full_name") or "",
        "filename": draft.get("filename") or "", "created_at": now, "updated_at": now, "completed_at": "",
        "status": RUNNING, "error": "", "error_step": "",
        **{f"step_{s}": TODO for s in STEPS},
        "original_blob": "", "history_blob": "", "old_incoming_blob": "", "incoming_blob": "",
        "document_id": "", "previous_version": "", "mcflash_mode": "", "last_request_id": "",
    }


# ---------------------------------------------------------------- passi

def _current_incoming_blob(id_mcflash: str, deps: CommitDeps) -> tuple[str | None, str | None]:
    """(blob in incoming-cv, document_id) del CV MC attuale del candidato, se c'e'."""
    entry = next((e for e in deps.map_entries() if e.get("id_mcflash") == str(id_mcflash)), None)
    if not entry:
        return None, None
    doc_id = deps.normalize_doc_id(entry["document_id"])
    doc = deps.registry_docs().get(doc_id) or {}
    prefix = f"/{settings.storage_container_incoming}/"
    source = doc.get("source_path") or ""
    if source.startswith(prefix):
        return source[len(prefix):], doc_id
    file = entry.get("file")
    return (file or None), doc_id


async def _step_blob(op: dict, draft: dict, deps: CommitDeps) -> str:
    op["original_blob"] = draft.get("original_blob") or ""
    if op["kind"] != "aggiorna" or op.get("history_blob"):
        return OK
    blob, _ = _current_incoming_blob(op["id_mcflash"], deps)
    if not blob:
        return OK  # nessun CV attuale: niente da conservare
    try:
        data = await deps.storage.download_bytes(blob, container=settings.storage_container_incoming)
    except ResourceNotFoundError:
        logger.warning("Commit %s: CV attuale %s non trovato in incoming-cv", op["id"], blob)
        return OK
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    history = f"{op['id_mcflash']}/{stamp}_{PurePosixPath(blob).name}"
    await deps.storage.upload_bytes(data, history, container=history_container())
    op["history_blob"], op["old_incoming_blob"] = history, blob
    return OK


async def _step_mcflash(op: dict, draft: dict, deps: CommitDeps) -> str:
    record = build_record(draft.get("edited") or {})
    op["mcflash_mode"] = getattr(deps.writer, "mode", "")
    if op["kind"] == "crea":
        if not op.get("id_mcflash"):
            missing = missing_for_create(record)
            if missing:
                raise ValueError(f"Mancano dati per creare il candidato su MCFlash: {', '.join(missing)}")
            op["id_mcflash"] = await asyncio.to_thread(deps.writer.create, record, user=op.get("user"))
            deps.save_op(op)  # l'Id va salvato subito: un nuovo tentativo non deve ricreare il candidato
        return OK
    await asyncio.to_thread(deps.writer.update, op["id_mcflash"], record, user=op.get("user"))
    return OK


async def _taken_stems(op: dict, deps: CommitDeps) -> set[str]:
    names = await deps.storage.list_names(container=settings.storage_container_incoming)
    names += [e.get("file") or "" for e in deps.map_entries() if e.get("id_mcflash") != str(op["id_mcflash"])]
    own = {_stem(op.get("old_incoming_blob")), _stem(op.get("incoming_blob"))}
    return {_stem(n) for n in names if n} - own - {""}


async def _step_indice(op: dict, draft: dict, deps: CommitDeps) -> str:
    stem = PurePosixPath((op.get("filename") or "").replace("\\", "/")).name.rsplit(".", 1)[0].strip()
    if not stem:
        raise ValueError("Manca il nome del file CV")
    name = f"{stem}.docx"
    if not op.get("incoming_blob") and _stem(name) in await _taken_stems(op, deps):
        raise ValueError(f"Il nome «{stem}» è già usato da un altro CV: cambialo nella bozza e riprova")
    context = context_from_form(draft.get("edited") or {})
    docx = await asyncio.to_thread(render_docx, context)
    document_id = deps.normalize_doc_id(TextNormalizer.normalize_document_id(name))
    if not op.get("incoming_blob"):
        # Versione gia' indicizzata prima di questo caricamento (aggiornamento con lo stesso nome).
        previous = (deps.registry_docs().get(document_id) or {}).get("version")
        op["previous_version"] = str(previous or "")
    await deps.storage.upload_bytes(docx, name, container=settings.storage_container_incoming)
    op["incoming_blob"], op["document_id"] = name, document_id
    old = op.get("old_incoming_blob")
    if old and old != name:
        try:
            await deps.storage.delete(old, container=settings.storage_container_incoming)
        except ResourceNotFoundError:
            pass
    return OK


async def _step_mappa(op: dict, draft: dict, deps: CommitDeps) -> str:
    expected_source = f"/{settings.storage_container_incoming}/{op['incoming_blob']}"
    # Il document_id lo decide l'ingestione (DocumentRegistry.resolve_document_id):
    # di norma quello previsto, ma se il nome ripulito e' gia' di un altro CV e'
    # il nome file originale. Si cerca quindi la riga del file caricato.
    docs = deps.registry_docs()
    document_id, doc = op["document_id"], docs.get(op["document_id"]) or {}
    if doc.get("source_path") != expected_source:
        document_id, doc = next(((k, d) for k, d in docs.items() if d.get("source_path") == expected_source),
                                (document_id, {}))
    previous = int(op.get("previous_version") or 0) if document_id == op["document_id"] else 0
    if (doc.get("source_path") != expected_source or doc.get("status") != "processed"
            or int(doc.get("version") or 0) <= previous):
        return WAITING  # l'ingestione non ha ancora indicizzato il nuovo file
    op["document_id"] = document_id
    for entry in deps.map_entries():
        other = deps.normalize_doc_id(entry.get("document_id") or "")
        if entry.get("id_mcflash") == str(op["id_mcflash"]) and other != op["document_id"]:
            deps.map_delete(entry["document_id"])  # un solo CV per candidato
    deps.map_upsert(op["document_id"], op["id_mcflash"], file=op["incoming_blob"], codice=op.get("trittico") or "")
    return OK


_HANDLERS = {"blob": _step_blob, "mcflash": _step_mcflash, "indice": _step_indice, "mappa": _step_mappa}


def _log_step(op: dict[str, Any], step: str, state: str, error: str = "") -> None:
    track_event("cv_commit_step", request_id=get_request_id(), properties={
        "operation_id": op["id"], "step": step, "state": state, "kind": op.get("kind"),
        "id_mcflash": op.get("id_mcflash"), "document_id": op.get("document_id"), "error": error[:200],
    })


async def advance(op: dict[str, Any], draft: dict[str, Any], deps: CommitDeps) -> dict[str, Any]:
    """Esegue i passi non ancora fatti, in ordine. Salva l'operazione dopo ogni passo."""
    op["error"], op["error_step"] = "", ""
    op["last_request_id"] = get_request_id() or op.get("last_request_id") or ""
    for step in STEPS:
        key = f"step_{step}"
        if op.get(key) == OK:
            continue
        try:
            state = await _HANDLERS[step](op, draft, deps)
        except Exception as exc:
            logger.exception("Commit %s: passo %s fallito", op["id"], step)
            op[key], op["status"] = ERROR, FAILED
            op["error"], op["error_step"] = str(exc)[:500], step
            _log_step(op, step, ERROR, op["error"])
            deps.save_op(op)
            return op
        op[key] = state
        _log_step(op, step, state)
        if state != OK:
            op["status"] = RUNNING
            deps.save_op(op)
            return op
        deps.save_op(op)
    op["status"], op["completed_at"] = DONE, _now()
    deps.save_op(op)
    return op

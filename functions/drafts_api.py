"""Bozze della dashboard CV -> MCFlash: dati estratti da un CV e versione corretta.

Ogni bozza e' un JSON nel container STORAGE_CONTAINER_DRAFTS (default "cv-drafts"),
blob "<draft_id>.json". Contiene l'estratto originale (mai sovrascritto: e' il
backup) e la versione modificata dall'utente, cosi' si vede cosa e' stato corretto.

PUT    /api/drafts/{draft_id}
    Body: {kind: crea|aggiorna, id_mcflash?, original_blob?, original_filename?,
           extracted?, edited, filename?, user?, origin?}
    Crea o aggiorna la bozza. `extracted` e `origin` (admin|recruiter) si
    registrano solo la prima volta.
    Output: la bozza salvata.
GET    /api/drafts/{draft_id}
    La bozza completa; 404 se non esiste.
GET    /api/drafts?kind=&id_mcflash=&origin=&tutte=
    Bozze piu' recenti (riassunto), per riprenderle dalla dashboard. Quelle gia'
    confermate (operation_id) o proposte come candidato probabile
    (submitted_at, vedi probabili_api) si escludono, salvo tutte=true.
    Una bozza confermata non si modifica piu': il PUT risponde 400.
DELETE /api/drafts/{draft_id}
    Scarta la bozza (il CV originale resta in incoming-cv-originals).
GET    /api/drafts/{draft_id}/original
    Il file CV originale della bozza (PDF/DOCX), per l'anteprima.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

import azure.functions as func
from azure.core.exceptions import ResourceNotFoundError

from core.config import settings
from core.dependencies import storage
from core.errors import InvalidInputError, NotFoundError
from domain.candidate_identity import trittico as compute_trittico
from utils.app_settings import settings_value
from utils.http_errors import http_error_handler
from utils.http_params import body_params

logger = logging.getLogger(__name__)

bp = func.Blueprint()

KINDS = ("crea", "aggiorna")
# Chi ha aperto la bozza: l'amministrazione (anche le bozze di prima, senza
# origine) o un recruiter, che la propone come candidato probabile.
ORIGIN_ADMIN, ORIGIN_RECRUITER = "admin", "recruiter"
_DRAFT_ID = re.compile(r"^[0-9a-f]{32}$")
# Una bozza e' testo strutturato: oltre questa soglia c'e' qualcosa che non va.
_MAX_DRAFT_BYTES = 2 * 1024 * 1024
_MAX_LISTED = 50
_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".txt": "text/plain; charset=utf-8",
}


def drafts_container() -> str:
    return settings_value("STORAGE_CONTAINER_DRAFTS", default="cv-drafts")


def _draft_id(req: func.HttpRequest) -> str:
    draft_id = str(req.route_params.get("draft_id") or "").strip().lower()
    if not _DRAFT_ID.match(draft_id):
        raise InvalidInputError("draft_id non valido: servono 32 caratteri esadecimali (uuid4().hex)")
    return draft_id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def load_draft(draft_id: str) -> dict[str, Any] | None:
    try:
        raw = await storage.download_bytes(f"{draft_id}.json", container=drafts_container())
    except ResourceNotFoundError:
        return None
    return json.loads(raw.decode("utf-8"))


def summarize(draft: dict[str, Any]) -> dict[str, Any]:
    """Riga dell'elenco bozze: chi e', che CV, quando."""
    edited = draft.get("edited") or {}
    first, last = edited.get("first_name"), edited.get("last_name")
    name = " ".join(p for p in (first, last) if p) or edited.get("full_name") or ""
    return {
        "draft_id": draft.get("draft_id"),
        "kind": draft.get("kind"),
        "id_mcflash": draft.get("id_mcflash"),
        "nome": name,
        "trittico": compute_trittico(first, last) or edited.get("trittico") or "",
        "original_filename": draft.get("original_filename"),
        "operation_id": draft.get("operation_id"),
        "origin": draft.get("origin") or ORIGIN_ADMIN,
        "submitted_at": draft.get("submitted_at"),
        "created_at": draft.get("created_at"),
        "updated_at": draft.get("updated_at"),
    }


def merge_draft(existing: dict[str, Any] | None, body: dict[str, Any], draft_id: str, now: str) -> dict[str, Any]:
    """Nuova versione della bozza. L'estratto e il file originale restano quelli
    della prima volta: sono il backup di cosa ha prodotto l'estrazione."""
    kind = str(body.get("kind") or (existing or {}).get("kind") or "").strip()
    if kind not in KINDS:
        raise InvalidInputError(f"'kind' deve essere uno di: {', '.join(KINDS)}")
    edited = body.get("edited")
    if not isinstance(edited, dict):
        raise InvalidInputError("'edited' (i dati del form) e' obbligatorio ed e' un oggetto")
    extracted = (existing or {}).get("extracted")
    if extracted is None:
        extracted = body.get("extracted") if isinstance(body.get("extracted"), dict) else None
    id_mcflash = body.get("id_mcflash", (existing or {}).get("id_mcflash"))
    if kind == "aggiorna" and not id_mcflash:
        raise InvalidInputError("Una bozza 'aggiorna' richiede 'id_mcflash'")

    def keep(field: str) -> Any:
        return (existing or {}).get(field) or body.get(field)

    return {
        "draft_id": draft_id,
        "kind": kind,
        "id_mcflash": str(id_mcflash) if id_mcflash not in (None, "") else None,
        "original_blob": keep("original_blob"),
        "original_filename": keep("original_filename"),
        "filename": body.get("filename", (existing or {}).get("filename")),
        "user": body.get("user") or (existing or {}).get("user"),
        "extracted": extracted,
        "edited": edited,
        "created_at": (existing or {}).get("created_at") or now,
        "updated_at": now,
        "operation_id": (existing or {}).get("operation_id"),
        "committed_at": (existing or {}).get("committed_at"),
        "origin": keep("origin"),
        "submitted_at": (existing or {}).get("submitted_at"),
    }


@bp.route(route="drafts/{draft_id}", methods=["PUT"])
@http_error_handler
async def save_draft(req: func.HttpRequest):
    draft_id = _draft_id(req)
    if len(req.get_body() or b"") > _MAX_DRAFT_BYTES:
        raise InvalidInputError("Bozza troppo grande")
    body = body_params(req)
    if not body:
        raise InvalidInputError("Body JSON mancante o non valido")
    existing = await load_draft(draft_id)
    if existing and existing.get("operation_id"):
        raise InvalidInputError("La bozza è già stata confermata e non si modifica più")
    draft = merge_draft(existing, body, draft_id, _now())
    await _write(draft)
    return draft


async def _write(draft: dict[str, Any]) -> None:
    await storage.upload_bytes(json.dumps(draft, ensure_ascii=False).encode("utf-8"),
                               f"{draft['draft_id']}.json", container=drafts_container())


async def mark_committed(draft: dict[str, Any], operation_id: str, *, committed_at: str | None = None) -> None:
    """Lega la bozza alla sua operazione di conferma (e, a operazione finita, la data)."""
    draft["operation_id"] = operation_id
    if committed_at:
        draft["committed_at"] = committed_at
    await _write(draft)


@bp.route(route="drafts/{draft_id}", methods=["GET"])
@http_error_handler
async def get_draft(req: func.HttpRequest):
    draft_id = _draft_id(req)
    draft = await load_draft(draft_id)
    if draft is None:
        raise NotFoundError(f"Bozza {draft_id} non trovata")
    return draft


@bp.route(route="drafts/{draft_id}", methods=["DELETE"])
@http_error_handler
async def delete_draft(req: func.HttpRequest):
    draft_id = _draft_id(req)
    if await load_draft(draft_id) is None:
        raise NotFoundError(f"Bozza {draft_id} non trovata")
    await storage.delete(f"{draft_id}.json", container=drafts_container())
    return {"draft_id": draft_id, "deleted": True}


@bp.route(route="drafts", methods=["GET"])
@http_error_handler
async def list_drafts(req: func.HttpRequest):
    kind = (req.params.get("kind") or "").strip()
    id_mcflash = (req.params.get("id_mcflash") or "").strip()
    origin = (req.params.get("origin") or "").strip()
    include_committed = (req.params.get("tutte") or "").strip().lower() in ("1", "true", "si", "sì")
    out = []
    for name in await storage.list_names(container=drafts_container()):
        if not name.endswith(".json"):
            continue
        try:
            draft = await load_draft(name[:-5])
        except Exception:
            logger.exception("Drafts: unreadable draft %s", name)
            continue
        if not draft or (kind and draft.get("kind") != kind) or (id_mcflash and draft.get("id_mcflash") != id_mcflash):
            continue
        if origin and (draft.get("origin") or ORIGIN_ADMIN) != origin:
            continue
        if (draft.get("operation_id") or draft.get("submitted_at")) and not include_committed:
            continue
        out.append(summarize(draft))
    out.sort(key=lambda d: d.get("updated_at") or "", reverse=True)
    return out[:_MAX_LISTED]


@bp.route(route="drafts/{draft_id}/original", methods=["GET"])
@http_error_handler
async def draft_original(req: func.HttpRequest):
    draft_id = _draft_id(req)
    draft = await load_draft(draft_id)
    if draft is None:
        raise NotFoundError(f"Bozza {draft_id} non trovata")
    blob = draft.get("original_blob")
    if not blob:
        raise NotFoundError("La bozza non ha un CV originale collegato")
    try:
        data = await storage.download_bytes(blob, container=settings.storage_container_original_uploads)
    except ResourceNotFoundError as exc:
        raise NotFoundError("CV originale non trovato nello storage") from exc
    filename = draft.get("original_filename") or PurePosixPath(blob).name
    content_type = _CONTENT_TYPES.get(PurePosixPath(filename).suffix.lower(), "application/octet-stream")
    safe = re.sub(r'[^A-Za-z0-9._ -]', "_", filename)
    return func.HttpResponse(body=data, status_code=200, mimetype=content_type.split(";")[0],
                             headers={"Content-Disposition": f'inline; filename="{safe}"'})

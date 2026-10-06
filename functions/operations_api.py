"""Conferma delle bozze e monitor delle operazioni (services/cv_commit.py).

POST /api/cv/commit
    Body: {draft_id, user?}. Crea l'operazione e la fa avanzare. Una bozza si
    conferma una volta sola: una seconda chiamata restituisce la stessa operazione.
GET  /api/operations/{op_id}
    Stato dell'operazione. Se aspetta l'indicizzazione, ricontrolla e avanza.
POST /api/operations/{op_id}/retry
    Riparte dal passo fallito (o in attesa).
GET  /api/operations?stato=&utente=&da=
    Elenco, dalla piu' recente (da = data ISO minima di creazione).
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import azure.functions as func

from core.errors import InvalidInputError, NotFoundError
from functions import dashboard_api, drafts_api
from services import cv_commit
from utils.http_errors import http_error_handler
from utils.http_params import body_params

logger = logging.getLogger(__name__)

bp = func.Blueprint()

_store: cv_commit.OperationStore | None = None


def _get_store() -> cv_commit.OperationStore:
    global _store
    if _store is None:
        _store = cv_commit.OperationStore()
    return _store


def _deps() -> cv_commit.CommitDeps:
    return cv_commit.default_deps(_get_store())


async def _draft_for(op: dict[str, Any]) -> dict[str, Any]:
    draft = await drafts_api.load_draft(op["draft_id"])
    if draft is None:
        raise NotFoundError(f"La bozza {op['draft_id']} dell'operazione non esiste più")
    return draft


async def _advance(op: dict[str, Any]) -> dict[str, Any]:
    draft = await _draft_for(op)
    before = op.get("step_mcflash")
    op = await cv_commit.advance(op, draft, _deps())
    if op.get("step_mcflash") == cv_commit.OK and before != cv_commit.OK:
        dashboard_api.invalidate_candidates_cache()  # il candidato creato/aggiornato si vede subito
    if op["status"] == cv_commit.DONE and not draft.get("committed_at"):
        await drafts_api.mark_committed(draft, op["id"], committed_at=op.get("completed_at"))
    return op


def _op_id(req: func.HttpRequest) -> str:
    op_id = str(req.route_params.get("op_id") or "").strip().lower()
    if not op_id.isalnum() or len(op_id) != 32:
        raise InvalidInputError("Id operazione non valido")
    return op_id


@bp.route(route="cv/commit", methods=["POST"])
@http_error_handler
async def cv_commit_route(req: func.HttpRequest):
    body = body_params(req)
    draft_id = str(body.get("draft_id") or "").strip().lower()
    draft = await drafts_api.load_draft(draft_id) if draft_id else None
    if draft is None:
        raise NotFoundError(f"Bozza {draft_id or '(mancante)'} non trovata")
    store = _get_store()
    if draft.get("operation_id"):
        op = store.get(draft["operation_id"])
        if op is not None:
            return op if op["status"] == cv_commit.DONE else await _advance(op)
    edited = draft.get("edited") or {}
    if not edited.get("trittico") or not edited.get("full_name"):
        raise InvalidInputError("Servono nome e cognome (e quindi il trittico) prima di confermare")
    if draft["kind"] == "aggiorna" and not draft.get("id_mcflash"):
        raise InvalidInputError("Una bozza 'aggiorna' richiede l'Id MCFlash")
    op = cv_commit.new_operation(uuid.uuid4().hex, draft, body.get("user"))
    store.save(op)
    await drafts_api.mark_committed(draft, op["id"])
    logger.info("Commit avviato op=%s draft=%s kind=%s", op["id"], draft["draft_id"], draft["kind"])
    return await _advance(op)


@bp.route(route="operations/{op_id}", methods=["GET"])
@http_error_handler
async def get_operation(req: func.HttpRequest):
    op = _get_store().get(_op_id(req))
    if op is None:
        raise NotFoundError("Operazione non trovata")
    if op["status"] == cv_commit.RUNNING:
        op = await _advance(op)
    return op


@bp.route(route="operations/{op_id}/retry", methods=["POST"])
@http_error_handler
async def retry_operation(req: func.HttpRequest):
    op = _get_store().get(_op_id(req))
    if op is None:
        raise NotFoundError("Operazione non trovata")
    if op["status"] == cv_commit.DONE:
        return op
    return await _advance(op)


@bp.route(route="operations", methods=["GET"])
@http_error_handler
async def list_operations(req: func.HttpRequest):
    stato = (req.params.get("stato") or "").strip()
    utente = (req.params.get("utente") or "").strip().casefold()
    da = (req.params.get("da") or "").strip()
    ops = _get_store().list()
    ops = [o for o in ops
           if (not stato or o.get("status") == stato)
           and (not utente or utente in str(o.get("user") or "").casefold())
           and (not da or str(o.get("created_at") or "") >= da)]
    ops.sort(key=lambda o: o.get("created_at") or "", reverse=True)
    return ops[:500]

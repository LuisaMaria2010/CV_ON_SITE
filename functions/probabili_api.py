"""Candidati probabili proposti dai recruiter (services/probable_candidates.py).

Nessuna di queste rotte scrive su MCFlash o sull'indice.

POST /api/candidati-probabili
    Body: {draft_id, user?}. Propone il candidato della bozza (kind "crea", non
    confermata): scrive la riga in CandidatiProbabili e segna la bozza come
    inviata (submitted_at). Riproporre una bozza gia' proposta restituisce la
    stessa riga; una bozza scartata si puo' riproporre; una approvata no.
GET  /api/candidati-probabili?stato=&recruiter=
    Le proposte (stato = proposto|approvato|scartato, facoltativo; recruiter =
    solo quelle di chi le ha caricate), dalla piu' recente, con i campi della
    lista candidati MCFlash e le note del colloquio.
POST /api/candidati-probabili/{draft_id}/scarta
    Body: {user?}. L'amministrazione rifiuta la proposta.

L'approvazione non ha una rotta: l'amministrazione apre la bozza e la conferma
con POST /api/cv/commit; a operazione completata operations_api chiama
on_commit_done, che segna la riga "approvato" con l'Id MCFlash.
"""
from __future__ import annotations

import logging
from typing import Any

import azure.functions as func

from core.errors import InvalidInputError, NotFoundError
from functions import drafts_api
from services import probable_candidates as pc
from utils.http_errors import http_error_handler
from utils.http_params import body_params

logger = logging.getLogger(__name__)

bp = func.Blueprint()

_store: pc.ProbableStore | None = None


def _get_store() -> pc.ProbableStore:
    global _store
    if _store is None:
        _store = pc.ProbableStore()
    return _store


def _valid_draft_id(raw: Any) -> str:
    draft_id = str(raw or "").strip().lower()
    if not drafts_api._DRAFT_ID.match(draft_id):
        raise InvalidInputError("draft_id non valido: servono 32 caratteri esadecimali (uuid4().hex)")
    return draft_id


@bp.route(route="candidati-probabili", methods=["POST"])
@http_error_handler
async def propose(req: func.HttpRequest):
    body = body_params(req)
    draft_id = _valid_draft_id(body.get("draft_id"))
    draft = await drafts_api.load_draft(draft_id)
    if draft is None:
        raise NotFoundError(f"Bozza {draft_id} non trovata")
    if draft.get("kind") != "crea":
        raise InvalidInputError("Si propone solo un candidato nuovo (bozza 'crea')")
    if draft.get("operation_id"):
        raise InvalidInputError("La bozza è già stata confermata su MCFlash")
    missing = pc.missing_for_proposal(draft)
    if missing:
        raise InvalidInputError("Per proporre il candidato mancano: " + ", ".join(missing))
    store = _get_store()
    existing = store.get(draft_id)
    if existing and existing.get("stato") == pc.APPROVED:
        raise InvalidInputError("Il candidato è già stato approvato")
    if existing and existing.get("stato") == pc.PROPOSED and draft.get("submitted_at"):
        return pc.dashboard_row(existing)
    row = pc.proposal_row(draft, body.get("user"), existing)
    store.save(row)
    draft["submitted_at"] = row["proposto_il"]
    await drafts_api._write(draft)
    logger.info("Candidato probabile proposto draft=%s trittico=%s", draft_id, row.get("nome"))
    return pc.dashboard_row(row)


@bp.route(route="candidati-probabili", methods=["GET"])
@http_error_handler
async def list_probables(req: func.HttpRequest):
    stato = (req.params.get("stato") or "").strip()
    recruiter = (req.params.get("recruiter") or "").strip().casefold()
    if stato and stato not in pc.STATES:
        raise InvalidInputError(f"'stato' deve essere uno di: {', '.join(pc.STATES)}")
    rows = [r for r in _get_store().list()
            if (not stato or r.get("stato") == stato)
            and (not recruiter or str(r.get("recruiter") or "").casefold() == recruiter)]
    rows.sort(key=lambda r: r.get("proposto_il") or "", reverse=True)
    return [pc.dashboard_row(r) for r in rows]


@bp.route(route="candidati-probabili/{draft_id}/scarta", methods=["POST"])
@http_error_handler
async def discard(req: func.HttpRequest):
    draft_id = _valid_draft_id(req.route_params.get("draft_id"))
    store = _get_store()
    row = store.get(draft_id)
    if row is None:
        raise NotFoundError("Candidato probabile non trovato")
    if row.get("stato") == pc.APPROVED:
        raise InvalidInputError("Il candidato è già stato approvato: non si scarta più")
    row = pc.discarded_row(row, body_params(req).get("user"))
    store.save(row)
    return pc.dashboard_row(row)


async def on_commit_done(draft: dict[str, Any], op: dict[str, Any]) -> None:
    """Bozza proposta da un recruiter e confermata dall'amministrazione: riga approvata."""
    if not draft.get("submitted_at"):
        return
    store = _get_store()
    row = store.get(draft["draft_id"])
    if row is None or row.get("stato") == pc.APPROVED:
        return
    store.save(pc.approved_row(row, draft, op))
    logger.info("Candidato probabile approvato draft=%s id_mcflash=%s", draft["draft_id"], op.get("id_mcflash"))

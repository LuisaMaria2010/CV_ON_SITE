"""Endpoint di supporto alla dashboard CV -> MCFlash (repo cv-mcflash-dashboard).

GET /api/roles
    Catalogo dei ruoli MC (infra/mcflash_budget_placeholders.json) per il campo
    "Ruolo" del CV: [{role, label, budget: {junior, mid, senior}}].

GET /api/cv/filename
    Propone il nome del file CV nel formato MC ("Skill Tecnico_<TRITTICO>_MC Engineering").
    Query:
    - trittico   (oppure first_name + last_name, da cui si calcola)
    - id_mcflash (opzionale): candidato esistente -> si riusa il suo file attuale,
      cosi' il nuovo CV sostituisce il vecchio nell'indice
    - ext        (opzionale, default ".pdf")
    Output: {trittico, filename, reused, alternatives}
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import PurePosixPath

import azure.functions as func

from core.config import settings
from core.dependencies import storage
from core.errors import InvalidInputError
from domain.candidate_identity import propose_cv_filename, trittico as compute_trittico
from domain.role_catalog import display_role, load_catalog
from infra import cv_mcflash_map
from utils.http_errors import http_error_handler

logger = logging.getLogger(__name__)

bp = func.Blueprint()


async def _existing_cv_names() -> tuple[list[str], list[dict[str, str]]]:
    """Nomi gia' usati: blob dei CV originali + file registrati in CvMcflashMap."""
    blob_names: list[str] = []
    for container in (settings.storage_container_incoming, settings.storage_container_original_uploads):
        try:
            blob_names += await storage.list_names(container=container)
        except Exception:
            logger.exception("Listing blobs failed container=%s", container)
    try:
        map_rows = await asyncio.to_thread(cv_mcflash_map.read_file_entries)
    except Exception:
        logger.exception("Reading CvMcflashMap failed")
        map_rows = []
    names = [PurePosixPath(n).name for n in blob_names] + [r["file"] for r in map_rows if r["file"]]
    return names, map_rows


@bp.route(route="roles", methods=["GET"])
@http_error_handler
async def roles(req: func.HttpRequest):
    """Catalogo dei ruoli MC per il menu a tendina "Ruolo": chiave, etichetta, tariffe."""
    return [
        {"role": name, "label": display_role(name), "budget": budget}
        for name, budget in sorted(load_catalog().items())
    ]


@bp.route(route="cv/filename", methods=["GET"])
@http_error_handler
async def cv_filename(req: func.HttpRequest):
    tri = (req.params.get("trittico") or "").strip()
    if not tri:
        tri = compute_trittico(req.params.get("first_name"), req.params.get("last_name")) or ""
    if not tri:
        raise InvalidInputError("Serve 'trittico' oppure 'first_name' e 'last_name'")
    ext = (req.params.get("ext") or ".pdf").strip()
    id_mcflash = (req.params.get("id_mcflash") or "").strip()

    names, map_rows = await _existing_cv_names()

    current = None
    if id_mcflash:
        current = next((r["file"] for r in map_rows if r["id_mcflash"] == id_mcflash and r["file"]), None)

    try:
        filename = propose_cv_filename(tri, names, extension=ext, current_name=current)

        # Alternative libere, nel caso l'utente voglia cambiare la proposta.
        taken = list(names) + [filename]
        alternatives: list[str] = []
        for _ in range(3):
            alt = propose_cv_filename(tri, taken, extension=ext)
            alternatives.append(alt)
            taken.append(alt)
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc

    return {
        "trittico": tri.upper(),
        "filename": filename,
        "reused": current is not None,
        "alternatives": alternatives,
    }

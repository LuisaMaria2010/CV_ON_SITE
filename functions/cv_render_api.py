"""Generazione del CV nel modello MCFlash per la dashboard.

POST /api/cv/render
    Body: {data: <dati del form, formato di /api/extract?profile=mcflash>,
           format: "both" (default) | "docx" | "html"}
    Output: {html?, docx_base64?, docx_bytes, checks: [avvisi]}

Il .docx esce da services/cv_mcflash_renderer (lo stesso che usera' la
conferma), l'HTML da services/cv_mcflash_preview con lo stesso contesto.
Email, telefono ed eta' non entrano nel CV: context_from_form li toglie prima
della validazione, cosi' un'email scritta male nel form non blocca l'anteprima.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

import azure.functions as func
from core.errors import InvalidInputError
from services.cv_mcflash_preview import check_context, render_html
from services.cv_mcflash_renderer import context_from_form, render_docx
from utils.http_errors import http_error_handler
from utils.http_params import body_params

logger = logging.getLogger(__name__)

bp = func.Blueprint()

FORMATS = ("both", "docx", "html")


@bp.route(route="cv/render", methods=["POST"])
@http_error_handler
async def cv_render(req: func.HttpRequest):
    body = body_params(req)
    data = body.get("data")
    if not isinstance(data, dict):
        raise InvalidInputError("'data' (i dati del CV) e' obbligatorio ed e' un oggetto")
    fmt = str(body.get("format") or "both").strip().lower()
    if fmt not in FORMATS:
        raise InvalidInputError(f"'format' deve essere uno di: {', '.join(FORMATS)}")

    context = context_from_form(data)
    out: dict[str, Any] = {"checks": check_context(context)}
    if fmt in ("both", "html"):
        out["html"] = render_html(context)
    if fmt in ("both", "docx"):
        # docxtpl e' sincrono e impiega qualche centinaio di ms: fuori dall'event loop.
        docx = await asyncio.to_thread(render_docx, context)
        out["docx_base64"] = base64.b64encode(docx).decode("ascii")
        out["docx_bytes"] = len(docx)
    return out

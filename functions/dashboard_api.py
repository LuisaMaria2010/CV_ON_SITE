"""Endpoint di supporto alla dashboard CV -> MCFlash (repo cv-mcflash-dashboard).

GET /api/roles
    Catalogo dei ruoli MC (infra/mcflash_budget_placeholders.json) per il campo
    "Ruolo" del CV: [{role, label, budget: {junior, mid, senior}}].

GET /api/mc-levels
    Scale dei livelli nei CV MC (domain/mc_levels.py), per i menu del form:
    {skill: [...], lingua: [...]}, dal piu' basso al piu' alto.

GET /api/cv/filename
    Propone il nome del file CV nel formato MC ("Skill Tecnico_<TRITTICO>_MC Engineering").
    Query:
    - trittico   (oppure first_name + last_name, da cui si calcola)
    - id_mcflash (opzionale): candidato esistente -> si riusa il suo file attuale,
      cosi' il nuovo CV sostituisce il vecchio nell'indice
    - ext        (opzionale, default ".pdf")
    Output: {trittico, filename, reused, alternatives}

GET /api/candidates
    Lista dei candidati MCFlash per la home, con il CV indicizzato (CvMcflashMap +
    DocumentRegistry). Query: q (ricerca testuale), sede, has_cv (true|false),
    tutti (true = anche i record non idonei: disabilitati o TalentUp),
    limit (default 50, max 1000), offset, refresh (true = rilegge MCFlash).
    Output: {total, offset, limit, items: [riga], warnings}

GET /api/candidates/{id}
    Riga di un candidato (anche non idoneo), per pre-impostare la pagina Aggiorna.

POST /api/candidates/{id}/nascondi
    Nasconde il candidato su MCFlash (comando di MCFlash, vedi infra/mcflash_writer.py):
    MCFlash non lo restituisce piu', quindi sparisce anche dalla dashboard. Con il
    MCFlash finto il candidato e' segnato nella tabella e non compare piu'. CV,
    indice e tabella CV -> MCFlash restano come sono.
    Query/body: user (opzionale). Output: {id, nascosto, modalita}

GET /api/candidates/{id}/cv
    Il file del CV indicizzato del candidato (PDF/DOCX, come byte), per l'anteprima
    nella home. 404 se il candidato non ha un CV o il file non e' nello storage.

POST /api/cv-finder
    Body: {id_mcflash} oppure {nome, cognome, trittico?, ruolo?, eta?, sede?, seniority?},
    modo (codice|esteso), escludi (document_id gia' mostrati). CV dell'archivio
    della persona con le regole della ricostruzione della tabella CV -> MCFlash
    (services/cv_finder.py, domain/cv_matching.py).
    Output: {persona, modo, esito: {decisione, motivo, document_id, file} | null,
             risultati: [...], warnings}

GET /api/cv/file?document_id=
    Il file di un CV dell'indice (byte), per l'anteprima dei risultati.

POST /api/cv-finder/link
    Body: {id_mcflash, document_id, user?}. Collega un CV gia' indicizzato a un
    candidato senza CV; rifiuta se il CV e' di un altro candidato.

POST /api/candidates/match
    Body: {nome, cognome, trittico?} (oppure nome_completo). Il trittico, se manca,
    si calcola da nome e cognome. Regola in domain/candidate_match.py.
    Output: {trittico, esito: nuovo|aggiorna|scegli, motivo, candidati, stesso_trittico, warnings}
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import PurePosixPath
from typing import Any

import azure.functions as func
from azure.core.exceptions import ResourceNotFoundError

from core.config import settings
from core.dependencies import get_mcflash_candidates_client, storage
from core.errors import InvalidInputError, NotFoundError, UpstreamServiceError
from domain.candidate_identity import (
    possible_tritticos,
    propose_cv_filename,
    split_full_name,
    trittico as compute_trittico,
)
from domain.candidate_match import candidate_row, filter_rows, match_candidate, record_id
from domain.mc_levels import LANGUAGE_LEVELS, SKILL_LEVELS
from domain.role_catalog import display_role, load_catalog
from infra import cv_mcflash_map
from services import cv_finder
from infra.document_registry import DocumentRegistry
from infra.mcflash_candidates import MCFlashApiError, MCFlashCandidatesClient
from infra.mcflash_writer import FakeMCFlashWriter, MCFlashWriteError, get_writer, overlay_records
from utils.app_settings import settings_value
from utils.http_errors import http_error_handler
from utils.http_params import body_params, parse_bool

logger = logging.getLogger(__name__)

bp = func.Blueprint()

# Pagina massima dell'API MCFlash e tetto di sicurezza alla scansione.
_MCFLASH_PAGE = 1000
_MCFLASH_MAX_RECORDS = 20000

_records_lock = asyncio.Lock()
_records_cache: list[dict[str, Any]] | None = None
_records_loaded_at = 0.0

_registry: DocumentRegistry | None = None

_CV_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
}


def _records_ttl_seconds() -> float:
    try:
        return float(settings_value("DASHBOARD_CANDIDATES_TTL_SECONDS", default="120"))
    except ValueError:
        return 120.0


async def _all_records(*, refresh: bool = False) -> list[dict[str, Any]]:
    """Tutti i record MCFlash (anche non idonei), tenuti in memoria per qualche minuto:
    lista, dettaglio e match li leggono per intero e MCFlash non ha una ricerca."""
    global _records_cache, _records_loaded_at
    async with _records_lock:
        fresh = time.monotonic() - _records_loaded_at < _records_ttl_seconds()
        if _records_cache is not None and fresh and not refresh:
            return _records_cache
        client = get_mcflash_candidates_client()
        records: list[dict[str, Any]] = []
        while len(records) < _MCFLASH_MAX_RECORDS:
            try:
                page = await client.fetch_page(limit=_MCFLASH_PAGE, offset=len(records))
            except MCFlashApiError as exc:
                raise UpstreamServiceError(f"MCFlash non raggiungibile: {exc}") from exc
            records += page
            if len(page) < _MCFLASH_PAGE:
                break
        records = await asyncio.to_thread(_with_fake_writes, records)
        _records_cache, _records_loaded_at = records, time.monotonic()
        logger.info("Dashboard: MCFlash records loaded=%d", len(records))
        return records


def _with_fake_writes(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Con il MCFlash finto attivo, le sue scritture si vedono nella dashboard come su MCFlash."""
    writer = get_writer()
    if not isinstance(writer, FakeMCFlashWriter):
        return records
    try:
        return overlay_records(records, writer.rows())
    except Exception:
        logger.exception("Dashboard: reading the fake MCFlash table failed")
        return records


def invalidate_candidates_cache() -> None:
    """Dopo una scrittura su MCFlash la prossima lettura rilegge tutto."""
    global _records_cache, _records_loaded_at
    _records_cache, _records_loaded_at = None, 0.0


def _cv_by_mcflash_id(warnings: list[str]) -> dict[str, dict[str, Any]]:
    """id MCFlash -> CV indicizzato {document_id, file, version, status}.

    Se lo storage non risponde la lista esce lo stesso, senza CV, con un avviso:
    meglio vedere i candidati che una pagina vuota.
    """
    try:
        entries = cv_mcflash_map.read_file_entries()
    except Exception:
        logger.exception("Dashboard: reading CvMcflashMap failed")
        warnings.append("Mappa CV -> MCFlash non disponibile: lo stato del CV non e' mostrato")
        return {}
    global _registry
    try:
        # from_settings verifica (e crea) la tabella: una volta per istanza basta.
        if _registry is None:
            _registry = DocumentRegistry.from_settings()
        registry = _registry
        documents = registry.list_documents()
        normalize = registry._normalize_document_id
    except Exception:
        logger.exception("Dashboard: reading DocumentRegistry failed")
        warnings.append("Registro documenti non disponibile: la versione del CV non e' mostrata")
        documents, normalize = {}, (lambda d: d)
    out: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not entry["id_mcflash"]:
            continue
        doc = documents.get(normalize(entry["document_id"])) or {}
        out[entry["id_mcflash"]] = {
            "document_id": entry["document_id"],
            "file": entry["file"],
            "version": doc.get("version"),
            "status": doc.get("status"),
            "source_path": doc.get("source_path"),
        }
    return out


async def _candidate_rows(*, refresh: bool = False) -> tuple[list[dict[str, Any]], list[str]]:
    records = await _all_records(refresh=refresh)
    warnings: list[str] = []
    cvs = await asyncio.to_thread(_cv_by_mcflash_id, warnings)
    rows = [
        candidate_row(r, cvs.get(record_id(r)), eligible=MCFlashCandidatesClient.is_eligible(r))
        for r in records
        if record_id(r)
    ]
    rows.sort(key=lambda row: (str(row["nome_completo"]).casefold(), row["id"]))
    return rows, warnings


def _non_negative_int(raw: str | None, default: int, name: str) -> int:
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise InvalidInputError(f"'{name}' deve essere un intero") from exc
    if value < 0:
        raise InvalidInputError(f"'{name}' non puo' essere negativo")
    return value


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


@bp.route(route="mc-levels", methods=["GET"])
@http_error_handler
async def mc_levels(req: func.HttpRequest):
    """Scale MC per i menu "Livello" di skill e lingue."""
    return {"skill": list(SKILL_LEVELS), "lingua": list(LANGUAGE_LEVELS)}


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


@bp.route(route="candidates", methods=["GET"])
@http_error_handler
async def candidates(req: func.HttpRequest):
    has_cv_raw = req.params.get("has_cv")
    has_cv = parse_bool(has_cv_raw, False) if has_cv_raw not in (None, "") else None
    limit = min(max(_non_negative_int(req.params.get("limit"), 50, "limit"), 1), 1000)
    offset = _non_negative_int(req.params.get("offset"), 0, "offset")

    rows, warnings = await _candidate_rows(refresh=parse_bool(req.params.get("refresh"), False))
    if not parse_bool(req.params.get("tutti"), False):
        rows = [r for r in rows if r["idoneo"]]
    rows = filter_rows(rows, query=req.params.get("q"), sede=req.params.get("sede"), has_cv=has_cv)
    return {
        "total": len(rows),
        "offset": offset,
        "limit": limit,
        "items": rows[offset:offset + limit],
        "warnings": warnings,
    }


@bp.route(route="candidates/{id:int}", methods=["GET"])
@http_error_handler
async def candidate_detail(req: func.HttpRequest):
    wanted = str(req.route_params.get("id") or "").strip()
    rows, warnings = await _candidate_rows(refresh=parse_bool(req.params.get("refresh"), False))
    row = next((r for r in rows if r["id"] == wanted), None)
    if row is None:
        raise NotFoundError(f"Candidato MCFlash {wanted} non trovato")
    return {**row, "warnings": warnings}


@bp.route(route="candidates/{id:int}/nascondi", methods=["POST"])
@http_error_handler
async def candidate_hide(req: func.HttpRequest):
    wanted = str(req.route_params.get("id") or "").strip()
    rows, _ = await _candidate_rows(refresh=True)
    row = next((r for r in rows if r["id"] == wanted), None)
    if row is None:
        raise NotFoundError(f"Candidato MCFlash {wanted} non trovato")
    user = req.params.get("user") or body_params(req).get("user")
    writer = get_writer()
    try:
        await asyncio.to_thread(writer.hide, wanted, user=user)
    except MCFlashWriteError as exc:
        raise UpstreamServiceError(f"MCFlash non ha nascosto il candidato: {exc}") from exc
    invalidate_candidates_cache()
    mode = "finto" if isinstance(writer, FakeMCFlashWriter) else "reale"
    logger.info("Dashboard: candidate hidden id=%s mode=%s user=%s cv=%s", wanted, mode, user,
                (row.get("cv") or {}).get("document_id"))
    return {"id": wanted, "nascosto": True, "modalita": mode}


def _blob_location(cv: dict[str, Any]) -> tuple[str, str]:
    """(container, blob) del CV: dal source_path del registro ("/incoming-cv/<blob>"),
    altrimenti il nome file della mappa nel container dei CV in ingresso."""
    source = str(cv.get("source_path") or "").strip("/")
    container, sep, blob = source.partition("/")
    if sep and blob:
        return container, blob
    return settings.storage_container_incoming, str(cv.get("file") or "")


@bp.route(route="candidates/{id:int}/cv", methods=["GET"])
@http_error_handler
async def candidate_cv(req: func.HttpRequest):
    wanted = str(req.route_params.get("id") or "").strip()
    rows, _ = await _candidate_rows()
    row = next((r for r in rows if r["id"] == wanted), None)
    if row is None:
        raise NotFoundError(f"Candidato MCFlash {wanted} non trovato")
    if not row.get("cv"):
        raise NotFoundError(f"Il candidato {wanted} non ha un CV indicizzato")
    return await _cv_file_response(*_blob_location(row["cv"]))


async def _cv_file_response(container: str, blob: str) -> func.HttpResponse:
    """Il file del CV come byte, con il nome nell'intestazione Content-Disposition."""
    if not blob:
        raise NotFoundError("File del CV sconosciuto")
    try:
        data = await storage.download_bytes(blob, container=container)
    except ResourceNotFoundError as exc:
        raise NotFoundError(f"CV «{blob}» non trovato nello storage") from exc
    filename = PurePosixPath(blob).name
    content_type = _CV_CONTENT_TYPES.get(PurePosixPath(filename).suffix.lower(), "application/octet-stream")
    safe = re.sub(r'[^A-Za-z0-9._ -]', "_", filename)
    return func.HttpResponse(body=data, status_code=200, mimetype=content_type,
                             headers={"Content-Disposition": f'inline; filename="{safe}"'})


# ---------------------------------------------------------------- ricerca dei CV nell'archivio

def _person_from_body(body: dict[str, Any]) -> dict[str, Any]:
    first = str(body.get("nome") or "").strip()
    last = str(body.get("cognome") or "").strip()
    full = " ".join(p for p in (first, last) if p) or str(body.get("nome_completo") or "").strip()
    if not (first or last) and full:
        first, last = (part or "" for part in split_full_name(full))
    tri = str(body.get("trittico") or "").strip().upper() or compute_trittico(first, last) or ""
    if not (tri or body.get("ruolo")):
        raise InvalidInputError("Servono almeno nome e cognome (per il codice) oppure il ruolo")
    # Prima il codice di nome + cognome come scritti, poi le altre divisioni del nome completo.
    codici = [c for c in [tri, *possible_tritticos(full)] if c]
    return {"id_mcflash": None, "nome": full or None, "trittico": tri, "codici": list(dict.fromkeys(codici)),
            "ruolo": body.get("ruolo"), "eta": body.get("eta"), "sede": body.get("sede"),
            "seniority": body.get("seniority")}


@bp.route(route="cv-finder", methods=["POST"])
@http_error_handler
async def cv_finder_search(req: func.HttpRequest):
    body = body_params(req)
    mode = str(body.get("modo") or "codice").strip().lower()
    if mode not in cv_finder.MODES:
        raise InvalidInputError(f"'modo' deve essere uno di: {', '.join(cv_finder.MODES)}")
    rows, warnings = await _candidate_rows()
    names = {r["id"]: r["nome_completo"] for r in rows}
    if body.get("id_mcflash") not in (None, ""):
        wanted = str(body["id_mcflash"]).strip()
        row = next((r for r in rows if r["id"] == wanted), None)
        if row is None:
            raise NotFoundError(f"Candidato MCFlash {wanted} non trovato")
        person = cv_finder.person_from_row(row)
    else:
        person = _person_from_body(body)
    entries = await asyncio.to_thread(cv_mcflash_map.read_file_entries)
    linked = cv_finder.linked_owners(entries, names.get)
    exclude = {str(d) for d in body.get("escludi") or []}
    docs = await cv_finder.documents(entries)
    found = await cv_finder.find(person, mode=mode, exclude=exclude, linked=linked, docs=docs)
    return {"persona": person, "modo": mode, **found, "warnings": warnings}


@bp.route(route="cv/file", methods=["GET"])
@http_error_handler
async def cv_file(req: func.HttpRequest):
    wanted = str(req.params.get("document_id") or "").strip().lower()
    if not wanted:
        raise InvalidInputError("Serve 'document_id'")
    entries = await asyncio.to_thread(cv_mcflash_map.read_file_entries)
    doc = next((d for d in await cv_finder.documents(entries)
                if cv_finder._key(d["document_id"]) == cv_finder._key(wanted)), None)
    if doc is None:
        raise NotFoundError(f"CV {wanted} non trovato nell'archivio")
    return await _cv_file_response(*_blob_location(doc))


@bp.route(route="cv-finder/link", methods=["POST"])
@http_error_handler
async def cv_finder_link(req: func.HttpRequest):
    """Collega un CV gia' indicizzato a un candidato che non ne ha uno (riga in CvMcflashMap)."""
    body = body_params(req)
    wanted = str(body.get("id_mcflash") or "").strip()
    doc_id = str(body.get("document_id") or "").strip().lower()
    if not wanted or not doc_id:
        raise InvalidInputError("Servono 'id_mcflash' e 'document_id'")
    rows, _ = await _candidate_rows()
    row = next((r for r in rows if r["id"] == wanted), None)
    if row is None:
        raise NotFoundError(f"Candidato MCFlash {wanted} non trovato")
    if row.get("cv"):
        raise InvalidInputError("Il candidato ha gia' un CV: per sostituirlo usa la pagina Aggiorna")
    entries = await asyncio.to_thread(cv_mcflash_map.read_file_entries)
    doc = next((d for d in await cv_finder.documents(entries)
                if cv_finder._key(d["document_id"]) == cv_finder._key(doc_id)), None)
    if doc is None:
        raise NotFoundError(f"CV {doc_id} non trovato nell'archivio")
    owner = next((e["id_mcflash"] for e in entries if cv_finder._key(e["document_id"]) == cv_finder._key(doc_id)), None)
    if owner and owner != wanted:
        raise InvalidInputError(f"Il CV e' gia' collegato al candidato {owner}")
    entity = await asyncio.to_thread(cv_mcflash_map.upsert_entry, doc["document_id"], wanted, file=doc["file"],
                                     codice=str(row.get("trittico") or ""), criterio="dashboard-ricerca")
    logger.info("CV finder: linked document_id=%s id_mcflash=%s user=%s", doc["document_id"], wanted, body.get("user"))
    return {"id_mcflash": wanted, "document_id": entity["RowKey"], "file": entity["file"]}


@bp.route(route="candidates/match", methods=["POST"])
@http_error_handler
async def candidates_match(req: func.HttpRequest):
    body = body_params(req)
    first = str(body.get("nome") or "").strip()
    last = str(body.get("cognome") or "").strip()
    if not (first or last):
        first, last = (part or "" for part in split_full_name(body.get("nome_completo")))
    full_name = " ".join(p for p in (first, last) if p)
    if not full_name:
        raise InvalidInputError("Servono 'nome' e 'cognome' (oppure 'nome_completo')")
    tri = str(body.get("trittico") or "").strip() or compute_trittico(first, last) or ""
    if not tri:
        raise InvalidInputError("Trittico non calcolabile: servono nome e cognome (almeno 2 lettere)")

    rows, warnings = await _candidate_rows(refresh=parse_bool(body.get("refresh"), False))
    return {"trittico": tri.upper(), **match_candidate(tri, full_name, rows), "warnings": warnings}

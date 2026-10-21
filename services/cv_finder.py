"""Ricerca dei CV di una persona nell'archivio, per la dashboard.

Stesso metodo e stesse regole della ricostruzione della tabella CV -> MCFlash
(domain/cv_matching.py, usato anche da scripts/build_cv_mcflash_map.py):
- i CV con il codice della persona; dell'intestazione (codice, nascita, citta',
  ruolo) fa fede il file del CV nello storage, come i file originali per la
  ricostruzione;
- giudizio LLM sul mestiere (lo stesso prompt), persone raggruppate per data di
  nascita, livello di ciascuna e scelta con le stesse soglie;
- un CV gia' collegato a un altro candidato, o riservato a un altro da una
  decisione manuale, non si propone; la decisione manuale della persona vince.
  A runtime conta solo CvMcflashMap: le decisioni manuali della ricostruzione
  sono gia' li' come collegamenti, quindi di norma overrides e' vuoto.

Quali CV esistono lo dice la tabella DocumentRegistry dello storage; il codice
viene dal nome del file, dall'intestazione e, per i CV gia' collegati, dalla
tabella CvMcflashMap (codice e file). L'indice di ricerca serve per ruolo,
skill e testo quando il file non si legge.

Modo "esteso" (se i CV con il codice non convincono): CV con un codice simile
(due lettere su tre, lettere scambiate, stesse iniziali) e con eta' o mestiere
compatibili, valutati con le stesse regole ma senza scelta automatica.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Awaitable, Callable

from core.config import settings
from domain import cv_matching as cm
from domain import cv_profile as cp

logger = logging.getLogger(__name__)

MODES = ("codice", "esteso")
MAX_EXTENDED = 10
CATALOG_TTL_SECONDS = 900

_catalog: list[dict[str, Any]] | None = None
_catalog_at = 0.0
_catalog_lock = asyncio.Lock()
_headers: dict[tuple[str, str], dict[str, Any] | None] = {}  # (document_id, versione) -> intestazione dal file


def _key(document_id: str) -> str:
    """document_id come RowKey di DocumentRegistry (minuscolo, spazi -> trattini)."""
    return str(document_id or "").strip().lower().replace(" ", "-")


# ---------------------------------------------------------------- indice

def profile_from_chunks(document_id: str, chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """Profilo di un CV dai suoi chunk dell'indice (come _all_cv_documents della ricostruzione)."""
    chunks = sorted(chunks, key=lambda c: c.get("chunk_index") or 0)
    text = "\n".join(c.get("content") or "" for c in chunks)
    source = next((str(c["source_path"]) for c in chunks if c.get("source_path")), "")
    file = source.rsplit("/", 1)[-1]
    m = cm.CODE_RE.search(text)
    code = m.group(1).upper() if m else next(
        (str(c["full_name"]).strip().upper() for c in chunks if len(cm.person_key(c.get("full_name"))) == 3), None)
    dob = None
    for rx in cm.DOB_RES:
        mm = rx.search(text)
        if mm and (dob := cm.parse_date(mm.group(1))):
            break
    city = cm.CITY_RE.search(text)
    skills: list[str] = []
    for c in chunks:
        for s in c.get("skills") or []:
            if s not in skills:
                skills.append(s)
    return {
        "document_id": document_id, "file": file, "source_path": source,
        "codes": cm.codes(code, file),
        "role": next((c.get("role") for c in chunks if c.get("role")), None),
        "skills": skills, "dob": dob,
        "city": (city.group(1) if city else None) or chunks[0].get("location"),
        "processed_at": max((c.get("processed_at") or "") for c in chunks),
        "text": text,
    }


async def _read_catalog() -> list[dict[str, Any]]:
    from azure.core.credentials import AzureKeyCredential
    from azure.identity.aio import DefaultAzureCredential
    from azure.search.documents.aio import SearchClient

    credential = (AzureKeyCredential(settings.azure_search_api_key) if settings.azure_search_api_key
                  else DefaultAzureCredential())
    client = SearchClient(endpoint=settings.search_endpoint, index_name=settings.document_search_index_name,
                          credential=credential)
    fields = ["document_id", "full_name", "role", "skills", "source_path", "location", "chunk_index", "content",
              "processed_at"]
    by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    try:
        skip = 0
        while True:  # pagine da 1000: l'indice non ne restituisce di piu' per richiesta
            page = [dict(h) async for h in await client.search(search_text="*", select=fields, top=1000, skip=skip)]
            for hit in page:
                if hit.get("document_id"):
                    by_doc[hit["document_id"]].append(hit)
            skip += len(page)
            if len(page) < 1000:
                break
    finally:
        await client.close()
    return [profile_from_chunks(doc_id, chunks) for doc_id, chunks in by_doc.items()]


async def catalog(*, refresh: bool = False) -> list[dict[str, Any]]:
    global _catalog, _catalog_at
    async with _catalog_lock:
        if _catalog is None or refresh or time.monotonic() - _catalog_at > CATALOG_TTL_SECONDS:
            t0 = time.monotonic()
            _catalog = await _read_catalog()
            _catalog_at = time.monotonic()
            logger.info("CV finder: catalog loaded docs=%d ms=%.0f", len(_catalog), (_catalog_at - t0) * 1000)
        return _catalog


# ---------------------------------------------------------------- archivio = tabelle dello storage

def merge_documents(registry: dict[str, dict[str, Any]], catalog_docs: list[dict[str, Any]],
                    map_entries: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Un profilo per ogni CV della tabella DocumentRegistry in incoming-cv.

    Codici = nome del file + codice della tabella CvMcflashMap + intestazione
    letta dall'indice. Il resto del profilo viene dall'indice, se c'e'.
    """
    indexed = {_key(d["document_id"]): d for d in catalog_docs}
    map_codes: dict[str, set[str]] = defaultdict(set)
    for e in map_entries:
        if e.get("codice"):
            map_codes[_key(e["document_id"])].add(cm.person_key(e["codice"]))
    prefix = f"/{settings.storage_container_incoming}/"
    out = []
    for doc_id, row in registry.items():
        source = str(row.get("source_path") or "")
        if not source.startswith(prefix):
            continue
        file = source.rsplit("/", 1)[-1]
        prof = dict(indexed.get(_key(doc_id)) or {
            "codes": set(), "role": None, "skills": [], "dob": None, "city": None,
            "processed_at": row.get("processed_at") or "", "text": ""})
        prof.update(document_id=doc_id, file=file, source_path=source, version=str(row.get("version") or ""))
        prof["codes"] = (set(prof.get("codes") or set()) | cm.codes(None, file) | map_codes[_key(doc_id)]) - {""}
        out.append(prof)
    return out


async def documents(map_entries: list[dict[str, str]]) -> list[dict[str, Any]]:
    """CV dell'archivio: tabella DocumentRegistry (letta a ogni ricerca) + profili dell'indice."""
    from infra.document_registry import DocumentRegistry

    registry = await asyncio.to_thread(lambda: DocumentRegistry.from_settings().list_documents())
    return merge_documents(registry, await catalog(), map_entries)


async def file_header(doc: dict[str, Any]) -> dict[str, Any] | None:
    """Intestazione letta dal file del CV nello storage (in memoria per documento e versione)."""
    from azure.core.exceptions import ResourceNotFoundError

    from core.dependencies import storage

    cache_key = (_key(doc["document_id"]), str(doc.get("version") or ""))
    if cache_key in _headers:
        return _headers[cache_key]
    container, _, blob = str(doc.get("source_path") or "").strip("/").partition("/")
    if not blob:
        return None
    try:
        data = await storage.download_bytes(blob, container=container)
    except ResourceNotFoundError:
        return None
    text = await asyncio.to_thread(cm.read_document_text, data, doc["file"])
    head = cm.header_from_text(text, doc["file"]) if text.strip() else None
    _headers[cache_key] = head
    return head


def apply_header(doc: dict[str, Any], head: dict[str, Any] | None) -> dict[str, Any]:
    """Come la ricostruzione: dell'intestazione del file fanno fede codice, nascita, citta', ruolo."""
    if not head:
        return doc
    out = dict(doc)
    out["codes"] = set(doc.get("codes") or set()) | cm.codes(head.get("code"), doc["file"])
    out["dob"] = head.get("dob") or doc.get("dob")
    out["city"] = head.get("city") or doc.get("city")
    out["role"] = head.get("role") or doc.get("role")
    out["text"] = head.get("text") or doc.get("text")
    return out


# ---------------------------------------------------------------- ricerca

def _extended_candidates(person: dict[str, Any], docs: list[dict[str, Any]], exclude: set[str],
                         today: dt.date, codes: set[str]) -> list[dict[str, Any]]:
    """CV con codice simile (o qualunque, se manca il codice) con eta' o mestiere plausibili."""
    scored = []
    for doc in docs:
        if doc["document_id"] in exclude or (codes & doc["codes"]):
            continue
        sim = max((cp.code_similarity(a, b) for a in codes for b in doc["codes"]), default=0) if codes else 0
        if codes and sim == 0:
            continue
        try:
            eta = int(person.get("eta")) if person.get("eta") not in (None, "") else None
        except (TypeError, ValueError):
            eta = None
        delta = cm.age_on(doc["dob"], today) - eta if doc.get("dob") and eta else None
        age_ok = delta is not None and cm.AGE_MIN_DELTA <= delta <= cm.AGE_MAX_DELTA
        words = cp.role_overlap(doc.get("role"), doc.get("skills") or [], person.get("ruolo"))
        if not (age_ok or words) or (delta is not None and abs(delta) > cm.MAX_DELTA_SINGLE_PERSON):
            continue
        scored.append((2 * sim + min(words, 3) + (2 if age_ok else 0), doc))
    scored.sort(key=lambda sd: -sd[0])
    return [d for _, d in scored[:MAX_EXTENDED]]


def _matches(decision: dict[str, Any], doc: dict[str, Any]) -> bool:
    return bool((decision.get("document_id") and _key(decision["document_id"]) == _key(doc["document_id"]))
                or (decision.get("file") and cm.norm_filename(decision["file"]) == cm.norm_filename(doc["file"])))


def _no_cv(mode: str, own_id: str, overrides: dict[int, dict[str, Any]]) -> dict[str, Any] | None:
    if mode != "codice":
        return None
    manual = overrides.get(int(own_id)) if own_id.isdigit() else None
    if manual is not None:
        return {"decisione": "manuale", "motivo": manual.get("nota") or "", "document_id": None, "file": None}
    return {"decisione": "nessun CV", "motivo": cm.NO_CV_REASON, "document_id": None, "file": None}


async def find(person: dict[str, Any], *, mode: str = "codice", exclude: set[str] | None = None,
               linked: dict[str, dict[str, str]] | None = None, docs: list[dict[str, Any]] | None = None,
               llm: Any = None, model: str | None = None,
               headers: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = None,
               overrides: dict[int, dict[str, Any]] | None = None,
               today: dt.date | None = None) -> dict[str, Any]:
    """CV della persona con le regole della ricostruzione.

    person: {id_mcflash?, nome, trittico, ruolo, eta, sede, seniority}
    linked: document_id -> {id, nome} dei CV gia' collegati a un candidato.
    Restituisce {esito, risultati}: esito = decisione della ricostruzione per la
    persona (solo nel modo "codice"), risultati = un elemento per CV.
    """
    if mode not in MODES:
        raise ValueError(f"modo non valido: {mode}")
    today = today or dt.date.today()
    linked = linked or {}
    exclude = exclude or set()
    if docs is None:
        from infra import cv_mcflash_map
        docs = await documents(await asyncio.to_thread(cv_mcflash_map.read_file_entries))
    headers = headers or file_header
    overrides = overrides or {}
    own_id = str(person.get("id_mcflash") or "")
    # Per un candidato MCFlash vale il suo trittico; per un nome scritto a mano
    # (pagina Crea) anche gli altri codici possibili del nome completo.
    codes = {cm.person_key(c) for c in (person.get("codici") or [person.get("trittico")])} - {""}

    if mode == "codice":
        picked = [d for d in docs if codes & d["codes"] and d["document_id"] not in exclude]
    else:
        picked = _extended_candidates(person, docs, exclude, today, codes)
    logger.info("CV finder: mode=%s codes=%s docs=%d picked=%d", mode, sorted(codes), len(docs), len(picked))
    if not picked:
        return {"esito": _no_cv(mode, own_id, overrides), "risultati": []}

    heads = await asyncio.gather(*(headers(d) for d in picked))
    picked = [apply_header(d, h) for d, h in zip(picked, heads)]
    if not str(person.get("ruolo") or "").strip() and person.get("eta") in (None, "", 0):
        # Solo il codice: senza ruolo ne' eta' le regole non hanno niente da confrontare.
        return {"esito": {"decisione": "solo codice", "motivo": "servono ruolo o eta' per la valutazione",
                          "document_id": None, "file": None},
                "risultati": [_plain_result(d, linked, own_id, mode, today) for d in picked]}

    if llm is None:
        from services.coherence_evaluator import get_judge_client
        llm = get_judge_client()
    model = model or settings.azure_openai_model
    role = str(person.get("ruolo") or "")

    def judge(doc: dict[str, Any]) -> dict[str, Any]:
        if llm is None:
            return {"coerente": cm.strict_role(doc.get("role"), role) or cm.role_matches(doc.get("role"), role),
                    "motivo": "confronto dei ruoli (LLM non configurato)"}
        try:
            return cm.judge(llm, model, doc, role)
        except Exception as exc:
            logger.warning("CV finder: judge failed doc=%s err=%s", doc.get("document_id"), exc)
            return {"coerente": cm.strict_role(doc.get("role"), role), "motivo": "giudizio LLM non disponibile"}

    with ThreadPoolExecutor(max_workers=8) as pool:
        verdicts = await asyncio.get_running_loop().run_in_executor(None, lambda: list(pool.map(judge, picked)))
    judged = {d["document_id"]: v for d, v in zip(picked, verdicts)}

    # CV non disponibili per la persona: collegati a un altro candidato o riservati a mano a un altro.
    reserved: dict[str, str] = {}
    for rid, decision in overrides.items():
        if str(rid) == own_id:
            continue
        for d in picked:
            if _matches(decision, d):
                reserved[d["document_id"]] = f"riservato a mano al candidato {rid}"
    owners = {d["document_id"]: linked.get(_key(d["document_id"])) for d in picked}
    taken = {doc_id for doc_id, o in owners.items() if o and o.get("id") != own_id} | set(reserved)

    try:
        eta = int(person.get("eta")) if person.get("eta") not in (None, "") else None
    except (TypeError, ValueError):
        eta = None
    rec = {"eta": eta, "ruolo": person.get("ruolo"), "sede": person.get("sede")}
    people = cm.group_people(picked)
    options = {key: cm.evaluate(rec, key, group, lambda d: judged[d["document_id"]]["coerente"], today)
               for key, group in people.items()}

    esito = None
    chosen_doc = None
    if mode == "codice":
        free = [o for key, o in options.items()
                if o["tier"] < 9 and not any(d["document_id"] in taken for d in people[key])]
        decision, chosen, why = cm.choose(free, len(people))
        if chosen:
            chosen_doc = cm.pick_file(people[chosen["key"]], rec)
        manual = overrides.get(int(own_id)) if own_id.isdigit() else None
        if manual is not None:
            decision, why = "manuale", manual.get("nota") or ""
            chosen_doc = next((d for d in picked if _matches(manual, d)), None)
        esito = {"decisione": decision, "motivo": why,
                 "document_id": chosen_doc["document_id"] if chosen_doc else None,
                 "file": chosen_doc["file"] if chosen_doc else None}

    results = []
    for key, group in people.items():
        o = options[key]
        for d in group:
            owner = owners.get(d["document_id"])
            dob = d.get("dob")
            results.append({
                "document_id": d["document_id"], "file": d["file"],
                "codici": sorted(c.upper() for c in d["codes"]),
                "nato": dob.isoformat() if dob else None,
                "eta_cv": cm.age_on(dob, today) if dob else None,
                "citta": d.get("city"), "ruolo_cv": d.get("role"),
                "livello": o["tier"], "livello_testo": cm.TIER_LABELS[o["tier"]],
                "eta_delta": o["delta"], "mestiere_ok": judged[d["document_id"]]["coerente"],
                "motivo": judged[d["document_id"]]["motivo"], "stessa_citta": o["city_ok"],
                "collegato_a": owner if owner and owner.get("id") != own_id else None,
                "gia_suo": bool(owner and owner.get("id") == own_id),
                "riservato": reserved.get(d["document_id"]),
                "scelto": bool(chosen_doc and chosen_doc["document_id"] == d["document_id"]),
                "modo": mode,
            })
    results.sort(key=lambda r: (not r["scelto"], r["livello"], r["collegato_a"] is not None or bool(r["riservato"]),
                                abs(r["eta_delta"] or 0)))
    return {"esito": esito, "risultati": results}


def _plain_result(d: dict[str, Any], linked: dict[str, dict[str, str]], own_id: str, mode: str,
                  today: dt.date) -> dict[str, Any]:
    """CV trovato per codice, senza valutazione."""
    owner = linked.get(_key(d["document_id"]))
    dob = d.get("dob")
    return {"document_id": d["document_id"], "file": d["file"], "codici": sorted(c.upper() for c in d["codes"]),
            "nato": dob.isoformat() if dob else None, "eta_cv": cm.age_on(dob, today) if dob else None,
            "citta": d.get("city"), "ruolo_cv": d.get("role"), "livello": None, "livello_testo": "da valutare",
            "eta_delta": None, "mestiere_ok": None, "motivo": "", "stessa_citta": False,
            "collegato_a": owner if owner and owner.get("id") != own_id else None,
            "gia_suo": bool(owner and owner.get("id") == own_id), "riservato": None, "scelto": False, "modo": mode}


def person_from_row(row: dict[str, Any]) -> dict[str, Any]:
    """Anagrafica per la ricerca da una riga candidato della dashboard."""
    return {"id_mcflash": row.get("id"), "nome": row.get("nome_completo"), "trittico": row.get("trittico"),
            "ruolo": row.get("ruolo"), "eta": row.get("eta"), "sede": row.get("sede"), "seniority": row.get("seniority")}


def linked_owners(entries: list[dict[str, str]], names: Callable[[str], str | None]) -> dict[str, dict[str, str]]:
    """document_id -> {id, nome} dalle righe della tabella CV -> MCFlash."""
    return {_key(e["document_id"]): {"id": e["id_mcflash"], "nome": names(e["id_mcflash"]) or ""}
            for e in entries if e.get("document_id") and e.get("id_mcflash")}

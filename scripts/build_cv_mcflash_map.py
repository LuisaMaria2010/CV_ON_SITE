"""
Genera la tabella CV -> MCFlash: un CV (file) per ogni record MCFlash.

L'indice CV identifica le persone solo con il codice anonimizzato (es. "DCA"),
che non e' univoco: lo stesso codice compare su CV di persone diverse, dentro e
fuori MCFlash. Questa tabella e' il collegamento esplicito usato dal pipeline di
ricerca. Un CV che non e' in tabella e' fuori perimetro.

Per ogni record MCFlash si guardano i CV dell'indice con lo stesso codice:
1. i CV con la stessa data di nascita sono la stessa persona (un candidato);
2. per ogni candidato:
   - eta': la data di nascita scritta nel CV contro l'eta' MCFlash. L'eta'
     MCFlash e' calcolata all'inserimento del record, quindi e' compatibile una
     differenza (eta' oggi - eta' MCFlash) da -1 a +3;
   - mestiere: un LLM giudica se ruolo e skill del CV sono lo stesso mestiere
     del ruolo MCFlash, oppure i due ruoli coincidono;
   - citta': quella del CV contro la sede del record;
3. scelta, dal livello piu' forte:
   - eta' compatibile e mestiere coerente                        -> assegnato
   - eta' non verificabile (eta' 0 / data mancante), mestiere ok -> assegnato
   - eta' che non torna, UNA sola persona con il mestiere giusto -> assegnato
     se la differenza e' al massimo 12 anni o se e' l'unico CV con quel codice
   - tutto il resto (eta' ok ma altro mestiere, piu' persone non distinguibili,
     differenze enormi)                                          -> caso dubbio
4. della persona scelta si tiene un solo file: ruolo piu' vicino, citta' giusta,
   a parita' il piu' recente.
Infine si applicano le decisioni manuali di cv_mcflash_map_overrides.json
(per record MCFlash). I casi dubbi senza decisione restano senza CV.

Uso:
    python scripts/build_cv_mcflash_map.py --report out.json   # scrive scripts/cv_mcflash_map.json (poi: upload_cv_mcflash_map.py)
    python scripts/build_cv_mcflash_map.py --dry-run --report out.json
    python scripts/build_cv_mcflash_map.py --reuse-report out.json   # stessi giudizi LLM del dry-run
    python scripts/build_cv_mcflash_map.py --cv-dir "<cartella CV>"  # intestazione dai file originali
    python scripts/build_cv_mcflash_map.py --tutti ...   # anche record disabilitati e TalentUp

Con --tutti si collegano anche i record fuori dal perimetro dell'agente: la
ricerca resta comunque limitata ai record idonei (filter_candidates), quindi i
loro CV non compaiono nelle risposte, ma la dashboard li mostra.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import datetime as dt
import json
import os
import re
import sys
import time
import unicodedata
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Any

root = Path(__file__).parent.parent
sys.path.insert(0, str(root))

settings_file = root / "local.settings.json"
if settings_file.exists():
    with settings_file.open(encoding="utf-8") as f:
        for k, v in json.load(f).get("Values", {}).items():
            os.environ.setdefault(k, str(v))

from openai import AzureOpenAI  # noqa: E402

from core.config import settings  # noqa: E402
from core.dependencies import get_mcflash_candidates_client  # noqa: E402
from infra.mcflash_candidates import MCFlashCandidatesClient  # noqa: E402
from services.search_pipeline import _normalise_person_key  # noqa: E402
from domain.normalizer import TextNormalizer  # noqa: E402
from domain import cv_matching as cm  # noqa: E402
from utils.site import site_key  # noqa: E402

MAP_PATH = root / "scripts" / "cv_mcflash_map.json"
OVERRIDES_PATH = cm.OVERRIDES_PATH

# Regole e soglie comuni con la ricerca dei CV della dashboard: domain/cv_matching.py.
AGE_MIN_DELTA, AGE_MAX_DELTA = cm.AGE_MIN_DELTA, cm.AGE_MAX_DELTA
MAX_DELTA_SINGLE_PERSON = cm.MAX_DELTA_SINGLE_PERSON
JUDGE_SYSTEM = cm.JUDGE_SYSTEM
_DATE, _DOB_RES, _CITY_RE, _CODE_RE = cm.DATE, cm.DOB_RES, cm.CITY_RE, cm.CODE_RE
_parse_date, _norm_filename, _file_code, _codes = cm.parse_date, cm.norm_filename, cm.file_code, cm.codes
_age_on, _norm_role, _strict_role, _cities = cm.age_on, cm.norm_role, cm.strict_role, cm.cities


# ------------------------------------------------------------------ lettura dati

def _search_request(body: dict[str, Any]) -> list[dict[str, Any]]:
    return _search_request_raw(body)["value"]


def _search_request_raw(body: dict[str, Any]) -> dict[str, Any]:
    endpoint = settings.search_endpoint.rstrip("/")
    index = settings.document_search_index_name
    req = urllib.request.Request(
        f"{endpoint}/indexes/{index}/docs/search?api-version=2023-11-01",
        data=json.dumps(body).encode(),
        headers={"api-key": settings.azure_search_api_key or "", "content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def _local_headers(cv_dir: str) -> dict[str, dict[str, Any]]:
    """Intestazione dei CV originali (modello MC): nome file -> codice, nascita, citta', ruolo."""
    import docx
    import fitz

    out: dict[str, dict[str, Any]] = {}
    for path in sorted(Path(cv_dir).iterdir()):
        if not path.is_file() or path.suffix.lower() not in (".pdf", ".docx"):
            continue
        try:
            if path.suffix.lower() == ".pdf":
                with fitz.open(path) as pdf:
                    text = "\n".join(page.get_text() for page in pdf)
            else:
                text = "\n".join(p.text for p in docx.Document(path).paragraphs)
        except Exception as exc:  # file danneggiato: resta il dato dell'indice
            print(f"ATTENZIONE: {path.name} non leggibile ({exc})")
            continue
        out[_norm_filename(path.name)] = cm.header_from_text(text, path.name)
    return out



def _all_cv_documents(local: dict[str, dict[str, Any]], include_unindexed: bool = False) -> list[dict[str, Any]]:
    """Tutti i CV dell'indice, uno per document_id, con codice, nascita, citta', ruolo.

    Se il file originale e' nella cartella locale, codice/nascita/citta'/ruolo
    vengono dalla sua intestazione; altrimenti dal testo indicizzato.
    """
    total = _search_request_raw({"search": "*", "top": 0, "count": True})["@odata.count"]
    by_doc: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    skip = 0
    while skip < total:
        page = _search_request({
            "search": "*",
            "select": "document_id,full_name,role,skills,source_path,location,processed_at,chunk_index,content",
            "top": 1000,
            "skip": skip,
        })
        if not page:
            break
        for hit in page:
            by_doc[hit["document_id"]].append(hit)
        skip += len(page)

    docs = []
    for doc_id, chunks in by_doc.items():
        chunks.sort(key=lambda c: c.get("chunk_index") or 0)
        text = "\n".join(c.get("content") or "" for c in chunks)
        first = chunks[0]
        file = str(first.get("source_path") or "").rsplit("/", 1)[-1]
        head = local.get(_norm_filename(file)) or {}

        code = head.get("code")
        if not code:
            m = _CODE_RE.search(text)
            code = m.group(1).upper() if m else None
        if not code:
            code = next((c["full_name"].strip().upper() for c in chunks
                         if len(_normalise_person_key(c.get("full_name"))) == 3), None)

        dob = head.get("dob")
        if not dob:
            for rx in _DOB_RES:
                m = rx.search(text)
                if m and (dob := _parse_date(m.group(1))):
                    break
        city_m = _CITY_RE.search(text)
        skills: list[str] = []
        for c in chunks:
            for s in c.get("skills") or []:
                if s not in skills:
                    skills.append(s)
        docs.append({
            "document_id": doc_id,
            "file": file,
            "in_locale": bool(head),
            "code": _normalise_person_key(code) if code else None,
            "codes": _codes(code, file),
            "role": head.get("role") or next((c.get("role") for c in chunks if c.get("role")), None),
            "skills": skills,
            "dob": dob,
            "city": head.get("city") or (city_m.group(1) if city_m else None) or first.get("location"),
            "processed_at": max((c.get("processed_at") or "") for c in chunks),
            "text": head.get("text") or text,
            "in_indice": True,
        })
    if include_unindexed:
        # CV locali non ancora nell'indice: il document_id e' quello che avranno
        # una volta caricati (nome file normalizzato, come in ingestion_triggers).
        # Stessa regola di DocumentRegistry.resolve_document_id: id ripulito dal
        # nome, ma se e' gia' di un altro file si usa il nome originale. Tra file
        # nuovi in collisione vince il primo in ordine di nome: caricarli in
        # quell'ordine.
        indexed_files = {_norm_filename(d["file"]) for d in docs}
        used = {d["document_id"] for d in docs}
        renamed: list[str] = []
        for key, head in sorted(local.items(), key=lambda kv: kv[1]["file"]):
            if key in indexed_files or not head.get("code"):
                continue
            doc_id = _registry_key(TextNormalizer.normalize_document_id(head["file"]))
            if doc_id in used:
                doc_id = _registry_key(head["file"])
                renamed.append(head["file"])
            used.add(doc_id)
            docs.append({
                "document_id": doc_id,
                "file": head["file"],
                "in_locale": True,
                "code": _normalise_person_key(head["code"]),
                "codes": _codes(head["code"], head["file"]),
                "role": head.get("role"),
                "skills": [],
                "dob": head.get("dob"),
                "city": head.get("city"),
                "processed_at": "",
                "text": head.get("text") or "",
                "in_indice": False,
            })
        if renamed:
            print(f"Nome ripulito gia' di un altro CV, document_id = nome originale ({len(renamed)}): {renamed}")
    return docs


def _registry_key(document_id: str) -> str:
    """document_id come lo salva DocumentRegistry (RowKey)."""
    return document_id.strip().lower().replace(" ", "-")


def _judge(llm: AzureOpenAI, model: str, doc: dict[str, Any], role: str) -> dict[str, Any]:
    return cm.judge(llm, model, doc, role)



# ------------------------------------------------------------------ confronti

def _record_id(record: dict[str, Any]) -> int:
    return int(record.get("id") or record.get("Id"))


async def _all_records(page_size: int = 1000, max_records: int = 20000) -> list[dict[str, Any]]:
    """Tutti i record MCFlash, anche disabilitati e TalentUp (pagine grezze dell'API)."""
    client = get_mcflash_candidates_client()
    records: list[dict[str, Any]] = []
    while len(records) < max_records:
        page = await client.fetch_page(limit=page_size, offset=len(records))
        records += page
        if len(page) < page_size:
            break
    return records


# ------------------------------------------------------------------ build

def build(report_path: str | None, dry_run: bool, reuse_report: str | None = None, cv_dir: str | None = None,
          no_llm: bool = False, out_path: Path = MAP_PATH, overrides_path: Path | None = OVERRIDES_PATH,
          include_unindexed: bool = False, include_ineligible: bool = False) -> None:
    today = dt.date.today()
    if include_ineligible:
        records = asyncio.run(_all_records())
    else:
        records = asyncio.run(get_mcflash_candidates_client().filter_candidates(
            limit=5000, offset=0, max_scan_records=20000, return_all_matches=True,
        ))
    records_by_code: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for record in records:
        code = _normalise_person_key(record.get("nome") or record.get("Nome"))
        if code:
            records_by_code[code].append(record)
    print(f"Record MCFlash: {len(records)} ({len(records_by_code)} codici)")

    local = _local_headers(cv_dir) if cv_dir else {}
    all_docs = _all_cv_documents(local, include_unindexed)
    docs_by_code: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for doc in all_docs:
        for code in doc["codes"] & set(records_by_code):
            docs_by_code[code].append(doc)
    n_docs = sum(len(d) for d in docs_by_code.values())
    print(f"File locali: {len(local)} | CV nell'indice: {sum(1 for d in all_docs if d['in_indice'])} "
          f"(con file locale: {sum(1 for d in all_docs if d['in_indice'] and d['in_locale'])}) | "
          f"CV locali da caricare: {sum(1 for d in all_docs if not d['in_indice'])} | con codice MCFlash: {n_docs} | con data di nascita: "
          f"{sum(1 for d in docs_by_code.values() for x in d if x['dob'])}")
    indexed = {_norm_filename(d["file"]) for d in all_docs}
    not_indexed = sorted(h["file"] for k, h in local.items()
                         if k not in indexed and _normalise_person_key(h.get("code")) in records_by_code)
    if not_indexed:
        print(f"ATTENZIONE: {len(not_indexed)} file locali con codice MCFlash non sono nell'indice "
              f"(da caricare): {not_indexed}")

    pairs = [(doc, rec) for code, docs in docs_by_code.items() for doc in docs for rec in records_by_code[code]]
    # Giudizi LLM di un report precedente: l'LLM non risponde sempre uguale sui
    # casi al limite, riusarli rende la rigenerazione riproducibile.
    judged: dict[tuple[str, int], dict[str, Any]] = {}
    if reuse_report:
        for rec_report in json.loads(Path(reuse_report).read_text(encoding="utf-8")):
            for cand in rec_report["candidati"]:
                for f in cand["files"]:
                    judged[(f["document_id"], int(rec_report["id_mcflash"]))] = {"coerente": f["llm"], "motivo": f["motivo_llm"]}
    todo = [p for p in pairs if (p[0]["document_id"], _record_id(p[1])) not in judged]
    model = os.environ.get("AZURE_OPENAI_MODEL") or settings.azure_openai_model
    if no_llm:
        # Senza LLM: il mestiere e' coerente se i ruoli coincidono (confronto del
        # pipeline); i casi incerti finiscono tra i dubbi e si decidono a mano.
        model = "nessuno (confronto ruoli)"
        verdicts = [
            {"coerente": _strict_role(doc["role"], rec.get("ruolo"))
                         or MCFlashCandidatesClient._role_matches(str(doc["role"] or ""), rec.get("ruolo")),
             "motivo": "confronto ruoli (senza LLM)"}
            for doc, rec in todo
        ]
    else:
        llm = AzureOpenAI(
            azure_endpoint=settings.azure_openai_endpoint.rstrip("/"),
            api_key=settings.azure_openai_key,
            api_version=settings.azure_openai_api_version,
        )
        with ThreadPoolExecutor(8) as pool:
            verdicts = list(pool.map(lambda p: _judge(llm, model, p[0], str(p[1].get("ruolo") or "")), todo))
    judged.update({(doc["document_id"], _record_id(rec)): v for (doc, rec), v in zip(todo, verdicts)})
    print(f"Coppie CV-record: {len(pairs)} | giudizi nuovi: {len(todo)} | riusati: {len(pairs) - len(todo)}")

    overrides = (json.loads(overrides_path.read_text(encoding="utf-8"))
                 if overrides_path and overrides_path.exists() else {})
    manual = {int(k): v for k, v in (overrides.get("records") or {}).items()}
    # CV indicato a mano: per document_id o per nome file, anche se il suo codice
    # non e' quello del record. Non puo' finire in automatico a un altro record.
    docs_by_id = {d["document_id"]: d for d in all_docs}
    docs_by_file = {_norm_filename(d["file"]): d for d in all_docs}
    manual_doc: dict[int, dict[str, Any] | None] = {}
    for rid, decision in manual.items():
        doc = docs_by_id.get(decision.get("document_id") or "") or docs_by_file.get(_norm_filename(decision.get("file") or ""))
        if (decision.get("document_id") or decision.get("file")) and doc is None:
            print(f"ATTENZIONE: override per {rid} su un CV sconosciuto: {decision}")
        manual_doc[rid] = doc
    reserved = {d["document_id"] for d in manual_doc.values() if d}
    for code in docs_by_code:
        docs_by_code[code] = [d for d in docs_by_code[code] if d["document_id"] not in reserved]
    assigned_docs: set[str] = set()  # un CV a un solo record, anche tra codici diversi

    mapping: dict[str, dict[str, Any]] = {}
    report: list[dict[str, Any]] = []

    for code, recs in sorted(records_by_code.items()):
        docs = docs_by_code.get(code) or []
        # 1. persone: CV con la stessa data di nascita
        people: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
        for doc in docs:
            people[f"dob:{doc['dob']}" if doc["dob"] else f"doc:{doc['document_id']}"].append(doc)

        def evaluate(rec: dict[str, Any], key: str) -> dict[str, Any]:
            rid = _record_id(rec)
            return cm.evaluate(rec, key, people[key], lambda d: judged[(d["document_id"], rid)]["coerente"], today)

        options = {_record_id(r): [evaluate(r, k) for k in people] for r in recs}
        taken: set[str] = set()
        # record in ordine di "quanto e' sicura" la loro miglior opzione
        order = sorted(recs, key=lambda r: min(((o["tier"], abs(o["delta"] or 0)) for o in options[_record_id(r)]),
                                               default=(9, 0)))
        for rec in order:
            rid = _record_id(rec)
            free = [o for o in options[rid] if o["key"] not in taken and o["tier"] < 9
                    and not any(d["document_id"] in assigned_docs for d in people[o["key"]])]
            decision, chosen, why = cm.choose(free, len(people))

            chosen_doc = None
            if chosen:
                taken.add(chosen["key"])
                chosen_doc = cm.pick_file(people[chosen["key"]], rec)

            if rid in manual:
                chosen_doc = manual_doc[rid]
                decision, why = "manuale", manual[rid].get("nota") or ""

            if chosen_doc:
                assigned_docs.add(chosen_doc["document_id"])
                mapping[chosen_doc["document_id"]] = {"id_mcflash": rid, "file": chosen_doc["file"], "codice": code.upper(),
                                                      "in_indice": chosen_doc["in_indice"]}

            report.append({
                "codice": code.upper(), "id_mcflash": rid, "site": site_key(rec.get("site")),
                "nome": str(rec.get("nomi") or "").strip(), "ruolo": str(rec.get("ruolo") or "").strip(),
                "eta": rec.get("eta"), "sede": rec.get("sede"), "seniority": rec.get("seniority"),
                "decisione": decision, "motivo": why,
                "file": chosen_doc["file"] if chosen_doc else None,
                "document_id": chosen_doc["document_id"] if chosen_doc else None,
                "in_indice": chosen_doc["in_indice"] if chosen_doc else None,
                "candidati": [
                    {
                        **{k: o[k] for k in ("tier", "delta", "role_ok", "city_ok")},
                        "nato": str(people[o["key"]][0]["dob"] or "") or None,
                        "files": [
                            {"file": d["file"], "document_id": d["document_id"], "in_indice": d["in_indice"],
                             "ruolo": d["role"], "citta": d["city"],
                             "llm": judged[(d["document_id"], rid)]["coerente"], "motivo_llm": judged[(d["document_id"], rid)]["motivo"]}
                            for d in people[o["key"]]
                        ],
                    }
                    for o in sorted(options[rid], key=lambda o: (o["tier"], abs(o["delta"] or 0)))
                ],
            })

    decisions = collections.Counter(r["decisione"] for r in report)
    print(f"Record: {len(report)} | decisioni: {dict(decisions)} | CV in tabella: {len(mapping)}")

    if report_path:
        Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        print(f"Dettaglio: {report_path}")
    if dry_run:
        return
    out_path.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "index": settings.document_search_index_name,
        "mcflash_url": get_mcflash_candidates_client().base_url,
        "model": model,
        "documents": dict(sorted(mapping.items(), key=lambda kv: (kv[1]["codice"], kv[1]["id_mcflash"]))),
    }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"Tabella scritta: {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report", help="file JSON con il dettaglio di ogni record e dei suoi candidati")
    parser.add_argument("--dry-run", action="store_true", help="non scrive scripts/cv_mcflash_map.json")
    parser.add_argument("--reuse-report", help="riusa i giudizi LLM di un report precedente")
    parser.add_argument("--cv-dir", help="cartella con i CV originali: codice, nascita, citta' e ruolo dall'intestazione")
    parser.add_argument("--out", default=str(MAP_PATH), help="file della tabella (default scripts/cv_mcflash_map.json)")
    parser.add_argument("--overrides", default=str(OVERRIDES_PATH),
                        help="decisioni manuali per id MCFlash; stringa vuota = nessuna (es. altro MCFlash, id diversi)")
    parser.add_argument("--include-unindexed", action="store_true",
                        help="considera anche i CV di --cv-dir non ancora nell'indice (document_id dal nome file)")
    parser.add_argument("--tutti", action="store_true",
                        help="anche i record disabilitati e TalentUp (fuori dal perimetro dell'agente)")
    parser.add_argument("--no-llm", action="store_true",
                        help="non chiama l'LLM: le coppie non gia' giudicate usano il confronto dei ruoli")
    args = parser.parse_args()
    build(args.report, args.dry_run, args.reuse_report, args.cv_dir, args.no_llm,
          Path(args.out), Path(args.overrides) if args.overrides else None, args.include_unindexed, args.tutti)

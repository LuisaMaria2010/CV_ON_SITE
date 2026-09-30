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
from pathlib import Path
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
from utils.site import site_key  # noqa: E402

MAP_PATH = root / "scripts" / "cv_mcflash_map.json"
OVERRIDES_PATH = root / "scripts" / "cv_mcflash_map_overrides.json"

AGE_MIN_DELTA, AGE_MAX_DELTA = -1, 3
MAX_DELTA_SINGLE_PERSON = 12

JUDGE_SYSTEM = """\
Valuti se un CV puo' appartenere a una persona che, nell'anagrafica aziendale,
ha un certo ruolo professionale. Del CV conosci il ruolo che vi e' indicato e
le competenze tecniche estratte.

coerente = true se il CV descrive lo stesso mestiere del ruolo in anagrafica:
- le competenze sono tipiche di chi svolge quel ruolo, oppure
- il ruolo indicato nel CV e' lo stesso mestiere (sinonimi, traduzioni, varianti
  come "PM" / "Project Manager", "Frontend" / "Front End") e le competenze non lo
  contraddicono. Per ruoli non tecnici (project manager, business analyst,
  analista funzionale, help desk) e' normale che le competenze elencate siano
  strumenti tecnici: conta soprattutto il ruolo indicato nel CV.

coerente = false se il CV descrive un altro mestiere: non basta che le
competenze siano genericamente informatiche. Un ruolo di sviluppo generico (es.
"Software Developer", "Analista Programmatore") non e' coerente con un CV di
sicurezza, reti/sistemi, data science, mobile nativo, design o gestione progetti.

Rispondi SOLO con JSON: {"coerente": true|false, "motivo": "<max 20 parole>"}
"""

_DATE = r"(\d{1,2}\s*[/.\-]\s*\d{1,2}\s*[/.\-]\s*\d{2,4})"
_DOB_RES = [
    re.compile(r"(?:data\s+di\s+nascita|date\s+of\s+birth)\W{0,12}" + _DATE, re.I),
    re.compile(r"\|\s*(?:nome|name)\s*\|\s*[A-Za-z]{3}\s+" + _DATE, re.I),
]
_CITY_RE = re.compile(r"\|\s*(?:citt[aà]|city)\s*\|\s*([^|\n]+?)\s*\|", re.I)
_CITY_NOISE = {"remoto", "remote", "ibrido", "hybrid", "italia", "italy", "provincia"}
_CITY_ALIASES = {"turin": "torino", "milan": "milano", "rome": "roma", "florence": "firenze",
                 "naples": "napoli", "venice": "venezia", "genoa": "genova"}


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


def _parse_date(text: str) -> dt.date | None:
    parts = [int(p) for p in re.split(r"\s*[/.\-]\s*", text.strip())]
    if len(parts) != 3:
        return None
    d, m, y = parts
    if y < 100:
        y += 1900 if y > 30 else 2000
    try:
        day = dt.date(y, m, d)
    except ValueError:
        return None
    return day if 1940 <= day.year <= 2010 else None


_CODE_RE = re.compile(r"\|\s*(?:nome|name)\s*\|\s*([A-Za-z]{3})\b", re.I)


def _norm_filename(name: str) -> str:
    return unicodedata.normalize("NFC", name).strip().lower()


def _local_headers(cv_dir: str) -> dict[str, dict[str, Any]]:
    """Intestazione dei CV originali (modello MC): nome file -> codice, nascita, citta', ruolo.

    L'intestazione e' la fonte piu' affidabile: nell'indice il campo full_name e'
    a volte sbagliato ("CANDIDATE PROFILE", "ORK", ...).
    """
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
        lines = [l.strip() for l in text.splitlines() if l.strip()][:40]
        head: dict[str, Any] = {"file": path.name}
        for i, line in enumerate(lines):
            low = line.lower().rstrip(":")
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if ("data di nascita" in low or "date of birth" in low) and "dob" not in head:
                for cand in [line, nxt] + lines[i + 2:i + 3]:
                    m = re.search(_DATE, cand)
                    if m and (day := _parse_date(m.group(1))):
                        head["dob"] = day
                        break
            if low in ("nome", "name") and "code" not in head:
                head["code"] = nxt[:3].upper()
            elif low.startswith(("nome ", "name ")) and "code" not in head:
                parts = line.split()
                if len(parts) > 1 and len(parts[1]) >= 3:
                    head["code"] = parts[1][:3].upper()
                m = re.search(_DATE, line)
                if m and "dob" not in head and (day := _parse_date(m.group(1))):
                    head["dob"] = day
            if low in ("città", "citta", "city") and "city" not in head:
                head["city"] = nxt
            if low in ("ruolo", "role") and "role" not in head:
                head["role"] = next((l for l in lines[i + 1:i + 3]
                                     if l.lower() not in ("overview", "education", "experience", "esperienze")), None)
        out[_norm_filename(path.name)] = head
    return out


def _all_cv_documents(local: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
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
            "role": head.get("role") or next((c.get("role") for c in chunks if c.get("role")), None),
            "skills": skills,
            "dob": dob,
            "city": head.get("city") or (city_m.group(1) if city_m else None) or first.get("location"),
            "processed_at": max((c.get("processed_at") or "") for c in chunks),
        })
    return docs


def _judge(llm: AzureOpenAI, model: str, doc: dict[str, Any], role: str) -> dict[str, Any]:
    if not doc["skills"] and not doc.get("role"):
        return {"coerente": False, "motivo": "CV senza ruolo ne' skill estratti"}
    for attempt in range(4):
        try:
            resp = llm.chat.completions.create(
                model=model,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": JUDGE_SYSTEM},
                    {"role": "user", "content": (
                        f"Ruolo in anagrafica: {role}\n"
                        f"Ruolo indicato nel CV: {doc.get('role') or '(non indicato)'}\n"
                        f"Competenze del CV: {', '.join(doc['skills'][:60]) or '(nessuna)'}"
                    )},
                ],
            )
            break
        except Exception:  # servizio sovraccarico o rete: riprova con attesa crescente
            if attempt == 3:
                raise
            time.sleep(5 * (attempt + 1))
    out = json.loads(resp.choices[0].message.content)
    return {"coerente": bool(out.get("coerente")), "motivo": str(out.get("motivo") or "")}


# ------------------------------------------------------------------ confronti

def _age_on(dob: dt.date, day: dt.date) -> int:
    return day.year - dob.year - ((day.month, day.day) < (dob.month, dob.day))


def _norm_role(value: Any) -> str:
    text = MCFlashCandidatesClient._normalize_role_text(str(value or "").replace("-", " "))
    return f" {text} "


def _strict_role(cv_role: Any, mc_role: Any) -> bool:
    a, b = _norm_role(cv_role), _norm_role(mc_role)
    return bool(a.strip() and b.strip()) and (a in b or b in a)


def _cities(value: Any) -> set[str]:
    words = re.split(r"[\s/,()\-]+", str(value or "").lower())
    return {_CITY_ALIASES.get(w, w) for w in words if len(w) > 2 and w not in _CITY_NOISE}


def _record_id(record: dict[str, Any]) -> int:
    return int(record.get("id") or record.get("Id"))


# ------------------------------------------------------------------ build

def build(report_path: str | None, dry_run: bool, reuse_report: str | None = None, cv_dir: str | None = None,
          no_llm: bool = False) -> None:
    today = dt.date.today()
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
    all_docs = _all_cv_documents(local)
    docs_by_code: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for doc in all_docs:
        if doc["code"] in records_by_code:
            docs_by_code[doc["code"]].append(doc)
    n_docs = sum(len(d) for d in docs_by_code.values())
    print(f"File locali: {len(local)} | CV nell'indice: {len(all_docs)} (con file locale: "
          f"{sum(1 for d in all_docs if d['in_locale'])}) | con codice MCFlash: {n_docs} | con data di nascita: "
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

    overrides = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8")) if OVERRIDES_PATH.exists() else {}
    manual = {int(k): v for k, v in (overrides.get("records") or {}).items()}

    mapping: dict[str, dict[str, Any]] = {}
    report: list[dict[str, Any]] = []

    for code, recs in sorted(records_by_code.items()):
        docs = docs_by_code.get(code) or []
        # 1. persone: CV con la stessa data di nascita
        people: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
        for doc in docs:
            people[f"dob:{doc['dob']}" if doc["dob"] else f"doc:{doc['document_id']}"].append(doc)

        def evaluate(rec: dict[str, Any], key: str) -> dict[str, Any]:
            group = people[key]
            rid, eta = _record_id(rec), rec.get("eta") or 0
            dob = group[0]["dob"]
            delta = _age_on(dob, today) - eta if dob and eta else None
            role_ok = any(judged[(d["document_id"], rid)]["coerente"] or _strict_role(d["role"], rec.get("ruolo"))
                          for d in group)
            if delta is None:
                tier = 2 if role_ok else 9
            elif AGE_MIN_DELTA <= delta <= AGE_MAX_DELTA:
                tier = 1 if role_ok else 3
            else:
                tier = 4 if role_ok else 9
            return {
                "key": key, "tier": tier, "delta": delta, "role_ok": role_ok,
                "strict": any(_strict_role(d["role"], rec.get("ruolo")) for d in group),
                "city_ok": any(_cities(d["city"]) & _cities(rec.get("sede")) for d in group),
            }

        options = {_record_id(r): [evaluate(r, k) for k in people] for r in recs}
        taken: set[str] = set()
        # record in ordine di "quanto e' sicura" la loro miglior opzione
        order = sorted(recs, key=lambda r: min(((o["tier"], abs(o["delta"] or 0)) for o in options[_record_id(r)]),
                                               default=(9, 0)))
        for rec in order:
            rid = _record_id(rec)
            free = [o for o in options[rid] if o["key"] not in taken and o["tier"] < 9]
            decision, chosen, why = "nessun CV", None, "nessun CV con questo codice e mestiere o eta' compatibili"
            if free:
                best_tier = min(o["tier"] for o in free)
                cands = sorted(
                    [o for o in free if o["tier"] == best_tier],
                    key=lambda o: (not o["city_ok"], not o["strict"], abs(o["delta"] or 0)),
                )
                distinct = len(cands) == 1 or (
                    (cands[0]["city_ok"], cands[0]["strict"], abs(cands[0]["delta"] or 0))
                    != (cands[1]["city_ok"], cands[1]["strict"], abs(cands[1]["delta"] or 0))
                )
                if best_tier == 1 and distinct:
                    decision, chosen, why = "assegnato", cands[0], "eta' e mestiere compatibili"
                elif best_tier == 2 and distinct:
                    decision, chosen, why = "assegnato", cands[0], "mestiere compatibile, eta' non verificabile"
                elif best_tier == 4 and len(cands) == 1 and (
                    len(people) == 1 or abs(cands[0]["delta"]) <= MAX_DELTA_SINGLE_PERSON
                ):
                    decision, chosen, why = "assegnato", cands[0], (
                        f"unica persona con il mestiere giusto; eta' MCFlash non combacia ({cands[0]['delta']:+d})")
                else:
                    decision, chosen = "dubbio", None
                    why = {1: "piu' persone con eta' e mestiere compatibili",
                           2: "piu' persone con mestiere compatibile, eta' non verificabile",
                           3: "eta' compatibile ma mestiere diverso",
                           4: "eta' non combacia e piu' persone o differenza troppo grande"}[best_tier]

            chosen_doc = None
            if chosen:
                taken.add(chosen["key"])
                # Un file per persona: ruolo piu' vicino, poi citta', poi il piu' recente.
                newest_first = sorted(people[chosen["key"]], key=lambda d: d["processed_at"] or "", reverse=True)
                chosen_doc = min(newest_first, key=lambda d: (
                    not _strict_role(d["role"], rec.get("ruolo")),
                    not MCFlashCandidatesClient._role_matches(str(d["role"] or ""), rec.get("ruolo")),
                    not (_cities(d["city"]) & _cities(rec.get("sede"))),
                ))

            if rid in manual:
                doc_id = manual[rid].get("document_id")
                chosen_doc = next((d for d in docs if d["document_id"] == doc_id), None) if doc_id else None
                if doc_id and chosen_doc is None:
                    print(f"ATTENZIONE: override per {rid} su document_id sconosciuto {doc_id}")
                decision, why = "manuale", manual[rid].get("nota") or ""

            if chosen_doc:
                mapping[chosen_doc["document_id"]] = {"id_mcflash": rid, "file": chosen_doc["file"], "codice": code.upper()}

            report.append({
                "codice": code.upper(), "id_mcflash": rid, "site": site_key(rec.get("site")),
                "nome": str(rec.get("nomi") or "").strip(), "ruolo": str(rec.get("ruolo") or "").strip(),
                "eta": rec.get("eta"), "sede": rec.get("sede"), "seniority": rec.get("seniority"),
                "decisione": decision, "motivo": why,
                "file": chosen_doc["file"] if chosen_doc else None,
                "document_id": chosen_doc["document_id"] if chosen_doc else None,
                "candidati": [
                    {
                        **{k: o[k] for k in ("tier", "delta", "role_ok", "city_ok")},
                        "nato": str(people[o["key"]][0]["dob"] or "") or None,
                        "files": [
                            {"file": d["file"], "document_id": d["document_id"], "ruolo": d["role"], "citta": d["city"],
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
    MAP_PATH.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "index": settings.document_search_index_name,
        "mcflash_url": get_mcflash_candidates_client().base_url,
        "model": model,
        "documents": dict(sorted(mapping.items(), key=lambda kv: (kv[1]["codice"], kv[1]["id_mcflash"]))),
    }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"Tabella scritta: {MAP_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report", help="file JSON con il dettaglio di ogni record e dei suoi candidati")
    parser.add_argument("--dry-run", action="store_true", help="non scrive scripts/cv_mcflash_map.json")
    parser.add_argument("--reuse-report", help="riusa i giudizi LLM di un report precedente")
    parser.add_argument("--cv-dir", help="cartella con i CV originali: codice, nascita, citta' e ruolo dall'intestazione")
    parser.add_argument("--no-llm", action="store_true",
                        help="non chiama l'LLM: le coppie non gia' giudicate usano il confronto dei ruoli")
    args = parser.parse_args()
    build(args.report, args.dry_run, args.reuse_report, args.cv_dir, args.no_llm)

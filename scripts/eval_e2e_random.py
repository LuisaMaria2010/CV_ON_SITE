"""
Test end-to-end casuali su /api/ai-matcher-wrapper (ambiente dev).

Pesca N domande a caso dal set del chatbot (in run_evaluation_classifier.py) e
dal dataset scripts/new_dataset_225.jsonl, le lancia alternando i due site e
per ognuna controlla:
- perimetro: ogni candidato esiste in MCFlash, e' abilitato, non e' TalentUp ed e' visibile dal
  site (risorse vede risorse+candidati, candidati vede solo candidati)
- candidates = hit restituiti dalla ricerca all'agente (stessi id, stesso ordine)
- il testo cita tutti i candidati; nessun conteggio ("6 candidati", "i primi 3")
- verdict weak/none: la risposta dichiara all'inizio che non ci sono corrispondenze
- nessun blocco dati JSON nel testo; ricerche vuote

Uso (credenziali MCFlash da env, es. lette con az):
    python scripts/eval_e2e_random.py --n 20 --seed 123
    python scripts/eval_e2e_random.py --site mcflashcandidati --out eval_e2e_runs

Scrive <out>/e2e_<seed>.json e <out>/e2e_<seed>.md (risposte complete).
Env: MCFLASH_CANDIDATI_URL, MCFLASH_CANDIDATI_API_KEY; opz. AI_MATCHER_URL.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).parent.parent
DEFAULT_URL = "https://dev-function-ai-mc.azurewebsites.net/api/ai-matcher-wrapper"
SITES = ("mcflashcandidati", "mcflashrisorse")
VISIBLE = {"mcflashrisorse": {"mcflashrisorse", "mcflashcandidati"}, "mcflashcandidati": {"mcflashcandidati"}}

_NUM = r"(?:\d+|due|tre|quattro|cinque|sei|sette|otto|nove|dieci)"
_NOUN = r"(?:candidat[oi]|profil[oi]|risors[ae]|person[ae]|figur[ae]|consulent[ei]|sviluppator[ei]|nominativ[oi])"
# Solo spazi sulla stessa riga e numero non parte di una data ("dal 02/01"):
# altrimenti "02/01\n\nTutti questi candidati" sembra un conteggio.
COUNT_RE = re.compile(
    rf"(?<![/\d.-])\b(?:{_NUM}[ \t]+(?:\w+[ \t]+){{0,2}}{_NOUN}|(?:primi|altri|ulteriori)[ \t]+{_NUM})\b", re.I)
NO_MATCH_RE = re.compile(r"non (ho trovato|ci sono|sono stati trovati|risultano)|nessun (profilo|candidato)", re.I)


def counts_in_text(answer: str, question: str) -> list[str]:
    """Conteggi di candidati nel testo, esclusi quelli che ripetono un numero
    della domanda ("team di 2-3 persone")."""
    asked = set(re.findall(r"\d+", question))
    return [m.group(0) for m in COUNT_RE.finditer(answer)
            if not set(re.findall(r"\d+", m.group(0))) & asked]


def load_questions() -> list[tuple[str, str]]:
    src = (ROOT / "scripts" / "run_evaluation_classifier.py").read_text(encoding="utf-8")
    embedded = re.findall(r'"query":\s*"([^"]+)"', src[src.index("_EMBEDDED_DATASET"):])
    rows = [json.loads(l) for l in (ROOT / "scripts" / "new_dataset_225.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    return [("chatbot", q) for q in embedded] + [(r["title"], r["query"]) for r in rows]


def load_mcflash() -> dict[str, dict]:
    url, key = os.environ["MCFLASH_CANDIDATI_URL"], os.environ["MCFLASH_CANDIDATI_API_KEY"]
    rows, off = [], 0
    while True:
        req = urllib.request.Request(f"{url}?limit=1000&offset={off}", headers={"X-Api-Key": key})
        page = json.load(urllib.request.urlopen(req, timeout=60))
        page = page.get("items", page) if isinstance(page, dict) else page
        if not page:
            break
        rows += page
        off += len(page)
        if len(page) < 1000:
            break
    return {str(r["id"]): r for r in rows}


def run_one(url: str, mc: dict[str, dict], n: int, cat: str, q: str, site: str) -> dict:
    body = {"user_id": "claude-e2e-eval", "chat_id": f"e2e-{uuid.uuid4().hex[:8]}", "user_request": q,
            "site": site, "include_raw_response": True}
    t0 = time.time()
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    try:
        env = json.loads(raw.decode() or "{}")
    except ValueError:
        env, status = {"error": f"risposta non JSON: {raw[:300]!r}"}, (599 if status == 200 else status)
    data = env.get("data") if isinstance(env.get("data"), dict) else env
    ok = status == 200
    cands = (data.get("candidates") or []) if ok else []
    answer = (data.get("answer") or "") if ok else ""

    out_of_perimeter = []
    for c in cands:
        rec = mc.get(str(c.get("id_mcflash")))
        if rec is None:
            out_of_perimeter.append(f"{c.get('trigramma')}: id {c.get('id_mcflash')} non in anagrafica")
        elif str(rec.get("site")).split(".")[0].lower() not in VISIBLE[site]:
            out_of_perimeter.append(f"{c.get('trigramma')}: site non visibile")
        elif rec.get("abilitato") is not True:
            out_of_perimeter.append(f"{c.get('trigramma')}: non abilitato")
        elif rec.get("isTalentUp") is not False:
            out_of_perimeter.append(f"{c.get('trigramma')}: TalentUp")

    hits, verdict, requests = [], None, []
    for it in ((data.get("raw_response") or {}).get("output") or []) if ok else []:
        if it.get("type") == "openapi_call" and it.get("arguments"):
            requests.append(it["arguments"])
        if it.get("type") == "openapi_call_output":
            try:
                inner = json.loads(json.loads(it["output"])["response"])
                inner = inner.get("data", inner)
                hits = [str(h.get("id_mcflash")) for h in (inner.get("search_response") or {}).get("hits") or []]
                verdict = inner.get("verdict")
            except Exception as exc:  # formato inatteso: segnalato, non blocca
                hits, verdict = [f"parse-error {exc}"], None
    code_by_id = {k: str(v.get("nome")).strip() for k, v in mc.items()}
    cited = [i for i in hits if re.search(rf"\b{re.escape(code_by_id.get(i, '???'))}\b", answer)]
    return {
        "n": n, "cat": cat, "q": q, "site": site, "status": status, "secs": round(time.time() - t0, 1),
        "search_requests": requests, "verdict": verdict, "hits": hits,
        "candidates": [str(c.get("id_mcflash")) for c in cands],
        "candidates_equal_hits": [str(c.get("id_mcflash")) for c in cands] == hits,
        "missing_in_text": [i for i in hits if i not in cited],
        "counts_in_text": counts_in_text(answer, q),
        "weak_declared": bool(NO_MATCH_RE.search(answer[:400])),
        "json_in_text": "CANDIDATES_JSON" in answer or '{"candidates"' in answer,
        "out_of_perimeter": out_of_perimeter,
        "answer": answer if ok else json.dumps(data, ensure_ascii=False)[:400],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--seed", type=int, default=None, help="default: casuale")
    ap.add_argument("--site", choices=SITES, help="default: alterna i due site")
    ap.add_argument("--out", default="eval_e2e_runs")
    ap.add_argument("--workers", type=int, default=5)
    args = ap.parse_args()

    seed = args.seed if args.seed is not None else random.randrange(1_000_000)
    picked = random.Random(seed).sample(load_questions(), args.n)
    jobs = [(i, cat, q, args.site or SITES[i % 2]) for i, (cat, q) in enumerate(picked, 1)]
    mc = load_mcflash()
    url = os.environ.get("AI_MATCHER_URL", DEFAULT_URL)

    with ThreadPoolExecutor(args.workers) as pool:
        res = sorted(pool.map(lambda j: run_one(url, mc, *j), jobs), key=lambda r: r["n"])

    out = ROOT / args.out
    out.mkdir(exist_ok=True)
    (out / f"e2e_{seed}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    md = [f"# Test end-to-end ai-matcher-wrapper (seed {seed})\n"]
    for r in res:
        md.append(f"## {r['n']}. [{r['site']}] {r['q']}\n\n*{r['cat']} · HTTP {r['status']} · {r['secs']} s · "
                  f"verdict {r['verdict']} · ricerca: {'; '.join(r['search_requests']) or '-'}*\n\n{r['answer']}\n")
    (out / f"e2e_{seed}.md").write_text("\n".join(md), encoding="utf-8")

    print(f"seed={seed}  risultati in {out}")
    print(" # site        HTTP  hit cand=hit verdict  no-match | mancano | conteggi | json | perimetro | domanda")
    for r in res:
        weak = r["verdict"] in ("weak", "none")
        print(f"{r['n']:2} {r['site'][7:]:10} {r['status']:>4} {len(r['hits']):>4} {'si' if r['candidates_equal_hits'] else 'NO':>8} "
              f"{str(r['verdict']):8} {('si' if r['weak_declared'] else 'NO') if weak else '-':>4} | {r['missing_in_text'] or '-'} | "
              f"{r['counts_in_text'] or '-'} | {'SI' if r['json_in_text'] else '-'} | {r['out_of_perimeter'] or '-'} | {r['q'][:45]}")
    weak = [r for r in res if r["verdict"] in ("weak", "none")]
    print(f"\nHTTP ok: {sum(r['status'] == 200 for r in res)}/{len(res)} | candidates=hit: {sum(r['candidates_equal_hits'] for r in res)}/{len(res)}"
          f" | testo completo: {sum(not r['missing_in_text'] for r in res)}/{len(res)} | senza conteggi: {sum(not r['counts_in_text'] for r in res)}/{len(res)}"
          f" | weak/none dichiarati: {sum(r['weak_declared'] for r in weak)}/{len(weak)} | ricerche vuote: {sum(not r['hits'] for r in res)}"
          f" | json nel testo: {sum(r['json_in_text'] for r in res)} | fuori perimetro: {sum(len(r['out_of_perimeter']) for r in res)}")


if __name__ == "__main__":
    main()

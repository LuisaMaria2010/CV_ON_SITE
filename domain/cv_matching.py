"""Regole per collegare un CV anonimo a un record MCFlash.

Uniche per la ricostruzione della tabella CV -> MCFlash
(scripts/build_cv_mcflash_map.py) e per la ricerca dei CV nella dashboard
(services/cv_finder.py): stesso giudizio LLM, stesse soglie, stessa scelta.

Il codice anonimizzato (3 lettere) non e' univoco, quindi per ogni record si
guardano i CV con il suo codice:
1. i CV con la stessa data di nascita sono la stessa persona;
2. per ogni persona: eta' (data di nascita del CV contro eta' MCFlash, che e'
   l'eta' all'inserimento del record: va bene da -1 a +3), mestiere (un LLM
   giudica se il CV e' lo stesso mestiere del ruolo MCFlash, oppure i ruoli
   coincidono), citta';
3. livello (tier) di ogni persona, dal piu' forte:
   1 = eta' compatibile e mestiere coerente
   2 = eta' non verificabile, mestiere coerente
   3 = eta' compatibile ma mestiere diverso
   4 = mestiere coerente ma eta' che non torna
   9 = niente in comune
4. scelta: livello 1 o 2 senza parita' -> assegnato; livello 4 con una sola
   persona e differenza fino a 12 anni (o unico CV con quel codice) ->
   assegnato; il resto e' un caso dubbio.
5. della persona scelta si tiene un solo file: ruolo piu' vicino, citta'
   giusta, a parita' il piu' recente.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
import unicodedata
from pathlib import Path, PurePath
from typing import Any, Callable, Iterable

AGE_MIN_DELTA, AGE_MAX_DELTA = -1, 3
MAX_DELTA_SINGLE_PERSON = 12
JUDGE_EXCERPT_CHARS = 2500

# Decisioni manuali per record MCFlash (id -> {file | document_id, nota}).
OVERRIDES_PATH = Path(__file__).resolve().parent.parent / "infra" / "cv_mcflash_map_overrides.json"

JUDGE_SYSTEM = """\
Valuti se un CV puo' appartenere a una persona che, nell'anagrafica aziendale,
ha un certo ruolo professionale. Del CV conosci il ruolo che vi e' indicato, le
competenze tecniche estratte e, quando disponibile, un estratto del testo
(esperienze, formazione): usalo per capire il mestiere reale.

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

TIER_LABELS = {
    1: "età e mestiere compatibili",
    2: "mestiere compatibile, età non verificabile",
    3: "età compatibile ma mestiere diverso",
    4: "mestiere compatibile ma età che non torna",
    9: "né età né mestiere compatibili",
}
DOUBT_REASONS = {
    1: "piu' persone con eta' e mestiere compatibili",
    2: "piu' persone con mestiere compatibile, eta' non verificabile",
    3: "eta' compatibile ma mestiere diverso",
    4: "eta' non combacia e piu' persone o differenza troppo grande",
}
NO_CV_REASON = "nessun CV con questo codice e mestiere o eta' compatibili"

DATE = r"(\d{1,2}\s*[/.\-]\s*\d{1,2}\s*[/.\-]\s*\d{2,4})"
DOB_RES = [
    re.compile(r"(?:data\s+di\s+nascita|date\s+of\s+birth)\W{0,12}" + DATE, re.I),
    re.compile(r"\|\s*(?:nome|name)\s*\|\s*[A-Za-z]{3}\s+" + DATE, re.I),
]
CITY_RE = re.compile(r"\|\s*(?:citt[aà]|city)\s*\|\s*([^|\n]+?)\s*\|", re.I)
CODE_RE = re.compile(r"\|\s*(?:nome|name)\s*\|\s*([A-Za-z]{3})\b", re.I)
_CITY_NOISE = {"remoto", "remote", "ibrido", "hybrid", "italia", "italy", "provincia"}
_CITY_ALIASES = {"turin": "torino", "milan": "milano", "rome": "roma", "florence": "firenze",
                 "naples": "napoli", "venice": "venezia", "genoa": "genova"}
_HEADER_LABELS = {"città", "citta", "city", "ruolo", "role", "data di nascita", "date of birth", "overview"}
_FILE_NOISE_RE = re.compile(r"(?i)skill[\s_]*tecnico|(?<![a-z])mc(?![a-z])")
_FILE_CODE_RE = re.compile(r"(?<![A-Za-z])([A-Za-z]{3})(?![A-Za-z])")


# ---------------------------------------------------------------- codici e intestazione

def person_key(value: Any) -> str:
    """Codice normalizzato (minuscolo, solo lettere e cifre), come nel pipeline di ricerca."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def norm_filename(name: str) -> str:
    return unicodedata.normalize("NFC", name).strip().lower()


def parse_date(text: str) -> dt.date | None:
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


def file_code(filename: str) -> str | None:
    """Codice di 3 lettere nel nome del file ("Skill_Tecnico_DCA_MC.pdf" -> "DCA")."""
    m = _FILE_CODE_RE.search(_FILE_NOISE_RE.sub(" ", PurePath(filename).stem))
    return m.group(1).upper() if m else None


def codes(header_code: str | None, filename: str) -> set[str]:
    """Codici del CV: quello dell'intestazione e quello nel nome del file.

    Non sempre coincidono (es. "Skill_Tecnico_FDB.pdf" con "FDE" nell'intestazione,
    "Skill_Tecnico_FJC.pdf" per un trittico MCFlash "FCU"): basta che uno dei due
    sia il codice del record.
    """
    return {person_key(c) for c in (header_code, file_code(filename)) if c} - {""}


def header_from_text(text: str, filename: str) -> dict[str, Any]:
    """Intestazione di un CV nel modello MC (testo del file originale): codice,
    nascita, citta', ruolo. E' la fonte piu' affidabile: nell'indice il campo
    full_name e' a volte sbagliato ("CANDIDATE PROFILE", "ORK", ...)."""
    all_lines = [l.strip() for l in text.splitlines() if l.strip()]
    lines = all_lines[:40]
    head: dict[str, Any] = {"file": filename, "text": "\n".join(all_lines)}
    for i, line in enumerate(lines):
        low = line.lower().rstrip(":")
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if ("data di nascita" in low or "date of birth" in low) and "dob" not in head:
            for cand in [line, nxt] + lines[i + 2:i + 3]:
                m = re.search(DATE, cand)
                if m and (day := parse_date(m.group(1))):
                    head["dob"] = day
                    break
        if low in ("nome", "name") and "code" not in head and nxt.lower().rstrip(":") not in _HEADER_LABELS:
            head["code"] = nxt[:3].upper()
        elif low.startswith(("nome ", "name ")) and "code" not in head:
            parts = line.split()
            if len(parts) > 1 and len(parts[1]) >= 3:
                head["code"] = parts[1][:3].upper()
            m = re.search(DATE, line)
            if m and "dob" not in head and (day := parse_date(m.group(1))):
                head["dob"] = day
        if low in ("città", "citta", "city") and "city" not in head:
            head["city"] = nxt
        if low in ("ruolo", "role") and "role" not in head:
            head["role"] = next((l for l in lines[i + 1:i + 3]
                                 if l.lower() not in ("overview", "education", "experience", "esperienze")), None)
    if "dob" not in head:
        # Data di nascita fuori dall'intestazione: la si cerca in tutto il testo.
        for rx in DOB_RES:
            m = rx.search(head["text"])
            if m and (day := parse_date(m.group(1))):
                head["dob"] = day
                break
    if "code" not in head and (fc := file_code(filename)):
        # Nome vuoto nell'intestazione: il codice resta quello nel nome del file.
        head["code"] = fc
    return head


def read_document_text(data: bytes, filename: str) -> str:
    """Testo di un CV PDF o DOCX (vuoto se il file non si legge)."""
    import io

    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    try:
        if suffix == "pdf":
            import fitz
            with fitz.open(stream=data, filetype="pdf") as pdf:
                return "\n".join(page.get_text() for page in pdf)
        if suffix == "docx":
            import docx
            return "\n".join(p.text for p in docx.Document(io.BytesIO(data)).paragraphs)
    except Exception:  # file danneggiato: resta il dato dell'indice
        return ""
    return ""


# ---------------------------------------------------------------- confronti

def age_on(dob: dt.date, day: dt.date) -> int:
    return day.year - dob.year - ((day.month, day.day) < (dob.month, dob.day))


def norm_role(value: Any) -> str:
    from infra.mcflash_candidates import MCFlashCandidatesClient

    text = MCFlashCandidatesClient._normalize_role_text(str(value or "").replace("-", " "))
    return f" {text} "


def strict_role(cv_role: Any, mc_role: Any) -> bool:
    a, b = norm_role(cv_role), norm_role(mc_role)
    return bool(a.strip() and b.strip()) and (a in b or b in a)


def role_matches(cv_role: Any, mc_role: Any) -> bool:
    from infra.mcflash_candidates import MCFlashCandidatesClient

    return MCFlashCandidatesClient._role_matches(str(cv_role or ""), mc_role)


def cities(value: Any) -> set[str]:
    words = re.split(r"[\s/,()\-]+", str(value or "").lower())
    return {_CITY_ALIASES.get(w, w) for w in words if len(w) > 2 and w not in _CITY_NOISE}


# ---------------------------------------------------------------- giudizio LLM sul mestiere

def judge(llm: Any, model: str, doc: dict[str, Any], role: str) -> dict[str, Any]:
    """{coerente, motivo}: il CV e' lo stesso mestiere del ruolo in anagrafica?"""
    excerpt = re.sub(r"\s+", " ", doc.get("text") or "")[:JUDGE_EXCERPT_CHARS]
    if not doc.get("skills") and not doc.get("role") and not excerpt:
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
                        f"Competenze del CV: {', '.join((doc.get('skills') or [])[:60]) or '(nessuna)'}\n"
                        f"Estratto del CV: {excerpt or '(non disponibile)'}"
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


# ---------------------------------------------------------------- persone, livelli, scelta

def group_people(docs: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """CV con la stessa data di nascita = stessa persona."""
    people: dict[str, list[dict[str, Any]]] = {}
    for doc in docs:
        people.setdefault(f"dob:{doc['dob']}" if doc.get("dob") else f"doc:{doc['document_id']}", []).append(doc)
    return people


def evaluate(rec: dict[str, Any], key: str, group: list[dict[str, Any]],
             coherent: Callable[[dict[str, Any]], bool], today: dt.date) -> dict[str, Any]:
    """Livello di una persona (gruppo di CV) per un record. `coherent(doc)` e' il giudizio LLM."""
    eta = rec.get("eta") or 0
    dob = group[0].get("dob")
    delta = age_on(dob, today) - eta if dob and eta else None
    role_ok = any(coherent(d) or strict_role(d.get("role"), rec.get("ruolo")) for d in group)
    if delta is None:
        tier = 2 if role_ok else 9
    elif AGE_MIN_DELTA <= delta <= AGE_MAX_DELTA:
        tier = 1 if role_ok else 3
    else:
        tier = 4 if role_ok else 9
    return {
        "key": key, "tier": tier, "delta": delta, "role_ok": role_ok,
        "strict": any(strict_role(d.get("role"), rec.get("ruolo")) for d in group),
        "city_ok": any(cities(d.get("city")) & cities(rec.get("sede")) for d in group),
    }


def choose(free: list[dict[str, Any]], n_people: int) -> tuple[str, dict[str, Any] | None, str]:
    """(decisione, opzione scelta, motivo) tra le persone libere di un record."""
    if not free:
        return "nessun CV", None, NO_CV_REASON
    best_tier = min(o["tier"] for o in free)
    cands = sorted([o for o in free if o["tier"] == best_tier],
                   key=lambda o: (not o["city_ok"], not o["strict"], abs(o["delta"] or 0)))
    distinct = len(cands) == 1 or (
        (cands[0]["city_ok"], cands[0]["strict"], abs(cands[0]["delta"] or 0))
        != (cands[1]["city_ok"], cands[1]["strict"], abs(cands[1]["delta"] or 0))
    )
    if best_tier == 1 and distinct:
        return "assegnato", cands[0], "eta' e mestiere compatibili"
    if best_tier == 2 and distinct:
        return "assegnato", cands[0], "mestiere compatibile, eta' non verificabile"
    if best_tier == 4 and len(cands) == 1 and (n_people == 1 or abs(cands[0]["delta"]) <= MAX_DELTA_SINGLE_PERSON):
        return "assegnato", cands[0], f"unica persona con il mestiere giusto; eta' MCFlash non combacia ({cands[0]['delta']:+d})"
    return "dubbio", None, DOUBT_REASONS[best_tier]


def pick_file(group: list[dict[str, Any]], rec: dict[str, Any]) -> dict[str, Any]:
    """Un file per persona: ruolo piu' vicino, poi citta', poi il piu' recente."""
    newest_first = sorted(group, key=lambda d: d.get("processed_at") or "", reverse=True)
    return min(newest_first, key=lambda d: (
        not strict_role(d.get("role"), rec.get("ruolo")),
        not role_matches(d.get("role"), rec.get("ruolo")),
        not (cities(d.get("city")) & cities(rec.get("sede"))),
    ))


def load_overrides(path: Path | None = OVERRIDES_PATH) -> dict[int, dict[str, Any]]:
    """Decisioni manuali per id MCFlash ({} se il file non c'e')."""
    if not path or not Path(path).exists():
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {int(k): v for k, v in (data.get("records") or {}).items()}

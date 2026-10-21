"""Scheda colloquio del recruiter: testo con etichette ("TARIFFA: 325", "SENIORITY: Medium"...).

La scheda contiene cio' che un CV non dice: tariffa, seniority, trigramma,
preavviso, RAL attesa, impressioni del colloquio. parse_sheet() legge i campi
con l'etichetta (in maiuscolo, a inizio riga, seguita da ":"); il valore
continua sulle righe dopo fino alla prossima etichetta. merge_sheet() unisce
la scheda ai dati estratti dal CV (o, senza CV, dalla scheda stessa letta
dall'LLM):

- anagrafica, ruolo, seniority, tariffa e disponibilita': vince la scheda, che
  il recruiter ha scritto dopo il colloquio;
- skill e lingue: quelle del CV piu' quelle della scheda (per le lingue vale il
  livello della scheda);
- esperienze, formazione, trainings e certificazioni: un misto delle due fonti,
  qualunque sia la piu' ricca. Una voce che c'e' in tutte e due (stesso
  periodo e stesso ruolo o azienda; stesso titolo) resta una: si tiene quella
  del CV e si completano i campi vuoti con la scheda. Le voci che ha una sola
  fonte si aggiungono. Il voto scritto in TITOLO DI STUDIO va nel titolo;
- il resto del colloquio va in edited["colloquio"], che non finisce nel CV MC.
"""
from __future__ import annotations

import copy
import datetime as dt
import re
import unicodedata
from typing import Any, Callable

from domain.candidate_identity import split_full_name, trittico
from domain.role_catalog import match_catalog_role

# Prefisso dell'etichetta (normalizzata: maiuscole senza accenti ne' apostrofi) -> campo.
# I prefissi piu' lunghi prima: "DATA COLLOQUIO" non deve finire in un altro campo.
_LABELS: tuple[tuple[str, str], ...] = (
    ("EVENTUALE SPECIFICA CITTADINANZA", "cittadinanza"),
    ("PRECEDENTI CONTRATTI", "contratti_ti"),
    ("ESPERIENZA LAVORATIVA", "esperienza"),
    ("CATEGORIA PROTETTA", "categoria_protetta"),
    ("ULTIMO CONTRATTO", "ultimo_contratto"),
    ("TITOLO DI STUDIO", "titolo_studio"),
    ("DATA COLLOQUIO", "data_colloquio"),
    ("CITTADINANZA", "cittadinanza"),
    ("ASPETTATIVA", "aspettativa"),
    ("STANDARD MC", "standard_mc"),
    ("IMPRESSIONI", "impressioni"),
    ("SENIORITY", "seniority"),
    ("RECRUITER", "recruiter"),
    ("PREAVVISO", "preavviso"),
    ("TARIFFA", "tariffa"),
    ("LINGUE", "lingue"),
    ("RUOLO", "ruolo"),
    ("SKILL", "skill"),
    ("CITTA", "citta"),
    ("CCNL", "ccnl"),
    ("NOME", "nome"),
    ("SEDE", "sede_trasferte"),
    ("ETA", "eta"),
)

# Campi che restano note del colloquio (edited["colloquio"]), in quest'ordine.
INTERVIEW_FIELDS: dict[str, str] = {
    "recruiter": "Recruiter",
    "data_colloquio": "Data colloquio",
    "standard_mc": "Standard MC",
    "categoria_protetta": "Categoria protetta",
    "cittadinanza": "Cittadinanza / permesso di soggiorno",
    "titolo_studio": "Titolo di studio",
    "esperienza": "Esperienza lavorativa attuale",
    "sede_trasferte": "Sede e trasferte",
    "ultimo_contratto": "Ultimo contratto",
    "ccnl": "CCNL",
    "contratti_ti": "Precedenti contratti TI",
    "preavviso": "Preavviso",
    "ral_attesa": "RAL attesa",
    "aspettativa": "Aspettativa economica e professionale",
    "impressioni": "Impressioni",
}

_SENIORITY = (("neo", "Neo"), ("junior", "Junior"), ("medium", "Medium"), ("mid", "Medium"), ("senior", "Senior"))

_LANGUAGES = ("inglese", "francese", "tedesco", "spagnolo", "portoghese", "italiano", "russo", "cinese",
              "arabo", "olandese", "giapponese", "rumeno", "polacco", "greco", "svedese", "turco")
# Parola nel testo -> livello del modello MC (domain/mc_levels.LANGUAGE_LEVELS).
_LANGUAGE_LEVELS = (
    ("madrelingua", "Madrelingua"), ("fluente", "Fluente"), ("avanzat", "Avanzato"), ("ottim", "Avanzato"),
    ("intermedi", "Intermedio"), ("buon", "Intermedio"), ("discret", "Intermedio"), ("base", "Base"),
    ("scolastic", "Base"), ("elementare", "Base"),
    ("c2", "Fluente"), ("c1", "Avanzato"), ("b2", "Intermedio"), ("b1", "Intermedio"), ("a2", "Base"), ("a1", "Base"),
)


def _norm_label(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    no_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", re.sub(r"['’`]", "", no_marks)).strip().upper()


def _label_of(line: str) -> tuple[str, str] | None:
    """(campo, valore sulla riga) se la riga apre un campo: etichetta in maiuscolo prima dei due punti."""
    head, sep, rest = line.partition(":")
    if not sep or not head.strip() or len(head) > 80 or head != head.upper():
        return None
    label = _norm_label(head)
    for prefix, key in _LABELS:
        if label.startswith(prefix):
            return key, rest.strip()
    return None


def split_sections(text: str) -> dict[str, str]:
    """Valore (testo) di ogni campo della scheda; le righe senza etichetta
    continuano il campo precedente."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw in (text or "").replace("\xa0", " ").splitlines():
        line = raw.strip()
        found = _label_of(line)
        if found:
            current, value = found
            sections.setdefault(current, [])
            if value:
                sections[current].append(value)
        elif current and line:
            sections[current].append(line)
    return {key: "\n".join(lines).strip() for key, lines in sections.items()}


def _first_int(text: str | None) -> int | None:
    match = re.search(r"\d+", (text or "").replace(".", ""))
    return int(match.group()) if match else None


def _name_and_code(text: str) -> tuple[str | None, str | None, str | None]:
    """"STEFANO PRANDI - SPR" -> ("Stefano", "Prandi", "SPR"). Si sceglie la divisione
    tra nome e cognome che ridà il trigramma scritto, se c'e'."""
    code = None
    match = re.search(r"[-–/]\s*([A-Za-z]{3})\s*$", text)
    if match:
        code, text = match.group(1).upper(), text[:match.start()]
    words = [w.title() for w in text.split()]
    if not words:
        return None, None, code
    cuts = [(" ".join(words[:i]), " ".join(words[i:])) for i in range(1, len(words))]
    for first, last in cuts + [(b, a) for a, b in cuts]:
        if code and trittico(first, last) == code:
            return first, last, code
    first, last = split_full_name(" ".join(words))
    return first, last, code


def _seniority(text: str | None) -> str | None:
    low = (text or "").casefold()
    return next((value for word, value in _SENIORITY if word in low), None)


def _disponibilita(preavviso: str | None) -> str | None:
    """Disponibilita' MCFlash dal preavviso: "15 giorni dal 1 o dal 15" -> "15 gg"."""
    text = (preavviso or "").strip()
    if not text:
        return None
    if "immediat" in text.casefold():
        return "Immediata"
    match = re.search(r"(\d+)\s*(giorni|gg|giorno)", text, re.IGNORECASE)
    if match:
        return f"{match.group(1)} gg"
    match = re.search(r"(\d+)\s*(mesi|mese)", text, re.IGNORECASE)
    if match:
        return f"{match.group(1)} mesi"
    return text[:40]


def parse_languages(text: str | None) -> list[dict[str, Any]]:
    """Lingue citate nel testo, con il livello della frase in cui compaiono."""
    out = []
    for clause in re.split(r"[;\n]|,(?![^()]*\))", text or ""):
        low = clause.casefold()
        level = next((value for word, value in _LANGUAGE_LEVELS if re.search(rf"\b{word}", low)), None)
        for lang in _LANGUAGES:
            if re.search(rf"\b{lang}\b", low) and all(item["lang"] != lang for item in out):
                out.append({"lang": lang, "level": level})
    return out


def parse_skills(text: str | None) -> list[dict[str, Any]]:
    """Skill separate da virgole; le virgole tra parentesi restano dentro la skill
    ("AWS (S3, Athena)")."""
    parts = re.split(r",(?![^()]*\))|;|\n", text or "")
    seen: set[str] = set()
    out = []
    for part in parts:
        name = part.strip().strip(".").strip()
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            out.append({"name": name, "level": None})
    return out


def _ral(aspettativa: str | None) -> str | None:
    match = re.search(r"RAL[^0-9]{0,30}([0-9][0-9.,/\-–\s]*(?:k|K|mila)?\s*€?)", aspettativa or "")
    return match.group(1).strip() if match else None


def parse_sheet(text: str) -> dict[str, Any]:
    """Dati della scheda nel formato di /api/extract?profile=mcflash, solo i campi
    presenti, piu' mcflash (tariffa, seniority, disponibilita') e colloquio."""
    s = split_sections(text)
    out: dict[str, Any] = {}
    if s.get("nome"):
        first, last, code = _name_and_code(s["nome"].splitlines()[0])
        if first or last:
            out.update(first_name=first, last_name=last, full_name=" ".join(p for p in (first, last) if p))
        out["trittico"] = code or trittico(first, last)
    if s.get("ruolo"):
        out["role"] = s["ruolo"].splitlines()[0].strip()
    if _first_int(s.get("eta")) is not None:
        out["age"] = _first_int(s.get("eta"))
    city = (s.get("citta") or "").splitlines()[0].strip(" .") if s.get("citta") else ""
    if city:
        out["location"] = city
    if s.get("skill"):
        out["cv_skills"] = parse_skills(s["skill"])
    if s.get("lingue"):
        out["languages"] = parse_languages(s["lingue"])
    mcflash = {
        "budget": _first_int(s.get("tariffa")),
        "seniority": _seniority(s.get("seniority")),
        "disponibilita": _disponibilita(s.get("preavviso")),
    }
    out["mcflash"] = {k: v for k, v in mcflash.items() if v not in (None, "")}
    colloquio = {key: s.get(key) for key in INTERVIEW_FIELDS if s.get(key)}
    if _ral(s.get("aspettativa")):
        colloquio["ral_attesa"] = _ral(s.get("aspettativa"))
    colloquio["testo"] = (text or "").strip()
    out["colloquio"] = colloquio
    return out


def _merge_languages(base: list[dict[str, Any]], sheet: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_lang = {str(item.get("lang") or "").casefold(): dict(item) for item in base if item.get("lang")}
    for item in sheet:
        key = item["lang"].casefold()
        if key in by_lang:
            by_lang[key]["level"] = item.get("level") or by_lang[key].get("level")
        else:
            by_lang[key] = dict(item)
    return list(by_lang.values())


def _merge_skills(base: list[dict[str, Any]], sheet: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Skill del CV piu' quelle nuove della scheda. Non e' nuova una skill le cui
    parole stanno tutte in una gia' presente, o viceversa ("S3" e "AWS S3")."""
    out = list(base)
    for item in sheet:
        name = str(item.get("name") or "")
        words = _words(name)
        if any(name.casefold() == str(o.get("name") or "").casefold()
               or (words and (o_words := _words(o.get("name"))) and (words <= o_words or o_words <= words))
               for o in out):
            continue
        out.append(item)
    return out


# ---------------------------------------------------------------- misto CV + scheda

_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = {"di", "in", "e", "ed", "il", "la", "lo", "le", "del", "della", "dei", "per", "con", "su", "the",
              "of", "and", "at", "srl", "spa", "sas", "snc", "s", "p", "a", "r", "l"}
# Voti italiani di laurea e diploma (104/110, 110/110 e lode, 85/100, 52/60): niente /10 o /30, che si
# confondono con le date.
_GRADE_RE = re.compile(r"\b\d{2,3}\s*/\s*(?:60|100|110)\b(?:\s*(?:e\s*)?(?:con\s*)?lode)?", re.IGNORECASE)
# Quanto si devono somigliare due testi: parole in comune / parole del piu' corto.
# Ruolo e azienda: piu' di meta' ("Data Engineer" e "Data Analyst" sono diversi).
_ROLE_OR_COMPANY_SIMILARITY = 0.6
_TITLE_SIMILARITY = 0.8


def _words(text: Any) -> set[str]:
    plain = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return {w for w in _WORD_RE.findall(plain) if len(w) > 1 and w not in _STOPWORDS}


def _similar(a: Any, b: Any, threshold: float) -> bool:
    wa, wb = _words(a), _words(b)
    return bool(wa and wb) and len(wa & wb) / min(len(wa), len(wb)) >= threshold


def _month(value: Any) -> int | None:
    """Mese assoluto (anno * 12 + mese) da una data o da "YYYY", "YYYY-MM", "YYYY-MM-DD"."""
    if isinstance(value, dt.date):
        return value.year * 12 + value.month - 1
    match = re.match(r"\s*(\d{4})(?:-(\d{1,2}))?", str(value or ""))
    return int(match.group(1)) * 12 + int(match.group(2) or 1) - 1 if match else None


def _period(entry: dict[str, Any], today: dt.date) -> tuple[int, int] | None:
    start = _month(entry.get("start_date"))
    if start is None:
        return None
    end = _month(entry.get("end_date"))
    if end is None:
        end = _month(today) if entry.get("ongoing") or not entry.get("end_date") else start
    return start, max(start, end)


def _same_experience(cv: dict[str, Any], sheet: dict[str, Any], today: dt.date) -> bool:
    """Stessa esperienza: periodi che si sovrappongono e ruolo simile (per almeno
    meta' del periodo piu' corto), oppure stessa azienda con un altro ruolo ma
    per almeno meta' del periodo piu' lungo: nella stessa azienda due ruoli in
    tempi diversi sono due esperienze, anche se una fonte li riassume in una.
    Senza date: azienda e ruolo simili."""
    role = _similar(cv.get("role"), sheet.get("role"), _ROLE_OR_COMPANY_SIMILARITY)
    company = _similar(cv.get("company"), sheet.get("company"), _ROLE_OR_COMPANY_SIMILARITY)
    pc, ps = _period(cv, today), _period(sheet, today)
    if pc is None or ps is None:
        return role and company
    overlap = min(pc[1], ps[1]) - max(pc[0], ps[0]) + 1
    lengths = (pc[1] - pc[0] + 1, ps[1] - ps[0] + 1)
    if overlap <= 0:
        return False
    return (role and overlap * 2 >= min(lengths)) or (company and overlap * 2 >= max(lengths))


def _complete(entry: dict[str, Any], other: dict[str, Any], keys: tuple[str, ...]) -> None:
    """Riempie i campi vuoti di `entry` con quelli di `other`; le liste si uniscono."""
    for key in keys:
        if isinstance(entry.get(key), list) or isinstance(other.get(key), list):
            current = list(entry.get(key) or [])
            seen = {str(item).casefold() for item in current}
            entry[key] = current + [item for item in other.get(key) or [] if str(item).casefold() not in seen]
        elif entry.get(key) in (None, "") and other.get(key) not in (None, ""):
            entry[key] = copy.deepcopy(other[key])


def _newest_first(entries: list[dict[str, Any]], when: Callable[[dict[str, Any]], int | None]) -> list[dict[str, Any]]:
    """Dalla piu' recente; quelle senza data in fondo, nell'ordine in cui erano."""
    return sorted(entries, key=lambda e: (when(e) is None, -(when(e) or 0)))


def _merge_experiences(cv: list[dict[str, Any]], sheet: list[dict[str, Any]], today: dt.date) -> list[dict[str, Any]]:
    out = copy.deepcopy(cv)
    added = False
    for item in sheet:
        match = next((e for e in out if _same_experience(e, item, today)), None)
        if match is None:
            out.append(copy.deepcopy(item))
            added = True
            continue
        _complete(match, item, ("start_date", "end_date", "company", "role", "description", "technologies"))
        if item.get("ongoing") and not match.get("end_date"):
            match["ongoing"] = True
        if not match.get("projects") and item.get("projects"):
            match["projects"] = copy.deepcopy(item["projects"])
    return _newest_first(out, lambda e: _month(e.get("start_date"))) if added else out


def _merge_by_title(cv: list[dict[str, Any]], sheet: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    out = copy.deepcopy(cv)
    added = False
    for item in sheet:
        match = next((e for e in out if _similar(e.get("title"), item.get("title"), _TITLE_SIMILARITY)), None)
        if match is None:
            out.append(copy.deepcopy(item))
            added = True
        else:
            _complete(match, item, keys)
    return _newest_first(out, lambda e: _month(e.get("year"))) if added else out


def _merge_strings(cv: list[Any], sheet: list[Any]) -> list[Any]:
    out = list(cv)
    for item in sheet:
        if not any(_similar(item, other, _TITLE_SIMILARITY) for other in out):
            out.append(item)
    return out


def _add_grades(education: list[dict[str, Any]], titolo_studio: str | None) -> None:
    """Il voto della scheda ("Laurea ..., voto: 104/110") va nel titolo, se il CV non lo dice."""
    for line in (titolo_studio or "").splitlines():
        grade = _GRADE_RE.search(line)
        if not grade:
            continue
        line_title = line[:grade.start()]
        for entry in education:
            title = str(entry.get("title") or "")
            if _similar(title, line_title, _TITLE_SIMILARITY) and not _GRADE_RE.search(title):
                entry["title"] = f"{title} – {' '.join(grade.group().split())}"
                break


def merge_sheet(base: dict[str, Any] | None, sheet: dict[str, Any],
                from_text: dict[str, Any] | None = None, today: dt.date | None = None) -> dict[str, Any]:
    """Dati del form: `base` (estratto del CV, o None), la scheda letta da
    parse_sheet e `from_text` (la scheda letta dall'LLM: esperienze, formazione,
    trainings e certificazioni da unire a quelle del CV). Applicata due volte
    da' lo stesso risultato."""
    today = today or dt.date.today()
    out = copy.deepcopy(base or from_text or {})
    if from_text and base:
        out["experiences"] = _merge_experiences(out.get("experiences") or [], from_text.get("experiences") or [], today)
        out["education"] = _merge_by_title(out.get("education") or [], from_text.get("education") or [],
                                           ("year", "institution"))
        out["trainings"] = _merge_by_title(out.get("trainings") or [], from_text.get("trainings") or [],
                                           ("year", "provider"))
        out["certifications"] = _merge_strings(out.get("certifications") or [], from_text.get("certifications") or [])
        out["cv_skills"] = _merge_skills(out.get("cv_skills") or [], from_text.get("cv_skills") or [])
        out["languages"] = _merge_languages(out.get("languages") or [], from_text.get("languages") or [])
    for key in ("first_name", "last_name", "full_name", "trittico", "role", "age", "location"):
        if sheet.get(key) not in (None, ""):
            out[key] = sheet[key]
    if sheet.get("role"):
        out["catalog_role"] = match_catalog_role(sheet["role"]) or out.get("catalog_role")
    out["cv_skills"] = _merge_skills(out.get("cv_skills") or [], sheet.get("cv_skills") or [])
    out["languages"] = _merge_languages(out.get("languages") or [], sheet.get("languages") or [])
    for key in ("experiences", "education", "trainings", "certifications"):
        out.setdefault(key, [])
    _add_grades(out["education"], (sheet.get("colloquio") or {}).get("titolo_studio"))
    out["mcflash"] = {**(out.get("mcflash") or {}), **sheet.get("mcflash", {})}
    out["colloquio"] = dict(sheet.get("colloquio") or {})
    return out

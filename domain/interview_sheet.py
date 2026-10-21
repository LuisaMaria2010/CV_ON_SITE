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
- esperienze e formazione: dal CV; dalla scheda solo se il CV non le ha;
- il resto del colloquio va in edited["colloquio"], che non finisce nel CV MC.
"""
from __future__ import annotations

import copy
import re
import unicodedata
from typing import Any

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
    names = {str(item.get("name") or "").casefold() for item in base}
    return list(base) + [item for item in sheet if item["name"].casefold() not in names]


def merge_sheet(base: dict[str, Any] | None, sheet: dict[str, Any],
                from_text: dict[str, Any] | None = None) -> dict[str, Any]:
    """Dati del form: `base` (estratto del CV, o None), la scheda letta da
    parse_sheet e `from_text` (la scheda letta dall'LLM, per esperienze e
    formazione quando il CV non c'e' o non le ha)."""
    out = copy.deepcopy(base or from_text or {})
    if from_text and base:
        for key in ("experiences", "education", "trainings", "certifications"):
            if not out.get(key) and from_text.get(key):
                out[key] = copy.deepcopy(from_text[key])
    for key in ("first_name", "last_name", "full_name", "trittico", "role", "age", "location"):
        if sheet.get(key) not in (None, ""):
            out[key] = sheet[key]
    if sheet.get("role"):
        out["catalog_role"] = match_catalog_role(sheet["role"]) or out.get("catalog_role")
    out["cv_skills"] = _merge_skills(out.get("cv_skills") or [], sheet.get("cv_skills") or [])
    out["languages"] = _merge_languages(out.get("languages") or [], sheet.get("languages") or [])
    for key in ("experiences", "education", "trainings", "certifications"):
        out.setdefault(key, [])
    out["mcflash"] = {**(out.get("mcflash") or {}), **sheet.get("mcflash", {})}
    out["colloquio"] = dict(sheet.get("colloquio") or {})
    return out

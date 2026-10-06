"""Candidati MCFlash per la dashboard: riga sintetica, ricerca e match.

Nel record MCFlash `nome` e' il trittico (es. "DCA") e `nomi` il nome completo.

Match di un CV caricato (docs/img/05-match-candidato.svg nel repo dashboard):
1. record con lo stesso trittico: nessuno -> "nuovo";
2. tra questi si tengono quelli con lo stesso nome:
   - uno -> "aggiorna" quel record;
   - nessuno -> "nuovo" (omonimo di trittico, persona diversa);
   - piu' di uno -> "scegli": decide l'utente (vede ruolo, sede, eta').
Il nome si confronta senza maiuscole, accenti, punteggiatura e spazi doppi, e
senza badare all'ordine delle parole ("Rossi Mario" = "Mario Rossi") ne' agli
spazi dentro il cognome ("D'Amico" = "Damico").
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

ESITO_NUOVO = "nuovo"
ESITO_AGGIORNA = "aggiorna"
ESITO_SCEGLI = "scegli"


def normalize_text(text: Any) -> str:
    """Minuscole, senza accenti, punteggiatura ridotta a spazio, spazi singoli."""
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    no_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^\w]+", " ", no_marks.casefold()).split())


def name_key(full_name: Any) -> tuple[str, ...]:
    """Chiave del nome indipendente dall'ordine delle parole."""
    return tuple(sorted(normalize_text(full_name).split()))


def same_person_name(a: Any, b: Any) -> bool:
    """Stesse parole in qualunque ordine, oppure stesse lettere senza spazi
    ("D'Amico" = "Damico", "De Luca" = "Deluca")."""
    key_a = name_key(a)
    if not key_a:
        return False
    if key_a == name_key(b):
        return True
    return normalize_text(a).replace(" ", "") == normalize_text(b).replace(" ", "")


def partial_person_name(a: Any, b: Any) -> bool:
    """Le parole di un nome sono tutte nell'altro, almeno due ("Maria Rossi" e
    "Maria Grazia Rossi"): forse la stessa persona con un secondo nome omesso."""
    words_a, words_b = set(name_key(a)), set(name_key(b))
    shorter, longer = sorted((words_a, words_b), key=len)
    return len(shorter) >= 2 and shorter < longer


def field(record: dict[str, Any], key: str) -> Any:
    """Valore di un campo MCFlash senza badare alle maiuscole della chiave."""
    if key in record:
        return record[key]
    lowered = key.lower()
    for k, v in record.items():
        if str(k).lower() == lowered:
            return v
    return None


def record_id(record: dict[str, Any]) -> str:
    return str(field(record, "id") or "").strip()


def record_trittico(record: dict[str, Any]) -> str:
    return str(field(record, "nome") or "").strip().upper()


def candidate_row(record: dict[str, Any], cv: dict[str, Any] | None, *, eligible: bool) -> dict[str, Any]:
    """Riga della dashboard: campi MCFlash con nomi espliciti + CV indicizzato."""
    return {
        "id": record_id(record),
        "trittico": record_trittico(record),
        "nome_completo": str(field(record, "nomi") or "").strip(),
        "ruolo": field(record, "ruolo"),
        "seniority": field(record, "seniority"),
        "sede": field(record, "sede"),
        "eta": field(record, "eta"),
        "lingue": field(record, "lingue"),
        "disponibilita": field(record, "disponibilita"),
        "budget": field(record, "budget"),
        "finto": bool(field(record, "finto")),
        "site": field(record, "site"),
        "abilitato": field(record, "abilitato"),
        "is_talent_up": field(record, "isTalentUp"),
        "idoneo": eligible,
        "cv": cv,
    }


def filter_rows(
    rows: Iterable[dict[str, Any]],
    *,
    query: str | None = None,
    sede: str | None = None,
    has_cv: bool | None = None,
) -> list[dict[str, Any]]:
    """Ricerca testuale (tutte le parole, in id/trittico/nome/ruolo/sede) e filtri."""
    words = normalize_text(query).split()
    sede_norm = normalize_text(sede)
    out = []
    for row in rows:
        if has_cv is not None and (row.get("cv") is not None) != has_cv:
            continue
        if sede_norm and sede_norm not in normalize_text(row.get("sede")):
            continue
        if words:
            haystack = normalize_text(" ".join(
                str(row.get(k) or "") for k in ("id", "trittico", "nome_completo", "ruolo", "sede")
            ))
            if not all(w in haystack for w in words):
                continue
        out.append(row)
    return out


def match_candidate(trittico: str, full_name: str, rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Esito del match per un CV caricato: {esito, motivo, candidati, stesso_trittico}.

    `candidati` sono i record con stesso trittico e stesso nome (1 per
    "aggiorna", piu' di 1 per "scegli"); `stesso_trittico` li elenca tutti, cosi'
    l'utente puo' cambiare l'esito proposto.

    Reti di sicurezza, quando per trittico non c'e' nessuno con quel nome: meglio
    far decidere l'utente ("scegli") che creare un doppione su MCFlash.
    - nome parziale con lo stesso trittico: secondo nome presente solo nel CV o
      solo su MCFlash ("Maria Rossi" / "Maria Grazia Rossi");
    - stesso nome con un altro trittico: su MCFlash alcuni trittici non seguono
      la regola (nomi di 3-4 parole scritti a mano).
    """
    rows = list(rows)
    tri = (trittico or "").strip().upper()
    same_code = [r for r in rows if tri and r.get("trittico") == tri]
    same_name = [r for r in same_code if same_person_name(r.get("nome_completo"), full_name)]
    if len(same_name) == 1:
        esito, motivo = ESITO_AGGIORNA, "stesso trittico e stesso nome"
    elif len(same_name) > 1:
        esito, motivo = ESITO_SCEGLI, "piu' record con stesso trittico e stesso nome"
    else:
        partial = [r for r in same_code if partial_person_name(r.get("nome_completo"), full_name)]
        other_code = [r for r in rows if r.get("trittico") != tri
                      and same_person_name(r.get("nome_completo"), full_name)]
        if partial:
            esito, motivo, same_name = ESITO_SCEGLI, "stesso trittico e nome simile (secondo nome?)", partial
        elif other_code:
            esito, motivo, same_name = ESITO_SCEGLI, "stesso nome ma trittico diverso su MCFlash", other_code
        elif same_code:
            esito, motivo = ESITO_NUOVO, "stesso trittico ma nome diverso (omonimo di codice)"
        else:
            esito, motivo = ESITO_NUOVO, "nessun record con questo trittico"
    return {"esito": esito, "motivo": motivo, "candidati": same_name, "stesso_trittico": same_code}

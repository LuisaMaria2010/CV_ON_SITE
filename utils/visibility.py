"""Chi e' l'utente che sta chiedendo, e cosa gli e' permesso vedere.

Serve a `/api/ai-matcher-wrapper` per dire all'agente, dentro CONTEXT_JSON, se
l'utente puo' vedere i consulenti senza profilo MCFlash (`id_mcflash` null,
cioe' presenti solo nell'indice CV). E' l'agente a decidere poi cosa farne:
qui si trasporta solo l'informazione.

Il default e' `external` (fail-closed): se lo scope non e' determinabile —
impostazione mancante, utente sconosciuto, campo assente — si dichiara il
permesso piu' ristretto.
"""
from __future__ import annotations

from typing import Any

from utils.app_settings import settings_value
from utils.values import first_non_empty, safe_str

SCOPE_INTERNAL = "internal"
SCOPE_EXTERNAL = "external"

_INTERNAL_ALIASES = {"internal", "interno", "interna", "mc", "mcflash", "staff"}
_EXTERNAL_ALIASES = {"external", "esterno", "esterna", "guest", "cliente", "client"}


def _setting_list(*keys: str) -> set[str]:
    raw = settings_value(*keys, default="")
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def _declared_scope(payload: dict[str, Any]) -> str | None:
    """Scope dichiarato esplicitamente nel payload, se riconoscibile.

    Ritorna None per valore vuoto o non riconosciuto, cosi' un refuso non vale
    come decisione e si passa alla regola successiva invece di fermarsi qui.
    """
    text = safe_str(
        first_non_empty(
            payload.get("user_scope"),
            payload.get("userScope"),
            payload.get("user_type"),
            payload.get("userType"),
        )
    ).lower()
    if text in _INTERNAL_ALIASES:
        return SCOPE_INTERNAL
    if text in _EXTERNAL_ALIASES:
        return SCOPE_EXTERNAL
    return None


def resolve_user_scope(payload: dict[str, Any] | None) -> str:
    """'internal' o 'external' per l'utente che ha originato la richiesta.

    Precedenza: campo esplicito `user_scope`/`user_type` -> `user_id` elencato
    in INTERNAL_USER_IDS -> dominio email in INTERNAL_EMAIL_DOMAINS -> external.

    NOTA: finche' /api/ai-matcher-wrapper e' una route anonima che legge
    `user_id` dal body, l'identita' e' dichiarata dal client e non autenticata.
    """
    payload = payload if isinstance(payload, dict) else {}

    declared = _declared_scope(payload)
    if declared:
        return declared

    user_id = safe_str(first_non_empty(payload.get("user_id"), payload.get("userId"))).lower()
    if not user_id:
        return SCOPE_EXTERNAL

    if user_id in _setting_list("INTERNAL_USER_IDS"):
        return SCOPE_INTERNAL

    domain = user_id.rsplit("@", 1)[-1] if "@" in user_id else ""
    if domain and domain in _setting_list("INTERNAL_EMAIL_DOMAINS"):
        return SCOPE_INTERNAL

    return SCOPE_EXTERNAL


def user_permissions(scope: str) -> dict[str, Any]:
    """Blocco permessi che finisce in CONTEXT_JSON, letto dall'agente."""
    return {"can_view_non_mcflash": scope == SCOPE_INTERNAL}

"""Visibilita' dei candidati in base al tipo di utente.

Regola unica: solo un utente `internal` vede i candidati senza profilo MCFlash
risolto (`id_mcflash` null). Per un `external` quei candidati non esistono.

Il tipo di utente arriva dal frontend nel campo `user_type` del body di
/api/ai-matcher-wrapper, insieme a user_id e chat_id. Non viene dedotto da
nient'altro: niente tabelle, niente domini email, niente impostazioni.

Il default e' `external` (fail-closed): campo assente, vuoto o non riconosciuto
significa visibilita' ristretta. Il modo in cui questo codice sbaglia e' "un
interno vede meno candidati del dovuto", mai il contrario.

NOTA SUL CONFINE DI FIDUCIA: /api/ai-matcher-wrapper e' una route anonima, quindi
`user_type` e' dichiarato dal client esattamente come `user_id`. Questo modulo
garantisce che l'AGENTE non possa causare una fuga (se omette il campo si filtra
comunque); non garantisce nulla contro un client che dichiara il falso. Per
quello serve un'identita' autenticata a monte.
"""
from __future__ import annotations

from typing import Any

from utils.values import first_non_empty, safe_str

USER_TYPE_INTERNAL = "internal"
USER_TYPE_EXTERNAL = "external"

# Sinonimi accettati, cosi' un frontend che manda "interno" non viene degradato
# a esterno per una differenza di lingua.
_INTERNAL_ALIASES = {"internal", "interno", "interna"}


def resolve_user_type(payload: dict[str, Any] | None) -> str:
    """'internal' o 'external' a partire dal body della richiesta.

    Qualsiasi cosa diversa da un valore internal riconosciuto -> 'external'.
    """
    payload = payload if isinstance(payload, dict) else {}
    declared = safe_str(
        first_non_empty(
            payload.get("user_type"),
            payload.get("userType"),
        )
    ).lower()
    return USER_TYPE_INTERNAL if declared in _INTERNAL_ALIASES else USER_TYPE_EXTERNAL


def can_view_non_mcflash(user_type: Any) -> bool:
    """Se questo tipo di utente puo' vedere i candidati senza id_mcflash."""
    return safe_str(user_type).lower() == USER_TYPE_INTERNAL

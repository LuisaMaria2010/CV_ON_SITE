"""
Request context helpers.

Gestisce la propagazione del request_id tramite contextvars,
sicuro per async e concorrenza.

Responsabilità:
- set/get request_id globale per request
- evitare passaggio manuale tra funzioni
"""

from contextvars import ContextVar, Token
from contextlib import contextmanager


# default esplicito
_DEFAULT_REQUEST_ID = ""


request_id_ctx: ContextVar[str] = ContextVar(
    "request_id",
    default=_DEFAULT_REQUEST_ID,
)


# =========================================================
# Basic API
# =========================================================

def set_request_id(value: str) -> Token:
    """
    Imposta il request_id corrente nel context locale (thread/async safe).

    Args:
        value (str): Identificativo della richiesta da propagare.

    Returns:
        Token: Token per eventuale reset (best practice contextvars).
    """
    return request_id_ctx.set(value)


def get_request_id() -> str:
    """
    Restituisce il request_id corrente dal context locale.

    Returns:
        str: Identificativo della richiesta corrente.
    """
    return request_id_ctx.get()


def reset_request_id(token: Token) -> None:
    """
    Ripristina il valore precedente di request_id tramite token.

    Args:
        token (Token): Token restituito da set_request_id.
    """
    request_id_ctx.reset(token)


# =========================================================
# Optional context manager (utile per test/script)
# =========================================================

@contextmanager
def request_context(request_id: str):
    """
    Context manager per impostare temporaneamente il request_id.

    Utile per test o script che richiedono propagazione esplicita.

    Args:
        request_id (str): Identificativo da impostare nel context.
    """
    token = set_request_id(request_id)
    try:
        yield
    finally:
        reset_request_id(token)

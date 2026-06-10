"""
Database infrastructure layer.

Responsabilità:
- creare e mantenere il pool MySQL
- ZERO logica business
- ZERO SQL di dominio
- solo connessioni

Il repository NON importa aiomysql.
Solo questo modulo lo fa.
"""

from __future__ import annotations
from contextlib import asynccontextmanager

import os
import logging
from typing import Any
from typing import Optional

import aiomysql


logger = logging.getLogger(__name__)

_pool: Optional[aiomysql.Pool] = None


def _parse_kv_connection_string(conn_str: str) -> dict[str, str]:
    """Parse simple key=value;key=value connection strings.

    Works with common .NET style strings like:
    Server=localhost;Port=3306;Database=db;User=user;Password=pwd;
    """
    parsed: dict[str, str] = {}
    for part in conn_str.split(";"):
        item = part.strip()
        if not item or "=" not in item:
            continue
        key, value = item.split("=", 1)
        parsed[key.strip().lower()] = value.strip()
    return parsed


def _connection_settings() -> dict[str, Any]:
    """Resolve DB settings from env vars and optional DefaultConnection.

    Precedence:
    1) MYSQL_* env vars (if set)
    2) Connection string env var (`DEFAULT_CONNECTION`, `DefaultConnection`, `MYSQL_CONNECTION_STRING`)
    3) Hard-coded defaults
    """
    conn_str = (
        os.getenv("DEFAULT_CONNECTION")
        or os.getenv("DefaultConnection")
        or os.getenv("MYSQL_CONNECTION_STRING")
        or ""
    )
    cs = _parse_kv_connection_string(conn_str) if conn_str else {}

    host = os.getenv("MYSQL_HOST") or cs.get("server") or cs.get("host") or "localhost"
    port_str = os.getenv("MYSQL_PORT") or cs.get("port") or "3306"
    user = os.getenv("MYSQL_USER") or cs.get("user") or cs.get("uid") or cs.get("user id") or "root"
    password = os.getenv("MYSQL_PASSWORD") or cs.get("password") or cs.get("pwd") or ""
    db = os.getenv("MYSQL_DB") or cs.get("database") or cs.get("initial catalog") or "app"

    try:
        port = int(port_str)
    except (TypeError, ValueError):
        port = 3306

    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "db": db,
    }


# =========================================================
# Pool factory (singleton lazy)
# =========================================================

async def get_pool() -> aiomysql.Pool:
    """
    Ritorna pool globale (lazy init).

    Vantaggi:
    - una sola connessione pool per processo
    - riusabile
    - efficiente
    """

    global _pool

    if _pool:
        return _pool

    settings = _connection_settings()
    logger.info(
        "Creating MySQL/MariaDB pool host=%s port=%s db=%s user=%s",
        settings["host"],
        settings["port"],
        settings["db"],
        settings["user"],
    )

    _pool = await aiomysql.create_pool(
        host=settings["host"],
        port=settings["port"],
        user=settings["user"],
        password=settings["password"],
        db=settings["db"],
        minsize=1,
        maxsize=10,
        autocommit=False,
    )

    return _pool


# =========================================================
# Helper opzionale
# =========================================================

@asynccontextmanager
async def acquire_conn():
    """
    Helper comodo:

    async with acquire_conn() as conn:
        ...

    così il worker è più pulito.
    """

    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn
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
from typing import Optional

import aiomysql


logger = logging.getLogger(__name__)

_pool: Optional[aiomysql.Pool] = None


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

    logger.info("Creating MySQL pool...")

    _pool = await aiomysql.create_pool(
        host=os.getenv("MYSQL_HOST", "localhost"),
        port=int(os.getenv("MYSQL_PORT", 3306)),
        user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        db=os.getenv("MYSQL_DB", "app"),
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
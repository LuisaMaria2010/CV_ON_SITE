"""
Hash utilities per idempotenza CV.

Responsabilità:
- generare hash deterministico del file
- usato come chiave cache storage

Scelte:
- SHA256
- input = bytes originali
"""

import hashlib


def sha256_bytes(data: bytes) -> str:
    """
    Calcola hash SHA256 di bytes.

    Raises
    ------
    ValueError
        Se data è vuoto o None.
    """
    if not data:
        raise ValueError("Cannot hash empty data")

    # memoryview evita copie inutili
    return hashlib.sha256(memoryview(data)).hexdigest()

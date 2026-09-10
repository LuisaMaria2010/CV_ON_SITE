"""Wiring delle dipendenze condivise fra i blueprint delle Function.

I singleton qui dentro sono creati una volta per istanza (cold start) ed erano
prima variabili di modulo in function_app.py. Vivono in un unico posto perche'
piu' blueprint li usano: duplicarli darebbe due cache invece di una.
"""
from __future__ import annotations

from db_data.pipeline import CVPipeline
from extraction.cache import TextCache
from infra.blob_storage import StorageService
from infra.mcflash_candidates import MCFlashCandidatesClient
from utils.app_settings import settings_value

# Cold start dependency wiring
storage = StorageService()
cache = TextCache(storage)
pipeline = CVPipeline(cache)

_mcflash_candidates_client: MCFlashCandidatesClient | None = None


def get_mcflash_candidates_client() -> MCFlashCandidatesClient:
    global _mcflash_candidates_client
    if _mcflash_candidates_client is not None:
        return _mcflash_candidates_client

    base_url = settings_value(
        "MCFLASH_CANDIDATI_URL",
        "MCFLASH_CANDIDATES_URL",
        default="https://mcflashcandidati.mcengineering.eu/api/Candidati",
    )
    api_key = settings_value(
        "MCFLASH_CANDIDATI_API_KEY",
        "MCFLASH_CANDIDATES_API_KEY",
        default="MCFlash-Candidati-2026!",
    )

    _mcflash_candidates_client = MCFlashCandidatesClient(
        base_url=base_url,
        api_key=api_key,
        timeout_seconds=20,
    )
    return _mcflash_candidates_client

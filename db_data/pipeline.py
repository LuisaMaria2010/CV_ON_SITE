"""
Pipeline di orchestrazione per l’estrazione e arricchimento dati da CV.

Flusso completo:
    bytes → hash → text cache → extract → llm cache → llm → mapper → enrich

Gestisce cache, chiamate LLM, mapping e arricchimento in modo trasparente.
"""

import logging

from extraction.hashing import sha256_bytes
from extraction.cache import TextCache

from core.llm_chain import CVExtractionChain
from core.errors import TextExtractionError
from services.document_processor import DocumentProcessor

from db_data.mapper import to_domain
from db_data.postprocess import enrich

from utils.observability import track_event
from utils.request_context import get_request_id

logger = logging.getLogger(__name__)


# Versione dell'estrazione del testo: entra nelle chiavi della cache, cosi' un
# cambio dell'estrattore (es. le tabelle dei DOCX, lette dalla v2) non riusa il
# testo vecchio ne' l'estrazione LLM fatta su quello.
TEXT_EXTRACTION_VERSION = "2"

PROFILE_DEFAULT = "default"
PROFILE_MCFLASH = "mcflash"
PROFILES = (PROFILE_DEFAULT, PROFILE_MCFLASH)


def is_raw_zip_text(text: str) -> bool:
    """Testo che in realta' e' un file ZIP (DOCX) letto come testo semplice."""
    return text.lstrip("﻿").startswith("PK")


class CVPipeline:
    """
    Orchestratore principale per il processing di CV.

    Gestisce:
    - caching testo e JSON
    - estrazione testo
    - chiamata LLM e parsing output
    - mapping su modello dominio
    - arricchimento finale
    """
    def __init__(
        self,
        cache: TextCache,
        chain: CVExtractionChain | None = None,
        document_processor: DocumentProcessor | None = None,
        mcflash_chain: CVExtractionChain | None = None,
    ):
        self.cache = cache
        self._chain = chain  # Private field for lazy init
        self._mcflash_chain = mcflash_chain
        self.document_processor = document_processor or DocumentProcessor()

    @property
    def chain(self):
        """Lazy initialization of CVExtractionChain to avoid early LLM connection."""
        if self._chain is None:
            self._chain = CVExtractionChain()
        return self._chain

    @property
    def mcflash_chain(self):
        """Catena del profilo mcflash (lazy, come quella base)."""
        if self._mcflash_chain is None:
            self._mcflash_chain = CVExtractionChain.for_mcflash()
        return self._mcflash_chain

    def _chain_for(self, profile: str) -> CVExtractionChain:
        if profile == PROFILE_MCFLASH:
            return self.mcflash_chain
        if profile == PROFILE_DEFAULT:
            return self.chain
        raise ValueError(f"Unknown extraction profile: {profile!r}")

    # =========================================================

    async def process(self, file_bytes: bytes, mime: str | None = None, profile: str = PROFILE_DEFAULT):
        """
        Esegue l’intero flusso di estrazione e arricchimento dati da un file CV.

        Args:
            file_bytes (bytes): Contenuto binario del file CV.
            mime (str | None): MIME type del file, opzionale.
            profile (str): "default" (schema base, usato anche dall'ingestione) o
                "mcflash" (in piu' esperienze complete, formazione, lingue, trittico).

        Returns:
            CVExtraction: Modello dominio arricchito pronto per API/DB.

        Raises:
            TextExtractionError: Se il testo estratto è vuoto.
        """
        request_id = get_request_id()
        chain = self._chain_for(profile)

        # -------------------------------------------------
        # HASH
        # -------------------------------------------------

        file_hash = sha256_bytes(file_bytes)

        # -------------------------------------------------
        # TEXT CACHE
        # -------------------------------------------------

        text_key = f"{file_hash}-t{TEXT_EXTRACTION_VERSION}"
        text = await self.cache.get(text_key)

        # Un DOCX arrivato senza MIME veniva letto come testo: in cache c'e' lo
        # ZIP grezzo e l'estrazione LLM inventata da quello. Si rifanno entrambi.
        stale = text is not None and is_raw_zip_text(text)
        if stale:
            logger.warning("Pipeline: cached text for %s is a raw ZIP, extracting again", file_hash[:12])
            text = None

        if text is None:
            track_event("pipeline_text_cache_miss", request_id=request_id)

            processing_result = self.document_processor.process(file_bytes, mime_type=mime)
            text = processing_result["extracted_text"]

            if not text:
                raise TextExtractionError("Empty extracted text")

            await self.cache.save(text_key, text)
        else:
            track_event("pipeline_text_cache_hit", request_id=request_id)

        # -------------------------------------------------
        # JSON CACHE (LLM)
        # -------------------------------------------------

        # La firma cambia con prompt e schema: i due profili non condividono cache.
        json_cache_key = f"{text_key}-{chain.cache_signature}"

        cached_json = None if stale else await self.cache.get_json(json_cache_key)

        if cached_json is not None:
            track_event("pipeline_llm_cache_hit", request_id=request_id)

            raw = chain.schema(**cached_json)

        else:
            track_event("pipeline_llm_cache_miss", request_id=request_id)

            raw = await chain.extract(text)

            await self.cache.save_json(json_cache_key, raw.model_dump())

        # -------------------------------------------------
        # DOMAIN MAPPING
        # -------------------------------------------------

        cv = to_domain(raw)

        # -------------------------------------------------
        # ENRICHMENT
        # -------------------------------------------------

        cv = enrich(cv)

        return cv

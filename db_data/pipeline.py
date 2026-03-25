"""
Pipeline di orchestrazione per l’estrazione e arricchimento dati da CV.

Flusso completo:
    bytes → hash → text cache → extract → llm cache → llm → mapper → enrich

Gestisce cache, chiamate LLM, mapping e arricchimento in modo trasparente.
"""

from extraction.hashing import sha256_bytes
from extraction.extract import extract_text
from extraction.cache import TextCache

from core.llm_chain import CVExtractionChain
from core.schema import LLMExtractionRaw
from core.errors import TextExtractionError

from db_data.mapper import to_domain
from db_data.postprocess import enrich

from utils.observability import track_event
from utils.request_context import get_request_id



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
    def __init__(self, cache: TextCache, chain: CVExtractionChain | None = None):
        self.cache = cache
        self._chain = chain  # Private field for lazy init
        
    @property
    def chain(self):
        """Lazy initialization of CVExtractionChain to avoid early LLM connection."""
        if self._chain is None:
            self._chain = CVExtractionChain()
        return self._chain

    # =========================================================

    async def process(self, file_bytes: bytes, mime: str | None = None):
        """
        Esegue l’intero flusso di estrazione e arricchimento dati da un file CV.

        Args:
            file_bytes (bytes): Contenuto binario del file CV.
            mime (str | None): MIME type del file, opzionale.

        Returns:
            CVExtraction: Modello dominio arricchito pronto per API/DB.

        Raises:
            TextExtractionError: Se il testo estratto è vuoto.
        """
        request_id = get_request_id()

        # -------------------------------------------------
        # HASH
        # -------------------------------------------------

        file_hash = sha256_bytes(file_bytes)

        # -------------------------------------------------
        # TEXT CACHE
        # -------------------------------------------------

        text = await self.cache.get(file_hash)

        if text is None:
            track_event("pipeline_text_cache_miss", request_id=request_id)

            text = extract_text(file_bytes, mime)

            if not text:
                raise TextExtractionError("Empty extracted text")

            await self.cache.save(file_hash, text)
        else:
            track_event("pipeline_text_cache_hit", request_id=request_id)

        # -------------------------------------------------
        # JSON CACHE (LLM)
        # -------------------------------------------------

        json_cache_key = f"{file_hash}-{self.chain.cache_signature}"

        cached_json = await self.cache.get_json(json_cache_key)

        if cached_json is not None:
            track_event("pipeline_llm_cache_hit", request_id=request_id)

            raw = LLMExtractionRaw(**cached_json)

        else:
            track_event("pipeline_llm_cache_miss", request_id=request_id)

            raw = await self.chain.extract(text)

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

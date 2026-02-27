"""
Catena di estrazione dati strutturati da testo CV tramite LLM (LangChain).

Questo modulo definisce la pipeline per:
- Caricare e parametrizzare il prompt
- Inizializzare la catena LLM+parser
- Gestire la chiamata asincrona e la validazione input/output
- Tracciare eventi e metriche per osservabilità
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from langchain.prompts import PromptTemplate
from langchain.output_parsers import PydanticOutputParser

from core.schema import LLMExtractionRaw
from core.errors import LLMProcessingError
from core.config import settings

from infra.llm_client import get_llm

from utils.observability import track_duration, track_event
from utils.request_context import get_request_id


logger = logging.getLogger(__name__)


# =========================================================
# Prompt loader
# =========================================================

def _load_prompt() -> str:
    prompt_path = Path(__file__).parent / settings.prompt_file
    with open(prompt_path, "r", encoding="utf-8") as f:
        return f.read()


# PROMPT_TEXT = _load_prompt()  # COMMENTED OUT - LAZY LOADING


# =========================================================
# Chain
# =========================================================


class CVExtractionChain:
    """
    Catena di estrazione dati strutturati da testo CV tramite LLM.

    Inizializza la pipeline composta da:
    - Prompt parametrico
    - LLM client (LangChain)
    - Output parser (Pydantic)

    Fornisce il metodo asincrono extract per invocare la catena e restituire un oggetto validato.
    """

    def __init__(self, llm=None):
        self.llm = llm or get_llm()

        self.parser = PydanticOutputParser(
            pydantic_object=LLMExtractionRaw
        )

        self.prompt = PromptTemplate(
            template=_load_prompt(),  # Lazy load prompt
            input_variables=["content"],
            partial_variables={
                "format_instructions": self.parser.get_format_instructions()
            },
        )

        self.chain = self.prompt | self.llm | self.parser

    # =====================================================

    async def extract(self, text: str) -> LLMExtractionRaw:
        """
        Estrae dati strutturati da testo CV tramite LLM asincrono.

        Args:
            text (str): Testo del CV da processare.

        Returns:
            LLMExtractionRaw: Oggetto strutturato estratto dal testo.

        Raises:
            LLMProcessingError: In caso di input vuoto, timeout o errori LLM.
        """

        if not text:
            raise LLMProcessingError("Empty input text")

        request_id = get_request_id()

        # truncate safety
        truncated = text[: settings.max_text_chars]

        if len(text) > len(truncated):
            track_event(
                "llm_input_truncated",
                request_id=request_id,
                properties={"original_chars": len(text)},
            )

        track_event(
            "llm_call_start",
            request_id=request_id,
            properties={"text_chars": len(truncated)},
        )

        formatted_prompt = self.prompt.format(
        content=truncated
    )

        logger.info("\n===== FINAL PROMPT SENT TO LLM =====\n")
        logger.info(formatted_prompt)
        logger.info("\n===================================\n")

        try:
            with track_duration("llm_processing_ms", request_id=request_id):

                result = await asyncio.wait_for(
                    self.chain.ainvoke({"content": truncated}),
                    timeout=settings.llm_timeout_seconds,
                )

            track_event("llm_call_success", request_id=request_id)

            return result

        except asyncio.TimeoutError as e:
            raise LLMProcessingError("LLM timeout") from e

        except Exception as e:
            logger.exception("LLM extraction failed")

            track_event(
                "llm_call_error",
                request_id=request_id,
                properties={"error": type(e).__name__},
            )

            raise LLMProcessingError(str(e)) from e

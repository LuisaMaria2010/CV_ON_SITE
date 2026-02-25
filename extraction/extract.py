"""
Estrazione testo da file PDF, DOCX e TXT per parsing CV.

Questo modulo fornisce funzioni per:
- rilevare il formato file
- estrarre testo da PDF, DOCX, TXT
- pulire e normalizzare il testo per LLM
"""
from __future__ import annotations

import io
import re
import logging

import fitz
from docx import Document

from core.config import settings
from core.errors import TextExtractionError


logger = logging.getLogger(__name__)


# =========================================================
# Public API
# =========================================================

def extract_text(file_bytes: bytes, mime_type: str | None = None) -> str:
    """
    Estrae testo da file PDF, DOCX o TXT in memoria.

    Args:
        file_bytes (bytes): Contenuto binario del file.
        mime_type (str | None): MIME type opzionale per hint.

    Returns:
        str: Testo estratto e normalizzato.

    Raises:
        TextExtractionError: In caso di errore nell'estrazione.
    """
    if not file_bytes:
        return ""

    try:
        if _is_pdf(mime_type, file_bytes):
            text = _from_pdf(file_bytes)

        elif _is_docx(mime_type):
            text = _from_docx(file_bytes)

        else:
            text = _from_txt(file_bytes)

        return _clean_text(text)

    except Exception as e:
        logger.exception("Text extraction failed")
        raise TextExtractionError(str(e)) from e


# =========================================================
# Format detection
# =========================================================

def _is_pdf(mime: str | None, data: bytes) -> bool:
    """
    Rileva se il file è un PDF tramite MIME o signature.
    """
    if mime and "pdf" in mime:
        return True
    return data.startswith(b"%PDF")


def _is_docx(mime: str | None) -> bool:
    """
    Rileva se il file è un DOCX tramite MIME type.
    """
    """
    DOCX detection solo via MIME (più sicuro).
    """
    if not mime:
        return False

    return "officedocument" in mime or "word" in mime


# =========================================================
# Extractors
# =========================================================

def _from_pdf(data: bytes) -> str:
    """
    Estrae testo da PDF usando PyMuPDF (fitz).
    """
    text_parts: list[str] = []

    with fitz.open(stream=data, filetype="pdf") as doc:
        for page in doc:
            t = page.get_text("text")
            if t:
                text_parts.append(t)

    return "\n".join(text_parts)


def _from_docx(data: bytes) -> str:
    """
    Estrae testo da file DOCX usando python-docx.
    """
    file_like = io.BytesIO(data)
    document = Document(file_like)

    return "\n".join(
        p.text for p in document.paragraphs if p.text
    )


def _from_txt(data: bytes) -> str:
    """
    Estrae testo da file TXT, gestendo UTF-8 e fallback latin-1.
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="ignore")


# =========================================================
# Cleanup (CRUCIALE per LLM)
# =========================================================

_whitespace_re = re.compile(r"[ \t]+")
_multiline_re = re.compile(r"\n{3,}")


def _clean_text(text: str) -> str:
    """
    Pulisce e normalizza il testo estratto:
    - rimuove null chars
    - normalizza spazi e newline
    - tronca a max_text_chars
    """
    if not text:
        return ""

    # rimuove null chars (comuni nei PDF)
    text = text.replace("\x00", "")

    # spazi multipli
    text = _whitespace_re.sub(" ", text)

    # newline eccessivi
    text = _multiline_re.sub("\n\n", text)

    text = text.strip()

    # limit token safety
    max_chars = settings.max_text_chars

    if len(text) > max_chars:
        logger.warning(
            "Text truncated to %s chars for token safety",
            max_chars,
        )
        text = text[:max_chars]

    return text

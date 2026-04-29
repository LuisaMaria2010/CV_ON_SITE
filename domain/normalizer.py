"""
Text normalization utilities for document content.
Handles unicode normalization, whitespace cleanup, and character filtering.
"""
import re
import unicodedata
import logging


class TextNormalizer:
    """Utility for document-id normalization and text cleanup."""

    @staticmethod
    def normalize_document_id(filename: str) -> str:
        """
        Normalizes the filename by removing extension and common version suffixes.
        """
        name = filename or ""
        name = re.sub(r"\.[^.]+$", "", name)
        pattern = r'([_\-\s]?(vers(ione)?|rev|v)[_\-\s]*\d+)$'
        name = re.sub(pattern, "", name, flags=re.IGNORECASE)
        return name.strip("_- .")

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    def normalize(self, text: str) -> str:
        """
        Normalize text by applying unicode normalization, whitespace cleanup,
        and character filtering.
        """
        text = unicodedata.normalize("NFKC", text or "")
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"\n\s*\n", "\n\n", text)
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
        text = text.strip()
        self.logger.debug("Normalized text length=%d", len(text))
        return text

    def normalize_markdown_heading(self, text: str) -> str:
        heading = self.normalize(text)
        heading = re.sub(r"[.,:;!?]+$", "", heading)
        return heading.strip()

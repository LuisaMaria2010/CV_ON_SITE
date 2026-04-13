from __future__ import annotations

from domain.document_elements import DocumentElement
from extraction.extract import extract_elements


class DocumentParser:
    """PDF-first structured parser built on top of extraction.extract."""

    def parse(self, file_bytes: bytes, mime_type: str | None = None) -> list[DocumentElement]:
        return extract_elements(file_bytes, mime_type=mime_type)

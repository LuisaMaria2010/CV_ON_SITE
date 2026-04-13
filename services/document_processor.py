from __future__ import annotations

import hashlib
import re

from domain.document_elements import DocumentElement
from services.document_parser import DocumentParser


class DocumentProcessor:
    """Render structured elements to deterministic markdown."""

    def __init__(self, parser: DocumentParser | None = None):
        self.parser = parser or DocumentParser()

    def process(self, file_bytes: bytes, mime_type: str | None = None) -> dict:
        elements = self.parser.parse(file_bytes, mime_type=mime_type)
        extracted_text = self.render_text(elements)
        markdown = self.render_markdown(elements)
        return {
            "elements": elements,
            "extracted_text": extracted_text,
            "markdown": markdown,
            "content_hash": hashlib.sha256(extracted_text.encode("utf-8")).hexdigest(),
        }

    def render_text(self, elements: list[DocumentElement]) -> str:
        blocks: list[str] = []
        for element in elements:
            if element.element_type in {"heading", "paragraph", "list_item"}:
                cleaned = self._clean_paragraph(element.text)
                if cleaned:
                    blocks.append(cleaned)
            elif element.element_type == "table":
                table_text = self._render_table_text(element.rows)
                if table_text:
                    blocks.append(table_text)

        return "\n\n".join(block for block in blocks if block).strip()

    def render_markdown(self, elements: list[DocumentElement]) -> str:
        blocks: list[str] = []
        for element in elements:
            if element.element_type == "heading":
                level = min(max(element.level or 1, 1), 6)
                blocks.append(f"{'#' * level} {self._clean_inline(element.text)}")
            elif element.element_type == "paragraph":
                blocks.append(self._clean_paragraph(element.text))
            elif element.element_type == "list_item":
                marker = "1." if element.list_ordered else "-"
                blocks.append(f"{marker} {self._clean_inline(element.text)}")
            elif element.element_type == "table":
                table_md = self._render_table(element.rows)
                if table_md:
                    blocks.append(table_md)

        return "\n\n".join(block for block in blocks if block).strip()

    def _render_table(self, rows: list[list[str]]) -> str:
        cleaned_rows = [
            [self._escape_table_cell(cell) for cell in row]
            for row in rows
            if any((cell or "").strip() for cell in row)
        ]
        if not cleaned_rows:
            return ""

        width = max(len(row) for row in cleaned_rows)
        normalized_rows = [row + [""] * (width - len(row)) for row in cleaned_rows]
        header = normalized_rows[0]
        separator = ["---"] * width
        body = normalized_rows[1:] or [[""] * width]

        lines = [self._row_to_md(header), self._row_to_md(separator)]
        lines.extend(self._row_to_md(row) for row in body)
        return "\n".join(lines)

    def _render_table_text(self, rows: list[list[str]]) -> str:
        cleaned_rows = [
            [self._clean_inline(cell) for cell in row]
            for row in rows
            if any((cell or "").strip() for cell in row)
        ]
        if not cleaned_rows:
            return ""
        return "\n".join(" | ".join(cell for cell in row if cell) for row in cleaned_rows)

    def _row_to_md(self, row: list[str]) -> str:
        return "| " + " | ".join(row) + " |"

    def _escape_table_cell(self, value: str) -> str:
        return self._clean_inline(value).replace("|", "\\|")

    def _clean_paragraph(self, text: str) -> str:
        text = text.replace("\r\n", "\n")
        text = re.sub(r"\n{3,}", "\n\n", text)
        return "\n".join(line.strip() for line in text.splitlines()).strip()

    def _clean_inline(self, text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").strip())

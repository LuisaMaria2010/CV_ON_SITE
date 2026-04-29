from __future__ import annotations

import fitz

from services.document_processor import DocumentProcessor


def _build_sample_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Profilo Professionale", fontsize=20)
    page.insert_text((72, 110), "Consulente IT con esperienza in Azure e automazione.", fontsize=11)
    page.insert_text((72, 140), "- Azure Functions", fontsize=11)
    page.insert_text((72, 158), "- Python", fontsize=11)
    return doc.tobytes()


def test_document_processor_extracts_text_from_pdf() -> None:
    pdf_bytes = _build_sample_pdf()
    processor = DocumentProcessor()

    result = processor.process(pdf_bytes, mime_type="application/pdf")
    text = result["extracted_text"]

    assert "Profilo Professionale" in text
    assert "Consulente IT" in text
    assert "Azure Functions" in text


def test_document_processor_produces_deterministic_markdown() -> None:
    pdf_bytes = _build_sample_pdf()
    processor = DocumentProcessor()

    first = processor.process(pdf_bytes, mime_type="application/pdf")
    second = processor.process(pdf_bytes, mime_type="application/pdf")

    # content_hash and extracted text must be identical across calls
    assert first["content_hash"] == second["content_hash"]
    assert first["extracted_text"] == second["extracted_text"]

    # Strip front matter (--- ... ---\n) before comparing body — processed_at differs per call
    def _body(md: str) -> str:
        if md.startswith("---\n"):
            end = md.find("\n---\n", 4)
            if end >= 0:
                return md[end + 5:]
        return md

    assert _body(first["markdown"]) == _body(second["markdown"])
    assert "# Profilo Professionale" in first["markdown"]
    assert "Profilo Professionale" in first["extracted_text"]
    assert "- Azure Functions" in first["markdown"]
    assert "- Python" in first["markdown"]

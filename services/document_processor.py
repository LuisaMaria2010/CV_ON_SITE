from __future__ import annotations

import hashlib
import re

from domain.document_elements import DocumentElement
from services.document_parser import DocumentParser
from domain.normalizer import TextNormalizer
from datetime import datetime, timezone
from typing import Any, Dict
from core.schema import NormalizedCVMetadata, CandidateInfo, LanguageEntry, ImageRef, WorkExperience
import logging
import os
from urllib import request, error

from azure.storage.blob import BlobServiceClient
from core.config import settings
from services.image_description_service import ImageDescriptionService


class DocumentProcessor:
    """Render structured elements to deterministic markdown."""

    def __init__(self, parser: DocumentParser | None = None):
        self.parser = parser or DocumentParser()
        self.normalizer = TextNormalizer()
        self.image_describer = ImageDescriptionService()

    def process(
        self,
        file_bytes: bytes,
        mime_type: str | None = None,
        filename: str | None = None,
        source_path: str | None = None,
        version: int | None = None,
    ) -> Dict[str, Any]:
        elements = self.parser.parse(file_bytes, mime_type=mime_type)

        # Infer language from sample text
        language = self._infer_document_language(elements)

        # Enrich images: upload to blob storage and keep descriptions if present
        document_id = TextNormalizer.normalize_document_id(filename or "")
        try:
            self._enrich_images(document_id, filename or "", elements, language)
        except Exception:
            logging.getLogger(__name__).exception("Image enrichment failed")

        # Sort elements for reading order and suppress OCR duplicates
        elements = self._sort_elements_for_reading_order(elements)
        elements = self._suppress_image_ocr_duplicate_paragraphs(elements)

        extracted_text = self.render_text(elements)
        content_hash = hashlib.sha256(extracted_text.encode("utf-8")).hexdigest()
        markdown_body = self.render_markdown(elements)

        # Build structured NormalizedCVMetadata using light heuristics
        doc_id = TextNormalizer.normalize_document_id(filename or "") if filename else ""
        srcs = [source_path] if source_path else []
        # candidate heuristics: first heading as name, find first email/phone in text
        first_heading = None
        for el in elements:
            if getattr(el, "element_type", None) == "heading" and getattr(el, "text", None):
                first_heading = el.text.strip()
                break

        # simple regexes
        email_match = re.search(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", extracted_text or "")
        phone_match = re.search(r"(\+?\d[\d\s().-]{6,}\d)", extracted_text or "")

        candidate = CandidateInfo(
            full_name=first_heading,
            email=email_match.group(0) if email_match else None,
            phone=phone_match.group(0) if phone_match else None,
        )

        image_count = sum(1 for e in (elements or []) if getattr(e, "element_type", None) == "image")

        metadata_obj = NormalizedCVMetadata(
            document_id=doc_id or TextNormalizer.normalize_document_id(filename or "doc"),
            source_paths=srcs,
            version=int(version or 1),
            hash=content_hash,
            processed_at=datetime.now(timezone.utc).isoformat(),
            language=language,
            candidate=candidate,
            skills=[],
            certifications=[],
            education_titles=[],
            languages_spoken=[],
            experience_years=None,
            employment_dates=[],
            images=[],
            element_count=len(elements or []),
            image_count=image_count,
            metadata={},
        )

        # Build YAML front matter lines from metadata object
        front_lines = self._build_yaml_front_matter(metadata_obj)
        final_markdown = "\n".join(front_lines) + "\n" + markdown_body

        return {
            "elements": elements,
            "extracted_text": extracted_text,
            "markdown": final_markdown,
            "content_hash": content_hash,
            "metadata": metadata_obj.model_dump(),
        }

    def apply_cv_extraction(
        self,
        markdown: str,
        base_metadata: dict,
        cv_extraction: Any,
    ) -> tuple[str, dict]:
        """
        Rebuild the YAML front matter in *markdown* using data from a CVExtraction result.

        No document re-parsing is performed.  Returns (enriched_markdown, enriched_metadata_dict).
        """
        base_candidate = base_metadata.get("candidate") or {}
        candidate = CandidateInfo(
            full_name=cv_extraction.full_name or base_candidate.get("full_name"),
            role=cv_extraction.role,
            location=cv_extraction.location,
            email=str(cv_extraction.email) if cv_extraction.email else base_candidate.get("email"),
            phone=cv_extraction.phone or base_candidate.get("phone"),
            birth_date=cv_extraction.birth_date.isoformat() if cv_extraction.birth_date else None,
            age=cv_extraction.age,
            seniority=cv_extraction.seniority,
        )

        lang = cv_extraction.language or base_metadata.get("language")
        languages_spoken = [LanguageEntry(lang=lang)] if lang else []

        # WorkExperience from CVExtraction maps 1-to-1 with NormalizedCVMetadata.employment_dates
        employment_dates = list(cv_extraction.employment_dates)

        base_images = base_metadata.get("images") or []
        images = []
        for img in base_images:
            if isinstance(img, dict):
                images.append(ImageRef(**img))
            elif isinstance(img, ImageRef):
                images.append(img)

        enriched = NormalizedCVMetadata(
            document_id=base_metadata.get("document_id", ""),
            source_paths=base_metadata.get("source_paths", []),
            version=base_metadata.get("version", 1),
            hash=base_metadata.get("hash", ""),
            processed_at=base_metadata.get("processed_at", datetime.now(timezone.utc).isoformat()),
            language=lang,
            candidate=candidate,
            skills=cv_extraction.skills,
            certifications=cv_extraction.certifications,
            education_titles=cv_extraction.education_titles,
            languages_spoken=languages_spoken,
            experience_years=cv_extraction.experience_years,
            employment_dates=employment_dates,
            images=images,
            element_count=base_metadata.get("element_count", 0),
            image_count=base_metadata.get("image_count", 0),
            metadata={},
        )

        front_lines = self._build_yaml_front_matter(enriched)
        front_matter = "\n".join(front_lines) + "\n"

        # Strip existing front matter block (--- ... ---) from start of markdown
        body = markdown
        if markdown.startswith("---\n"):
            end_idx = markdown.find("\n---\n", 4)
            if end_idx != -1:
                body = markdown[end_idx + 5:]  # skip past \n---\n

        return front_matter + body, enriched.model_dump()

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

    def _yaml_scalar(self, value: object) -> str:
        if value is None:
            return "''"
        if isinstance(value, bool):
            return "true" if value else "false"
        s = str(value)
        # simple quoting if spaces or colon present
        if any(ch.isspace() for ch in s) or ":" in s:
            # escape double quotes and wrap in double quotes (avoid f-string backslash issue)
            return '"' + s.replace('"', '\\"') + '"'
        return s

    def _build_yaml_front_matter(self, metadata: NormalizedCVMetadata | Dict[str, Any]) -> list[str]:
        # Accept either the Pydantic model or a raw dict
        if hasattr(metadata, "model_dump"):
            data = metadata.model_dump()
        else:
            data = dict(metadata or {})

        lines: list[str] = ["---"]

        def _emit(obj: Any, indent: int = 0):
            pad = "  " * indent
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if isinstance(v, (dict, list)):
                        lines.append(f"{pad}{k}:")
                        _emit(v, indent + 1)
                    else:
                        lines.append(f"{pad}{k}: {self._yaml_scalar(v)}")
            elif isinstance(obj, list):
                for item in obj:
                    if isinstance(item, dict):
                        keys = list(item.keys())
                        if keys:
                            # emit first key inline with the dash: "- key: value"
                            first_k = keys[0]
                            first_v = item[first_k]
                            if isinstance(first_v, (dict, list)):
                                lines.append(f"{pad}-")
                                _emit({first_k: first_v}, indent + 1)
                            else:
                                lines.append(f"{pad}- {first_k}: {self._yaml_scalar(first_v)}")
                            # remaining keys at indent+1
                            inner_pad = "  " * (indent + 1)
                            for k in keys[1:]:
                                v = item[k]
                                if isinstance(v, (dict, list)):
                                    lines.append(f"{inner_pad}{k}:")
                                    _emit(v, indent + 2)
                                else:
                                    lines.append(f"{inner_pad}{k}: {self._yaml_scalar(v)}")
                        else:
                            lines.append(f"{pad}-")
                    elif isinstance(item, list):
                        lines.append(f"{pad}-")
                        _emit(item, indent + 1)
                    else:
                        lines.append(f"{pad}- {self._yaml_scalar(item)}")
            else:
                lines.append(f"{pad}{self._yaml_scalar(obj)}")

        # emit keys in deterministic order: prioritize common fields
        ordered_keys = [
            "document_id",
            "source_paths",
            "version",
            "hash",
            "processed_at",
            "language",
            "candidate",
            "skills",
            "certifications",
            "education_titles",
            "languages_spoken",
            "experience_years",
            "employment_dates",
            "images",
            "element_count",
            "image_count",
            "metadata",
        ]

        for key in ordered_keys:
            if key in data:
                val = data.get(key)
                if isinstance(val, (dict, list)):
                    lines.append(f"{key}:")
                    _emit(val, 1)
                else:
                    lines.append(f"{key}: {self._yaml_scalar(val)}")

        # append any other keys not in ordered_keys
        for key in sorted(k for k in data.keys() if k not in ordered_keys):
            val = data.get(key)
            if isinstance(val, (dict, list)):
                lines.append(f"{key}:")
                _emit(val, 1)
            else:
                lines.append(f"{key}: {self._yaml_scalar(val)}")

        lines.append("---")
        lines.append("")
        return lines

    @staticmethod
    def _image_extension_from_content_type(content_type: str) -> str:
        mapping = {
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/jpg": "jpg",
            "image/gif": "gif",
            "image/bmp": "bmp",
            "image/tiff": "tiff",
            "image/webp": "webp",
            "image/x-emf": "emf",
            "image/emf": "emf",
            "application/x-emf": "emf",
            "image/x-wmf": "wmf",
            "image/wmf": "wmf",
            "application/x-wmf": "wmf",
        }
        return mapping.get((content_type or "").lower(), "png")

    def _infer_document_language(self, elements: list[DocumentElement]) -> str:
        text_chunks = []
        char_count = 0
        for item in elements:
            if item.element_type in {"heading", "paragraph", "list_item"} and item.text:
                text_chunks.append(item.text)
                char_count += len(item.text)
            if char_count >= 12000:
                break

        if not text_chunks:
            return "en"

        sample = " ".join(text_chunks).lower()
        tokens = re.findall(r"[a-zàèéìíîòóùúçñäöüß]+", sample)
        if not tokens:
            return "en"

        stopwords = {
            "it": {"il", "lo", "la", "i", "gli", "le", "un", "una", "uno", "di", "del", "della", "delle", "che", "per", "con", "nel", "nella", "nelle"},
            "en": {"the", "and", "to", "of", "in", "for", "with", "on", "from"},
        }

        scores = {code: 0 for code in stopwords}
        for token in tokens:
            for code, vocab in stopwords.items():
                if token in vocab:
                    scores[code] += 1

        best_lang = max(scores, key=scores.get)
        if scores[best_lang] == 0:
            return "en"
        return best_lang

    def _enrich_images(self, document_id: str, filename: str, elements: list[DocumentElement], language_code: str) -> None:
        image_elements = [item for item in elements if item.element_type == "image"]
        if not image_elements:
            return

        container = os.environ.get("EXTRACTED_IMAGES_CONTAINER") or "extracted-images"
        conn = settings.storage_account_connection_string or settings.storage_connection_string
        if not conn:
            # cannot upload images without connection string
            return
        client = BlobServiceClient.from_connection_string(conn)
        container_client = client.get_container_client(container)
        try:
            container_client.create_container()
        except Exception:
            pass

        for idx, element in enumerate(image_elements, start=1):
            image_bytes = getattr(element, "image_bytes", None)
            content_type = getattr(element, "image_content_type", None) or "image/png"

            # Try fetch external src if no bytes
            if not image_bytes and getattr(element, "image_src", None) and element.image_src.startswith(("http://", "https://")):
                try:
                    req = request.Request(element.image_src, headers={"User-Agent": "doc-ingest/1.0"})
                    with request.urlopen(req, timeout=15) as resp:
                        fetched = resp.read()
                        fetched_ct = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
                    if fetched:
                        image_bytes = fetched
                        element.image_bytes = fetched
                        if fetched_ct:
                            content_type = fetched_ct
                            element.image_content_type = fetched_ct
                except error.HTTPError:
                    pass
                except Exception:
                    pass

            if not image_bytes:
                continue

            ext = self._image_extension_from_content_type(content_type)
            image_blob_name = f"{document_id}/{idx:04d}.{ext}"
            try:
                blob_client = container_client.get_blob_client(image_blob_name)
                blob_client.upload_blob(image_bytes, overwrite=True, content_type=content_type)
                element.image_src = f"/{container}/{image_blob_name}"
            except Exception:
                continue

            # Describe image via image_describer (Foundry/OpenAI) if available and image seems analyzable
            try:
                describer = getattr(self, "image_describer", None)
                enabled = getattr(describer, "enabled", False)
            except Exception:
                describer = None
                enabled = False

            # Heuristics to avoid describing logos/very small images or vector icons
            skip_ct = {"image/svg+xml", "image/svg", "image/x-icon", "image/vnd.microsoft.icon"}
            src_lower = (getattr(element, "image_src", "") or "").lower()
            filename_lower = (filename or "").lower()
            looks_like_logo = "logo" in src_lower or "logo" in filename_lower
            min_bytes = int(os.environ.get("IMAGE_DESCRIPTION_MIN_BYTES", "2048"))

            should_describe = (
                enabled
                and image_bytes
                and len(image_bytes) >= min_bytes
                and (content_type or "").lower() not in skip_ct
                and not looks_like_logo
            )

            if should_describe:
                try:
                    desc = describer.describe(
                        image_bytes=image_bytes,
                        content_type=content_type,
                        context_hint=filename,
                        language_hint=language_code,
                    )
                    if desc.get("description"):
                        element.image_description = desc.get("description")
                    if desc.get("ocr_text"):
                        element.image_ocr_text = desc.get("ocr_text")
                    if not desc.get("description") and not desc.get("ocr_text"):
                        if (element.image_content_type or "").lower() == "image/jp2":
                            element.image_description = (
                                "Immagine estratta ma non analizzabile automaticamente (formato JP2)."
                            )
                        else:
                            element.image_description = "Immagine estratta ma non analizzabile automaticamente."
                except Exception:
                    # best effort; do not fail the whole pipeline on image description errors
                    try:
                        if (element.image_content_type or "").lower() == "image/jp2":
                            element.image_description = (
                                "Immagine estratta ma non analizzabile automaticamente (formato JP2)."
                            )
                        else:
                            element.image_description = "Immagine estratta ma non analizzabile automaticamente."
                    except Exception:
                        pass

            # Clear raw bytes (ensure no raw bytes remain in elements)
            try:
                element.image_bytes = None
            except Exception:
                pass

    def _suppress_image_ocr_duplicate_paragraphs(self, elements: list[DocumentElement]) -> list[DocumentElement]:
        ocr_parts = [re.sub(r"\s+", " ", (item.image_ocr_text or "")).strip().lower() for item in elements if item.element_type == "image" and (item.image_ocr_text or "").strip()]
        ocr_corpus = " ".join(part for part in ocr_parts if part)
        if not ocr_corpus:
            return elements

        padded = f" {ocr_corpus} "

        def _is_short_ocr_duplicate(text: str) -> bool:
            cleaned = re.sub(r"\s+", " ", (text or "")).strip().lower()
            if not cleaned:
                return False
            if len(cleaned) > 48:
                return False
            if len(cleaned.split()) > 4:
                return False
            return f" {cleaned} " in padded

        cleaned_elements: list[DocumentElement] = []
        index = 0
        while index < len(elements):
            current = elements[index]
            if current.element_type != "paragraph":
                cleaned_elements.append(current)
                index += 1
                continue

            run_end = index
            while run_end < len(elements):
                candidate = elements[run_end]
                if candidate.element_type != "paragraph":
                    break
                if not _is_short_ocr_duplicate(candidate.text):
                    break
                run_end += 1

            if run_end - index >= 3:
                index = run_end
                continue

            cleaned_elements.append(current)
            index += 1

        return cleaned_elements

    def _sort_elements_for_reading_order(self, elements: list[DocumentElement]) -> list[DocumentElement]:
        indexed = list(enumerate(elements))

        def _sort_key(item):
            index, element = item
            page_number = getattr(element, "page_number", None)
            vertical = getattr(element, "vertical_position", None)
            horizontal = getattr(element, "horizontal_position", None)

            if page_number is not None and vertical is not None:
                return (0, int(page_number), float(vertical), float(horizontal or 0.0), index)
            if page_number is not None:
                return (1, int(page_number), float(index), 0.0, index)
            return (2, 0, float(index), 0.0, index)

        ordered = sorted(indexed, key=_sort_key)
        return [element for _, element in ordered]

import os
import importlib.util
from typing import Dict, Any


class ImageDescriptionService:
    """Adapter: use AgentIngestionPipeline ImageDescriptionService if available.

    Falls back to a disabled stub if the external service is not importable.
    """

    def __init__(self):
        self.enabled = False
        self._impl = None

        # Attempt to locate the AgentIngestionPipeline image_description_service module
        candidate = os.path.join(
            os.path.expanduser("~"),
            "Documents",
            "Progetti_agenti",
            "AgentIngestionPipeline",
            "services",
            "image_description_service.py",
        )

        if os.path.exists(candidate):
            try:
                spec = importlib.util.spec_from_file_location("agent_image_desc", candidate)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)  # type: ignore
                # The AgentIngestionPipeline defines ImageDescriptionService
                impl = getattr(mod, "ImageDescriptionService", None)
                if impl:
                    self._impl = impl()
                    self.enabled = getattr(self._impl, "enabled", True)
            except Exception:
                self.enabled = False

    def describe(self, *, image_bytes: bytes, content_type: str | None = None, context_hint: str | None = None, language_hint: str | None = None, timeout_sec: int | None = None) -> Dict[str, Any]:
        """Return description dict {description, ocr_text} or empty dict when disabled."""
        if not self.enabled or not self._impl:
            return {}
        try:
            # Agent implementation uses describe(image_bytes=..., content_type=..., context_hint=..., language_hint=...)
            return self._impl.describe(image_bytes=image_bytes, content_type=content_type, context_hint=context_hint, language_hint=language_hint)
        except Exception:
            return {}

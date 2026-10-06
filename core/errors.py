"""
Errori di dominio FlashCV.

Questi errori:
- NON dipendono da HTTP
- NON dipendono da Azure
- servono a esprimere errori semantici chiari
"""


class CVError(Exception):
    """Errore base dominio FlashCV."""
    pass


# ===============================
# Input / Validation
# ===============================

class InvalidInputError(CVError):
    """Input non valido (file vuoto, formato non supportato, ecc.)."""
    pass


class FileTooLargeError(CVError):
    """File oltre i limiti consentiti."""
    pass


class NotFoundError(CVError):
    """La risorsa richiesta non esiste (es. candidato MCFlash)."""
    pass


class UpstreamServiceError(CVError):
    """Un servizio esterno (es. API MCFlash) non risponde o risponde male."""
    pass


# ===============================
# Extraction / Parsing
# ===============================

class TextExtractionError(CVError):
    """Errore durante l'estrazione del testo dal CV."""
    pass


# ===============================
# LLM
# ===============================

class LLMProcessingError(CVError):
    """Errore durante la chiamata al modello LLM."""
    pass


# ===============================
# Storage / Cache
# ===============================

class CacheError(CVError):
    """Errore accesso cache/storage."""
    pass

from __future__ import annotations

from pydantic import Field
import os
import json
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):

    # =====================================================
    # Pydantic config
    # =====================================================

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        # pydantic-settings env alias mapping for field names
        env_names={
            "azure_openai_key": ["AZURE_OPENAI_KEY"],
            "azure_search_api_key": ["AZURE_SEARCH_API_KEY"],
            "azure_subscription_id": ["AZURE_SUBSCRIPTION_ID"],
            "azure_tenant_id": ["AZURE_TENANT_ID"],
            "storage_account_connection_string": [
                "AzureWebJobsStorage",
                "STORAGE_ACCOUNT_CONNECTION_STRING",
                "STORAGE_CONNECTION_STRING",
            ],
            "storage_account_url": ["STORAGE_ACCOUNT_URL"],
            "search_endpoint": ["AZURE_SEARCH_SERVICE_ENDPOINT"],
        },
    )

    # =====================================================
    # Environment
    # =====================================================

    environment: str = "local"

    # =====================================================
    # Azure LLM (Azure OpenAI OR Azure AI Foundry)
    # =====================================================

    azure_openai_endpoint: str = Field(
        default="https://foundry-ai-mc-dev.cognitiveservices.azure.com/",
        description=(
            "Endpoint Azure LLM. "
            "Può essere Azure OpenAI (*.openai.azure.com) "
            "oppure Azure AI Foundry inference (*.ai.azure.com)."
        ),
    )

    # deployment name (Azure OpenAI) OR model name (Foundry)
    azure_openai_model: str = "gpt-4.1-mini"

    # embedding deployment/model name
    azure_openai_embedding_model: str = "text-embedding-3-large"
    # optional dedicated endpoint for embedding (Foundry may use services.ai.azure.com)
    azure_openai_embedding_endpoint: str | None = None

    # richiesta solo per Azure OpenAI
    azure_openai_api_version: str = "2025-01-01-preview"

    azure_openai_temperature: float = 0.0
    azure_openai_max_tokens: int = 1200
    llm_timeout_seconds: int = 120

    # opzionale override manuale tipo backend
    # auto = dedotto da endpoint
    # openai = forza Azure OpenAI
    # foundry = forza Foundry inference
    azure_llm_backend: str = "auto"

    # Optional explicit keys / identifiers (can be provided via env)
    azure_openai_key: str | None = Field(default=None)
    azure_search_api_key: str | None = Field(default=None)
    azure_subscription_id: str | None = Field(default=None)
    azure_tenant_id: str | None = Field(default=None)
    storage_account_connection_string: str | None = Field(default=None)

    # =====================================================
    # Storage
    # =====================================================

    storage_account_url: str = "https://devsaaimc.blob.core.windows.net"
    # allow override from AZURE storage env name
    storage_account_url: str = Field(default="https://devsaaimc.blob.core.windows.net")
    storage_container_incoming: str = "incoming-cv"
    storage_container_original_uploads: str = "incoming-cv-originals"
    storage_container_cache: str = "raw-text-cache"
    storage_container_normalized_markdown: str = "normalized-cv-md"
    storage_queue_name: str = "cv-persist"
    document_processing_queue_name: str = "document-processing"
    document_indexing_queue_name: str = "document-indexing"
    document_processing_dlq_name: str = "document-processing-deadletter"
    document_indexing_dlq_name: str = "document-indexing-deadletter"
    document_registry_connection_name: str = "AzureWebJobsStorage"
    document_registry_table_name: str = "DocumentRegistry"

    # =====================================================
    # Azure AI Search
    # =====================================================

    search_endpoint: str = Field(default="https://as-ai-sitemc-dev.search.windows.net")
    search_index_name: str = "cv-doc-chunks"
    document_search_index_name: str = "cv-doc-chunks"
    search_vector_dimensions: int = 1536
    # Chunking defaults for indexing
    azure_search_chunk_size: int = 2000
    azure_search_chunk_overlap: int = 200

    # Subco-specific indexes (Phase E)
    search_subco_risorse_index: str = "cv-doc-chunks"
    search_subco_candidati_index: str = "cv-doc-chunks"

    # Reranker weights (Phase E)
    search_reranker_lex_weight: float = 0.40
    search_reranker_vec_weight: float = 0.60
    search_reranker_skill_boost: float = 0.10
    search_reranker_role_boost: float = 0.05
    search_reranker_location_boost: float = 0.05
    search_reranker_recency_boost: float = 0.02
    search_fallback_threshold: float = 0.20

    # =====================================================
    # Limits
    # =====================================================

    max_file_size_mb: int = 10
    max_text_chars: int = 25_000

    # =====================================================
    # Prompt
    # =====================================================

    prompt_file: str = "prompts.txt"

    # =====================================================
    # MySQL (COMMENTED OUT - UNCOMMENT WHEN NEEDED)
    # =====================================================

    # mysql_host: str = "localhost"
    # mysql_port: int = 3306
    # mysql_user: str = "test"
    # mysql_password: str = "test"
    # mysql_database: str = "test"

    # =====================================================
    # Helpers
    # =====================================================

    @property
    def is_azure_openai(self) -> bool:
        """
        Determina automaticamente il backend.
        """
        if self.azure_llm_backend.lower() == "openai":
            return True
        if self.azure_llm_backend.lower() == "foundry":
            return False

        return "openai.azure.com" in self.azure_openai_endpoint.lower()

    @property
    def normalized_endpoint(self) -> str:
        """
        Normalizza endpoint Foundry per compatibilità OpenAI-style.
        """
        endpoint = self.azure_openai_endpoint.rstrip("/")

        if not self.is_azure_openai:
            if not endpoint.endswith("/v1"):
                endpoint = endpoint + "/v1"

        return endpoint

    @property
    def storage_connection_string(self) -> str | None:
        """
        Restituisce la connection string dello storage.

        Ordine di priorità:
        1. valore esplicito `storage_account_connection_string` (env `AzureWebJobsStorage`)
        2. variabile d'ambiente `AzureWebJobsStorage` letta direttamente
        3. None
        """
        if self.storage_account_connection_string:
            return self.storage_account_connection_string

        # fallback diretto alle env (in alcuni contesti local.settings.json non è caricata automaticamente)
        for k in ("AzureWebJobsStorage", "STORAGE_ACCOUNT_CONNECTION_STRING", "STORAGE_CONNECTION_STRING"):
            v = os.environ.get(k)
            if v:
                return v

        # fallback: try to read local.settings.json (useful for local dev runs)
        try:
            cfg_path = Path(__file__).parent.parent / "local.settings.json"
            if cfg_path.exists():
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
                vals = data.get("Values", {}) or {}
                v = vals.get("AzureWebJobsStorage") or vals.get("AZUREWEBJOBSSTORAGE")
                if v:
                    return v
        except Exception:
            pass

        return None

    # =====================================================
    # VALIDAZIONE SOLO IN PROD (COMMENTED OUT)
    # =====================================================

    # @model_validator(mode="after")
    # def validate_required_in_prod(self):

    #     if self.environment.lower() in {"prod", "production"}:

    #         if "test.openai.azure.com" in self.azure_openai_endpoint:
    #             raise ValueError("azure_openai_endpoint must be set in production")

    #         if self.storage_account_url.startswith("https://test"):
    #             raise ValueError("storage_account_url must be set in production")

    #         if not self.azure_openai_model:
    #             raise ValueError("azure_openai_model must be set in production")

    #     return self


# singleton
settings = Settings()

# Ensure `storage_account_connection_string` field is populated when possible
if not settings.storage_account_connection_string:
    _fallback = settings.storage_connection_string
    if _fallback:
        try:
            settings.storage_account_connection_string = _fallback
        except Exception:
            # best-effort: non-blocking if Settings is immutable in some contexts
            pass


# --------------------------------------------------
# Env alias mapping (compatibilità con precedenti nomi env)
# --------------------------------------------------
def _apply_env_aliases(s: Settings) -> None:
    """
    Populate settings fields from common environment variable aliases
    to preserve backward compatibility with existing local.settings.json
    and legacy env names (e.g. AzureWebJobsStorage).
    """
    aliases = {
        "azure_openai_key": ["AZURE_OPENAI_KEY"],
        "azure_search_api_key": ["AZURE_SEARCH_API_KEY"],
        "azure_subscription_id": ["AZURE_SUBSCRIPTION_ID"],
        "azure_tenant_id": ["AZURE_TENANT_ID"],
        "storage_account_connection_string": [
            "AzureWebJobsStorage",
            "STORAGE_ACCOUNT_CONNECTION_STRING",
            "STORAGE_CONNECTION_STRING",
        ],
        "storage_account_url": ["STORAGE_ACCOUNT_URL"],
        "search_endpoint": ["AZURE_SEARCH_SERVICE_ENDPOINT"],
    }

    for field, env_names in aliases.items():
        try:
            current = getattr(s, field)
        except Exception:
            current = None

        if current:
            continue

        for name in env_names:
            v = os.environ.get(name)
            if v:
                try:
                    setattr(s, field, v)
                except Exception:
                    # ignore if Settings is frozen/immutable in some contexts
                    pass
                break


# Apply aliases after instantiation
_apply_env_aliases(settings)

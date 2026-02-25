from __future__ import annotations

from pydantic import Field, model_validator
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
    )

    # =====================================================
    # Environment
    # =====================================================

    environment: str = "local"

    # =====================================================
    # Azure LLM (Azure OpenAI OR Azure AI Foundry)
    # =====================================================

    azure_openai_endpoint: str = Field(
        default="https://dev-foundry-ai-mc.openai.azure.com/",
        description=(
            "Endpoint Azure LLM. "
            "Può essere Azure OpenAI (*.openai.azure.com) "
            "oppure Azure AI Foundry inference (*.ai.azure.com)."
        ),
    )

    # deployment name (Azure OpenAI) OR model name (Foundry)
    azure_openai_model: str = "flashcv-gpt"

    # richiesta solo per Azure OpenAI
    azure_openai_api_version: str = "2024-02-15-preview"

    azure_openai_temperature: float = 0.0
    azure_openai_max_tokens: int = 1200
    llm_timeout_seconds: int = 60

    # opzionale override manuale tipo backend
    # auto = dedotto da endpoint
    # openai = forza Azure OpenAI
    # foundry = forza Foundry inference
    azure_llm_backend: str = "auto"

    # =====================================================
    # Storage
    # =====================================================

    storage_account_url: str = "https://devsaaimc.blob.core.windows.net"
    storage_container_incoming: str = "incoming-cv"
    storage_container_cache: str = "raw-text-cache"
    storage_queue_name: str = "cv-persist"

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
    # MySQL
    # =====================================================

    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "test"
    mysql_password: str = "test"
    mysql_database: str = "test"

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

    # =====================================================
    # VALIDAZIONE SOLO IN PROD
    # =====================================================

    @model_validator(mode="after")
    def validate_required_in_prod(self):

        if self.environment.lower() in {"prod", "production"}:

            if "test.openai.azure.com" in self.azure_openai_endpoint:
                raise ValueError("azure_openai_endpoint must be set in production")

            if self.storage_account_url.startswith("https://test"):
                raise ValueError("storage_account_url must be set in production")

            if not self.azure_openai_model:
                raise ValueError("azure_openai_model must be set in production")

        return self


# singleton
settings = Settings()

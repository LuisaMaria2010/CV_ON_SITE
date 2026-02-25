"""
Factory per client LLM Azure universale.

Supporta:
- Azure OpenAI (resource *.openai.azure.com)
- Azure AI Foundry inference / model catalog (*.ai.azure.com)

Responsabilità:
- configurare e restituire client LangChain pronto
- centralizzare autenticazione Azure AD
- selezionare automaticamente il tipo di endpoint

Nota: nessuna logica CV qui, solo configurazione LLM.
"""

from azure.identity import DefaultAzureCredential
from langchain_openai import AzureChatOpenAI, ChatOpenAI
from core.config import settings

_credential = DefaultAzureCredential()


def _token_provider() -> str:
    """
    Restituisce token Azure AD valido.
    """
    token = _credential.get_token(
        "https://cognitiveservices.azure.com/.default"
    )
    return token.token


def _is_azure_openai(endpoint: str) -> bool:
    """
    Determina se endpoint è Azure OpenAI classico.
    """
    if not endpoint:
        return False
    endpoint = endpoint.lower()
    return (
        "openai.azure.com" in endpoint
        or "cognitiveservices.azure.com" in endpoint
    )


def _normalize_base_url(endpoint: str) -> str:
    """
    Assicura che endpoint Foundry abbia /v1 finale.
    """
    endpoint = endpoint.rstrip("/")
    if not endpoint.endswith("/v1"):
        endpoint = endpoint + "/v1"
    return endpoint


def get_llm(
    temperature: float | None = None,
    max_tokens: int | None = None,
):
    """
    Restituisce client LLM configurato automaticamente.

    Se endpoint è Azure OpenAI -> AzureChatOpenAI
    Altrimenti -> ChatOpenAI compatibile con Azure AI Foundry.

    Args:
        temperature: override opzionale
        max_tokens: override opzionale

    Returns:
        AzureChatOpenAI | ChatOpenAI
    """

    endpoint = settings.azure_openai_endpoint

    temperature = (
        temperature
        if temperature is not None
        else settings.azure_openai_temperature
    )

    max_tokens = (
        max_tokens
        if max_tokens is not None
        else settings.azure_openai_max_tokens
    )

    # -------------------------------------------------
    # CASO 1 — AZURE OPENAI CLASSICO
    # -------------------------------------------------
    if _is_azure_openai(endpoint):

        return AzureChatOpenAI(
            azure_endpoint=endpoint,
            azure_deployment=settings.azure_openai_model,
            api_version=settings.azure_openai_api_version,
            azure_ad_token_provider=_token_provider,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=settings.llm_timeout_seconds,
        )

    # -------------------------------------------------
    # CASO 2 — AZURE AI FOUNDRY INFERENCE / SERVERLESS
    # -------------------------------------------------
    else:

        return ChatOpenAI(
            base_url=_normalize_base_url(endpoint),
            model=settings.azure_openai_model,
            api_key=_token_provider(),
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=settings.llm_timeout_seconds,
        )

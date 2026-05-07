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

from langchain_openai import AzureChatOpenAI, ChatOpenAI
from core.config import settings

_credential = None  # lazy-init: usato solo se manca AZURE_OPENAI_KEY


def _token_provider() -> str:
    """
    Restituisce token Azure AD valido (fallback se non c'è API key).
    """
    global _credential
    if _credential is None:
        from azure.identity import DefaultAzureCredential
        _credential = DefaultAzureCredential()
    token = _credential.get_token(
        "https://cognitiveservices.azure.com/.default"
    )
    return token.token


def _auth_kwargs() -> dict:
    """
    Restituisce kwargs di autenticazione per i client OpenAI.
    Preferisce API key statica; usa token AD solo come fallback.
    """
    key = settings.azure_openai_key
    if key:
        return {"api_key": key}
    return {"azure_ad_token_provider": _token_provider}


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
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=settings.llm_timeout_seconds,
            **_auth_kwargs(),
        )

    # -------------------------------------------------
    # CASO 2 — AZURE AI FOUNDRY INFERENCE / SERVERLESS
    # -------------------------------------------------
    else:

        auth = _auth_kwargs()
        # ChatOpenAI non supporta azure_ad_token_provider: usa il token direttamente
        if "azure_ad_token_provider" in auth:
            auth = {"api_key": auth["azure_ad_token_provider"]()}
        return ChatOpenAI(
            base_url=_normalize_base_url(endpoint),
            model=settings.azure_openai_model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=settings.llm_timeout_seconds,
            **auth,
        )


def get_embedding_client():
    """
    Restituisce client AzureOpenAIEmbeddings usando le stesse credenziali del LLM.

    Returns:
        AzureOpenAIEmbeddings pronto all'uso per generare vettori 1536-dim.
    """
    from langchain_openai import AzureOpenAIEmbeddings

    # Use dedicated embedding endpoint if configured (Foundry may expose a different URL)
    embed_endpoint = getattr(settings, "azure_openai_embedding_endpoint", None) or settings.azure_openai_endpoint
    return AzureOpenAIEmbeddings(
        azure_endpoint=embed_endpoint,
        azure_deployment=settings.azure_openai_embedding_model,
        api_version=settings.azure_openai_api_version,
        dimensions=settings.search_vector_dimensions,
        **_auth_kwargs(),
    )

"""
core/model.py — Provider-agnostic LLM factory.

Switch providers by setting env vars:
    LLM_PROVIDER=groq | gemini | openai
    LLM_MODEL=<model-name>   (optional — falls back to provider default)

No code changes needed anywhere else to swap models.
"""

import os
from dotenv import load_dotenv

load_dotenv()


def get_config(key: str, default: str | None = None) -> str | None:
    """Read a config value from Streamlit secrets (if available) or environment variable."""
    try:
        import streamlit as st
        val = st.secrets.get(key)
        if val is not None:
            return str(val)
    except Exception:
        pass
    return os.getenv(key, default)


# ---------------------------------------------------------------------------
# Provider config
# ---------------------------------------------------------------------------
LLM_PROVIDER: str = get_config("LLM_PROVIDER", "groq")
LLM_MODEL: str | None = get_config("LLM_MODEL")  # None → use provider default

_PROVIDER_DEFAULTS: dict[str, str] = {
    "groq": "qwen/qwen3-32b",
    "gemini": "gemini-2.0-flash",
    "openai": "gpt-4o-mini",
}


# ---------------------------------------------------------------------------
# Public factory functions
# ---------------------------------------------------------------------------

def get_llm(temperature: float = 0.0):
    """
    Return a LangChain chat model for the configured provider.

    Usage:
        llm = get_llm(temperature=0.0)
        structured = llm.with_structured_output(MySchema)
    """
    model_name = LLM_MODEL or _PROVIDER_DEFAULTS.get(LLM_PROVIDER)

    if LLM_PROVIDER == "groq":
        from langchain_groq import ChatGroq
        return ChatGroq(
            api_key=get_config("GROQ_API_KEY"),
            model=model_name,
            temperature=temperature,
        )

    elif LLM_PROVIDER == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            model=model_name,
            google_api_key=get_config("GOOGLE_API_KEY"),
            temperature=temperature,
        )

    elif LLM_PROVIDER == "openai":
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:
            raise ImportError(
                "langchain-openai is not installed. "
                "Run: pip install langchain-openai"
            ) from exc
        return ChatOpenAI(
            api_key=get_config("OPENAI_API_KEY"),
            model=model_name,
            temperature=temperature,
        )

    else:
        raise ValueError(
            f"Unknown LLM_PROVIDER='{LLM_PROVIDER}'. "
            "Set LLM_PROVIDER to one of: groq, gemini, openai"
        )


def get_embedding_model():
    """
    Return the embedding model.
    Currently fixed to Cohere — swap provider here without touching other files.
    """
    from langchain_cohere import CohereEmbeddings
    return CohereEmbeddings(
        model="embed-english-v3.0",
        cohere_api_key=get_config("COHERE_API_KEY"),
        user_agent="langchain",
    )


# ---------------------------------------------------------------------------
# Pre-built structured-output model instances
# Imported by pipeline nodes — temperatures differ per role.
# ---------------------------------------------------------------------------
from core.state import (  # noqa: E402
    ExtractedClauses,
    query as QuerySchema,
    ClauseComplianceResult,
    MicroBatchComplianceResult,
)

extract_clause_llm = get_llm(temperature=0.0).with_structured_output(ExtractedClauses)
query_generator_llm = get_llm(temperature=0.3).with_structured_output(QuerySchema)
compliance_model = get_llm(temperature=0.0).with_structured_output(ClauseComplianceResult)
micro_batch_compliance_model = get_llm(temperature=0.0).with_structured_output(MicroBatchComplianceResult)

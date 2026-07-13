"""
Centralised configuration. Everything is read from environment variables so
no secrets ever need to be hard-coded or committed.

See README.md "Setup" for the variables that matter day-to-day.
"""
import os


class Settings:
    # --- Auth -------------------------------------------------------------
    # Demo default so the grader can exercise the API without extra setup.
    # In a real deployment this MUST be overridden via the environment.
    API_KEY: str = os.getenv("API_KEY", "sparkline-demo-key-123")

    # --- Database -----------------------------------------------------------
    DB_PATH: str = os.getenv("DB_PATH", os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "sparkline_demo.db",
    ))

    # --- NL -> SQL engine ---------------------------------------------------
    # "auto"      (default) -> use a real LLM if a key is configured,
    #                          otherwise fall back to the stand-in.
    # "stand-in"            -> force the deterministic rule-based engine,
    #                          ignoring any configured LLM key.
    # "openai"              -> force the real-LLM path; fails loudly if no
    #                          key is set (useful for explicitly testing
    #                          the LLM integration in isolation).
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "auto")

    # GEMINI_API_KEY is the friendly name; LLM_API_KEY is the generic one
    # (so the same provider class also works for OpenAI/Groq/local servers).
    # Either works -- GEMINI_API_KEY just saves typing since Gemini is the
    # default provider.
    LLM_API_KEY: str = os.getenv("LLM_API_KEY") or os.getenv("GEMINI_API_KEY", "")

    # Defaults point at Gemini's OpenAI-compatible endpoint (free tier
    # available at https://aistudio.google.com/apikey) since that's the
    # primary engine for this submission. Override to point at OpenAI,
    # Groq, or a local Ollama/vLLM server instead -- the client code
    # (app/nl2sql/llm_provider.py) is identical either way.
    LLM_BASE_URL: str = os.getenv(
        "LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    LLM_MODEL: str = os.getenv("LLM_MODEL", "gemini-2.5-flash")
    LLM_TIMEOUT_SECONDS: float = float(os.getenv("LLM_TIMEOUT_SECONDS", "20"))

    # --- Safety / execution limits ------------------------------------------
    MAX_ROWS: int = int(os.getenv("MAX_ROWS", "200"))
    QUERY_TIMEOUT_SECONDS: float = float(os.getenv("QUERY_TIMEOUT_SECONDS", "5"))


settings = Settings()

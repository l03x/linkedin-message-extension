from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Values from .env.example that mean "not configured yet".
PLACEHOLDER_TOKENS = {"", "change-me", "changeme"}


class Settings(BaseSettings):
    """Process configuration, read from the environment (docker compose passes `.env`)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_token: str = ""
    database_url: str = "sqlite:////data/app.db"
    timezone: str = "UTC"

    llm_base_url: str = "http://host.docker.internal:8000/v1"
    llm_api_key: str = ""
    llm_model: str = "sonnet"
    # Non-standard field understood by claude-headless-api. Empty = not sent.
    llm_effort: str = ""
    llm_timeout_seconds: float = 180.0

    # Tesseract languages for OCR of scanned CVs ("+"-separated). eng, fra and ara are installed in the image.
    ocr_langs: str = "eng+fra"

    # Set to true when the dashboard is served over HTTPS.
    cookie_secure: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()


def check_settings(settings: Settings) -> None:
    if settings.app_token.strip() in PLACEHOLDER_TOKENS:
        raise RuntimeError(
            "APP_TOKEN is not set. Put a long random value in .env, e.g. the output of:\n"
            "  python3 -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )

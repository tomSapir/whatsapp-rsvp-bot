"""Application settings, loaded from the environment (and `.env`) via pydantic-settings.

Field names map case-insensitively to the env var names documented in `.env.example`
(e.g. ``phone_number_id`` ← ``PHONE_NUMBER_ID``). Secrets have no defaults, so the app
fails fast at startup if they are missing; operational knobs carry sensible defaults.

Use :func:`get_settings` rather than instantiating ``Settings`` directly — it caches a
single instance and keeps import side-effect-free (importing this module never reads the
environment), which matters for the offline tests in M1–M7.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- WhatsApp Business Cloud API (Meta) ---
    whatsapp_access_token: str
    phone_number_id: str
    whatsapp_app_secret: str
    webhook_verify_token: str
    graph_api_version: str = "v21.0"

    # --- OpenAI ---
    openai_api_key: str
    openai_model: str = "gpt-4o-mini"

    # --- Database ---
    db_path: str = "data/app.sqlite3"

    # --- Reminders ---
    # Re-send the invite to silent Invitations after N days, up to `reminder_max_count`
    # times, never past the event date (the event-date cutoff lives in the reminder job).
    reminder_delay_days: int = Field(default=3, ge=1)
    reminder_max_count: int = Field(default=2, ge=0)

    @property
    def database_url(self) -> str:
        """SQLAlchemy URL for the SQLite database (consumed by ``app/db.py`` in M1)."""
        return f"sqlite:///{self.db_path}"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, constructed once on first call."""
    return Settings()

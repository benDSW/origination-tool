"""Runtime configuration, loaded from environment variables (or a local ``.env``).

Secrets are ``SecretStr`` so they never appear in reprs, logs or tracebacks.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8-sig", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"

    # External APIs
    evertrace_api_key: SecretStr = SecretStr("")
    rocketreach_api_key: SecretStr = SecretStr("")
    affinity_api_key: SecretStr = SecretStr("")

    # Database
    database_url: str = "sqlite:///./origination.db"

    # Dashboard auth
    admin_password_hash: SecretStr = SecretStr("")
    session_secret: SecretStr = SecretStr("")
    session_max_age_seconds: int = 12 * 60 * 60

    # Behaviour
    rocketreach_daily_cap: int = Field(default=40, ge=0, le=500)
    affinity_origination_list_id: int = 123879
    affinity_applicant_list_id: int = 310945
    dsw_email_domains: Annotated[tuple[str, ...], NoDecode] = ("dsw.vc",)
    follow_up_after_days: int = Field(default=14, ge=1, le=90)
    run_lock_ttl_seconds: int = 60 * 60

    @field_validator("dsw_email_domains", mode="before")
    @classmethod
    def _split_domains(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(d.strip().lower() for d in value.split(",") if d.strip())
        return value

    @field_validator("database_url")
    @classmethod
    def _normalise_db_url(cls, value: str) -> str:
        # Railway/Heroku style URLs -> SQLAlchemy psycopg3 driver.
        if value.startswith("postgres://"):
            value = "postgresql://" + value[len("postgres://") :]
        if value.startswith("postgresql://"):
            value = "postgresql+psycopg://" + value[len("postgresql://") :]
        return value

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    def secret_values(self) -> list[str]:
        """Every configured secret, for the log-redaction filter."""
        secrets = [
            self.evertrace_api_key,
            self.rocketreach_api_key,
            self.affinity_api_key,
            self.admin_password_hash,
            self.session_secret,
        ]
        values = [s.get_secret_value() for s in secrets]
        return [v for v in values if len(v) >= 6]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

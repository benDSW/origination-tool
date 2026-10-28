"""Runtime configuration, loaded from environment variables (or a local ``.env``).

Secrets are ``SecretStr`` so they never appear in reprs, logs or tracebacks.

The defaults suit local development (``APP_ENV=development``, a SQLite file). They
must never silently apply on the hosting platform, so the entry points call
:func:`check_runtime_safety`, which refuses to start a misconfigured deployment.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
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
    # Keys the HMAC of erased founders' identifiers (both services). Never rotate it,
    # or forgotten founders could be re-ingested.
    suppression_secret: SecretStr = SecretStr("")
    session_max_age_seconds: int = 12 * 60 * 60
    # Proxies that append to X-Forwarded-For in front of the web service (Railway: 1).
    trusted_proxy_hops: int = Field(default=1, ge=0, le=5)

    # Behaviour
    rocketreach_daily_cap: int = Field(default=40, ge=0, le=500)
    affinity_origination_list_id: int = 123879
    affinity_applicant_list_id: int = 310945
    dsw_email_domains: Annotated[tuple[str, ...], NoDecode] = ("dsw.vc",)
    follow_up_after_days: int = Field(default=14, ge=1, le=90)
    # The dashboard checks Affinity for replies this often while it runs (0 = off).
    tracking_interval_minutes: int = Field(default=60, ge=0, le=24 * 60)
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
            self.suppression_secret,
        ]
        values = [s.get_secret_value() for s in secrets]
        return [v for v in values if len(v) >= 6]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


# --------------------------------------------------------------------------- #
# Fail-closed start-up checks
# --------------------------------------------------------------------------- #

# Variables Railway sets on every service; any one of them means "deployed".
PLATFORM_MARKERS = (
    "RAILWAY_ENVIRONMENT",
    "RAILWAY_ENVIRONMENT_NAME",
    "RAILWAY_PROJECT_ID",
    "RAILWAY_SERVICE_ID",
)
MIN_SUPPRESSION_SECRET_LEN = 32


class ConfigurationError(RuntimeError):
    """The process refuses to start with this configuration. Messages name the
    setting to fix, never its value."""


def on_hosting_platform(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return any(env.get(marker) for marker in PLATFORM_MARKERS)


def check_runtime_safety(settings: Settings, environ: Mapping[str, str] | None = None) -> None:
    """Refuse to start a deployment whose security depends on a forgotten variable.

    * On Railway, ``APP_ENV`` must be ``production`` (otherwise cookies lose
      ``Secure``, HSTS is off and a missing SESSION_SECRET becomes ephemeral).
    * In production, ``DATABASE_URL`` must not be SQLite (web and cron would each
      get their own ephemeral file: no shared run lock, lookup cache or budget).
    * In production, ``SUPPRESSION_SECRET`` must be set (erasure suppression).
    """
    if on_hosting_platform(environ) and not settings.is_production:
        raise ConfigurationError("APP_ENV must be 'production' on the hosting platform")
    if not settings.is_production:
        return
    if settings.database_url.startswith("sqlite"):
        raise ConfigurationError("DATABASE_URL must point at Postgres in production (SQLite is not shared)")
    if len(settings.suppression_secret.get_secret_value()) < MIN_SUPPRESSION_SECRET_LEN:
        raise ConfigurationError(
            f"SUPPRESSION_SECRET must be set to at least {MIN_SUPPRESSION_SECRET_LEN} characters "
            "in production"
        )


def describe_security_mode(settings: Settings) -> str:
    """One log line with the effective security posture, and no values."""
    backend = settings.database_url.split(":", 1)[0].split("+", 1)[0] or "unknown"
    secure = "on" if settings.is_production else "off"

    def state(secret: SecretStr) -> str:
        return "set" if secret.get_secret_value() else "missing"

    return (
        f"app_env={settings.app_env} secure_cookies={secure} hsts={secure} db_backend={backend} "
        f"session_secret={state(settings.session_secret)} "
        f"suppression_secret={state(settings.suppression_secret)}"
    )

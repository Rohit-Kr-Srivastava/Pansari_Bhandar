"""Application settings, loaded from environment variables (and .env in development).

Business rules (WhatsApp number, delivery fees, homepage text) are NOT here: they live in the
database and are edited from the admin dashboard. This file only holds deployment settings.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_SECRET = "dev-only-insecure-secret-key-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    debug: bool = False
    database_url: str = "postgresql+psycopg://pansari:pansari@localhost:5432/pansari"
    secret_key: str = DEV_SECRET
    # Public origin used for canonical URLs, sitemap and Open Graph, e.g. https://pansaribhandar.in
    site_url: str = "http://localhost:8000"
    # Uploaded product images. On Railway point this at a mounted volume, e.g. /data/media.
    media_root: Path = Path("media")
    max_upload_mb: int = 5
    session_max_age_days: int = 30
    log_level: str = "INFO"
    # Comma-separated hostnames accepted in the Host header ("*" allows any; dev only).
    allowed_hosts: str = "*"
    db_pool_size: int = 5
    # Reverse proxies in front of the app that append to X-Forwarded-For (Railway: 1).
    # 0 = use the TCP peer address (local development).
    trusted_proxy_hops: int = 0
    # Admin and seller sessions expire sooner than shoppers' (stolen-cookie window).
    staff_session_hours: int = 12

    @field_validator("database_url")
    @classmethod
    def _normalise_db_url(cls, v: str) -> str:
        # Railway and Heroku hand out postgres:// or postgresql:// URLs; use the psycopg 3 driver.
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+psycopg://" + v[len(prefix) :]
        return v

    @field_validator("site_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @model_validator(mode="after")
    def _production_guards(self) -> "Settings":
        if self.environment == "production":
            if self.debug:
                raise ValueError("DEBUG must be false in production")
            weak = self.secret_key == DEV_SECRET or "change-me" in self.secret_key.lower()
            if weak or len(self.secret_key) < 32:
                raise ValueError("Set SECRET_KEY to a random value of at least 32 characters")
            if self.allowed_hosts.strip() == "*":
                raise ValueError("Set ALLOWED_HOSTS in production")
        return self

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def host_list(self) -> list[str]:
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://agent_eval:agent_eval@localhost:5432/agent_eval"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "text"] = "json"
    db_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    worker_idle_seconds: float = Field(default=1.0, gt=0)
    worker_concurrency: int = Field(default=2, ge=1, le=64)
    lease_seconds: float = Field(default=30.0, gt=0)
    heartbeat_seconds: float = Field(default=10.0, gt=0)
    reconcile_seconds: float = Field(default=15.0, gt=0)
    max_job_attempts: int = Field(default=3, ge=1, le=10)
    session_ttl_hours: float = Field(default=12, gt=0)
    session_cookie_secure: bool = False
    invite_ttl_hours: float = Field(default=72, gt=0)
    app_master_key: SecretStr | None = None
    gemini_base_url: str = "https://generativelanguage.googleapis.com"
    model_call_timeout_seconds: float = Field(default=60, gt=0)
    model_call_max_retries: int = Field(default=4, ge=0, le=10)
    ingest_max_bytes: int = Field(default=5_000_000, gt=0)

    @model_validator(mode="after")
    def _heartbeat_fits_the_lease(self) -> "Settings":
        # Two heartbeats per lease leave room to miss one and still stop in time.
        if self.heartbeat_seconds * 2 > self.lease_seconds:
            raise ValueError("heartbeat_seconds must be at most half of lease_seconds")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

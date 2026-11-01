from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://agent_eval:agent_eval@localhost:5432/agent_eval"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "text"] = "json"
    db_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    worker_idle_seconds: float = Field(default=5.0, gt=0)
    session_ttl_hours: float = Field(default=12, gt=0)
    session_cookie_secure: bool = False
    invite_ttl_hours: float = Field(default=72, gt=0)
    app_master_key: SecretStr | None = None
    gemini_base_url: str = "https://generativelanguage.googleapis.com"
    ollama_base_url: str = "http://localhost:11434"
    model_call_timeout_seconds: float = Field(default=60, gt=0)
    model_call_max_retries: int = Field(default=4, ge=0, le=10)


@lru_cache
def get_settings() -> Settings:
    return Settings()

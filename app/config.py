from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "langgraph-service"
    anthropic_api_key: str
    model_name: str = "claude-sonnet-5-5"
    temperature: float = 0.0
    max_tokens: int = 1024
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
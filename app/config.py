from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "langgraph-service"
    app_env: str = "development"

    anthropic_api_key: SecretStr
    service_api_key: SecretStr

    model_name: str = "claude-sonnet-5-5"
    temperature: float = 0.0
    max_tokens: int = 1024
    llm_timeout: int = 60

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
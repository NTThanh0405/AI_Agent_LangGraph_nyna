# app/config.py
# Cấu hình toàn bộ dịch vụ, đọc từ biến môi trường / file .env: khoá API, tham số LLM,
# kết nối PostgreSQL chỉ-đọc (kèm schema mặc định), dashboard và lưu hội thoại.
# Biến bắt buộc mà thiếu (hoặc khoá service quá ngắn) thì app báo lỗi ngay khi khởi động.

from functools import lru_cache
from typing import Optional
from urllib.parse import quote

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "langgraph-service"
    app_env: str = "production"  # đặt APP_ENV=development khi chạy local để bật /docs

    anthropic_api_key: SecretStr
    service_api_key: SecretStr  # khoá n8n gửi kèm ở header x-api-key, tối thiểu 32 ký tự

    model_name: str = "claude-sonnet-5-5"
    temperature: float = 0.0
    max_tokens: int = 4096  # create_dashboard nhiều biểu đồ cần chỗ cho JSON của tool call
    llm_timeout: int = 60

    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "NynaGroup"
    db_user: str = "agent_readonly"
    db_password: SecretStr  # bắt buộc, không có mặc định
    db_schema: str = "dwh"  # schema áp dụng cho bảng không ghi schema (guard, describe_table, search_path)

    db_pool_min: int = 1
    db_pool_max: int = 5
    db_statement_timeout_ms: int = 15000
    db_max_rows: int = 200

    dashboard_dir: str = "/data/dashboards"
    dashboard_base_url: str = ""
    dashboard_ttl_hours: int = 24
    dashboard_max_charts: int = 6

    checkpoint_database_url: Optional[SecretStr] = None  # bỏ trống -> MemorySaver (RAM)

    log_level: str = "INFO"

    @field_validator("service_api_key")
    @classmethod
    def _service_key_long_enough(cls, v: SecretStr) -> SecretStr:
        if len(v.get_secret_value()) < 32:
            raise ValueError("SERVICE_API_KEY phải dài ít nhất 32 ký tự")
        return v

    @field_validator("db_schema")
    @classmethod
    def _schema_is_identifier(cls, v: str) -> str:
        v = v.strip().lower()
        if not v.isidentifier():
            raise ValueError("DB_SCHEMA chỉ được gồm chữ, số, dấu gạch dưới")
        return v

    @property
    def database_url(self) -> str:
        user = quote(self.db_user, safe="")
        password = quote(self.db_password.get_secret_value(), safe="")
        return (
            f"postgresql://{user}:{password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
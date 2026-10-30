# app/config.py
# Cấu hình toàn bộ dịch vụ, đọc từ biến môi trường / file .env: khoá API, tham số LLM,
# bot Telegram (allowlist, rate limit, phiên), kết nối PostgreSQL chỉ-đọc (kèm schema
# mặc định), dashboard và lưu hội thoại.
# Biến bắt buộc mà thiếu (hoặc cấu hình sai) thì app báo lỗi ngay khi khởi động.

from functools import lru_cache
from typing import Literal, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "langgraph-service"
    app_env: str = "production"  # đặt APP_ENV=development khi chạy local để bật /docs
    timezone: str = "Asia/Ho_Chi_Minh"  # múi giờ để tính "hôm nay", "tuần này", "tháng này"

    anthropic_api_key: SecretStr
    telegram_bot_token: SecretStr  # token từ @BotFather

    # Không còn n8n gọi vào agent nên không bắt buộc. Chỉ đặt nếu bạn mở lại endpoint
    # nội bộ bảo vệ bằng header x-api-key (app/api/deps.py). Nếu đặt thì tối thiểu 32 ký tự.
    service_api_key: Optional[SecretStr] = None

    model_name: str = "claude-sonnet-5-5"
    temperature: float = 0.0
    max_tokens: int = 4096  # create_dashboard nhiều biểu đồ cần chỗ cho JSON của tool call
    llm_timeout: int = 60
    graph_max_steps: int = 12  # số bước tối đa của vòng chat <-> tools trong một lượt hỏi

    # ---- Telegram bot ----
    allowlist_path: str = "config/allowlist.yaml"  # ai được dùng bot và vai trò của họ
    rate_limit_per_min: int = 5      # số câu hỏi tối đa mỗi phút cho mỗi người
    rate_limit_per_day: int = 100    # số câu hỏi tối đa mỗi ngày cho mỗi người
    session_ttl_minutes: int = 60    # im lặng quá thời gian này thì xoá hội thoại (sessions.py)

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

    # ---- Dashboard ----
    # "file": bot gửi file HTML qua Telegram. "link": ghi file vào dashboard_dir,
    # nginx phục vụ và bot gửi link (cần dashboard_base_url).
    delivery_mode: Literal["file", "link"] = "file"
    dashboard_dir: str = "/data/dashboards"
    dashboard_base_url: str = ""
    dashboard_ttl_hours: int = 24
    dashboard_max_charts: int = 6

    checkpoint_database_url: Optional[SecretStr] = None  # bỏ trống -> MemorySaver (RAM)

    log_level: str = "INFO"

    @field_validator("service_api_key")
    @classmethod
    def _service_key_long_enough(cls, v: Optional[SecretStr]) -> Optional[SecretStr]:
        if v is not None and len(v.get_secret_value()) < 32:
            raise ValueError("SERVICE_API_KEY phải dài ít nhất 32 ký tự")
        return v

    @field_validator("timezone")
    @classmethod
    def _timezone_is_valid(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except Exception as e:  # ZoneInfoNotFoundError, ValueError...
            raise ValueError(f"TIMEZONE không hợp lệ: {v!r}") from e
        return v

    @field_validator("db_schema")
    @classmethod
    def _schema_is_identifier(cls, v: str) -> str:
        v = v.strip().lower()
        if not v.isidentifier():
            raise ValueError("DB_SCHEMA chỉ được gồm chữ, số, dấu gạch dưới")
        return v

    @model_validator(mode="after")
    def _link_mode_needs_base_url(self) -> "Settings":
        if self.delivery_mode == "link" and not self.dashboard_base_url:
            raise ValueError("DELIVERY_MODE=link cần đặt DASHBOARD_BASE_URL")
        return self

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
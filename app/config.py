# app/config.py
# Cấu hình toàn bộ dịch vụ: đọc từ biến môi trường / file .env.
# Gồm: thông tin app, khoá API (Anthropic + khoá bảo vệ service), tham số LLM,
# và kết nối PostgreSQL nội bộ cho agent phân tích dữ liệu.

from functools import lru_cache
from typing import Optional
from urllib.parse import quote

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Đọc giá trị từ file .env; bỏ qua các biến không khai báo trong class này.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- Thông tin ứng dụng ----
    app_name: str = "langgraph-service"
    # "production" sẽ tắt trang /docs (xem app/main.py)
    app_env: str = "development"

    # ---- Khoá bí mật (bắt buộc phải có trong .env) ----
    anthropic_api_key: SecretStr  # khoá gọi Claude
    service_api_key: SecretStr    # khoá n8n phải gửi kèm (header x-api-key)

    # ---- Tham số LLM ----
    model_name: str = "claude-sonnet-5-5"
    temperature: float = 0.0   # 0 để số liệu phân tích ổn định, không "sáng tạo"
    max_tokens: int = 2048     # đủ chỗ cho câu trả lời phân tích có bảng số liệu
    llm_timeout: int = 60      # giây

    # ---- PostgreSQL nội bộ (tài khoản CHỈ-ĐỌC dành cho agent truy vấn) ----
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "postgres"
    db_user: str = "agent_readonly"      # role chỉ có quyền SELECT
    db_password: SecretStr = SecretStr("")

    # Connection pool
    db_pool_min: int = 1
    db_pool_max: int = 5

    # Giới hạn an toàn cho mỗi câu truy vấn của agent
    db_statement_timeout_ms: int = 15000  # tự huỷ câu SQL chạy quá 15 giây
    db_max_rows: int = 200                # số dòng tối đa trả về cho LLM

    # ---- Lưu hội thoại (checkpoint) ----
    # Role chỉ-đọc KHÔNG tạo/ghi được bảng checkpoint, nên cần chuỗi kết nối
    # riêng (role có quyền ghi vào schema dành cho agent).
    # Bỏ trống -> dùng MemorySaver (mất hội thoại khi restart, chỉ để chạy thử).
    checkpoint_database_url: Optional[SecretStr] = None

    # ---- Log ----
    log_level: str = "INFO"

    @property
    def database_url(self) -> str:
        """Chuỗi kết nối PostgreSQL cho tài khoản chỉ-đọc.

        Mật khẩu/user được mã hoá URL để ký tự đặc biệt (@, :, /...) không làm
        hỏng chuỗi kết nối.
        """
        user = quote(self.db_user, safe="")
        password = quote(self.db_password.get_secret_value(), safe="")
        return (
            f"postgresql://{user}:{password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


@lru_cache
def get_settings() -> Settings:
    """Trả về cấu hình dùng chung (chỉ đọc .env một lần nhờ lru_cache)."""
    return Settings()
# app/agent/contracts.py
# Kiểu dữ liệu chung giữa các agent (Pydantic v2). Mỗi agent trả đúng một kiểu, graph chỉ truyền
# các kiểu này qua state, không truyền văn bản tự do. Dữ liệu sai dạng thì báo lỗi ngay thay vì
# chạy tiếp với số liệu hỏng.

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_RESULT_ROWS = 200  # khớp db_max_rows trong config.py
MAX_CHART_POINTS = 500


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AnalysisBrief(_Base):
    """Analyst hiểu câu hỏi -> bản tóm tắt để Coder/Designer làm theo."""

    question: str = Field(min_length=1, max_length=2000)
    intent: Literal["lookup", "compare", "trend", "explain_change", "report", "dashboard", "other"] = "lookup"
    metrics: list[str] = Field(default_factory=list)       # ví dụ ["revenue", "orders"]
    dimensions: list[str] = Field(default_factory=list)    # ví dụ ["channel", "day"]
    filters: dict[str, str] = Field(default_factory=dict)  # ví dụ {"channel": "GrabFood"}
    period_start: Optional[date] = None                    # gồm ngày này
    period_end: Optional[date] = None                      # gồm ngày này
    compare_to_previous: bool = False
    preferred_query: Optional[str] = None                  # tên file trong queries/, không có đuôi .sql
    needs_chart: bool = False
    needs_dashboard: bool = False
    # Trang dashboard dựng sẵn người dùng muốn (tong_quan, cua_hang...; xem dashboard_presets.PAGE_NAMES).
    # Rỗng = báo cáo đầy đủ 5 trang chính; ["custom"] = yêu cầu không khớp trang nào (Designer tự khai báo).
    dashboard_pages: list[str] = Field(default_factory=list, max_length=8)
    notes: str = Field(default="", max_length=1000)        # lưu ý nghiệp vụ (ngày lễ, khuyến mãi...)

    @field_validator("dashboard_pages")
    @classmethod
    def _known_pages_only(cls, v: list[str]) -> list[str]:
        """Bỏ tên trang lạ (LLM viết sai) thay vì làm hỏng cả bản tóm tắt; giữ thứ tự, bỏ trùng."""
        from app.agent.tools.dashboard_presets import PAGE_NAMES

        allowed = set(PAGE_NAMES) | {"custom"}
        out: list[str] = []
        for name in v:
            n = (name or "").strip().lower()
            if n in allowed and n not in out:
                out.append(n)
        return out

    @field_validator("preferred_query")
    @classmethod
    def _query_name_is_plain(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not v.replace("_", "").isalnum():
            raise ValueError("preferred_query chỉ gồm chữ, số, gạch dưới (tên file, không có đường dẫn)")
        return v

    @model_validator(mode="after")
    def _period_is_ordered(self) -> "AnalysisBrief":
        if (self.period_start is None) != (self.period_end is None):
            raise ValueError("period_start và period_end phải cùng có hoặc cùng không")
        if self.period_start and self.period_end and self.period_start > self.period_end:
            raise ValueError("period_start phải <= period_end")
        return self


class QueryResult(_Base):
    """Kết quả một câu SQL do Coder chạy (qua guard.py)."""

    sql: str = Field(min_length=1)
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = Field(default=0, ge=0)
    truncated: bool = False                                 # True nếu bị cắt ở MAX_RESULT_ROWS
    source_file: Optional[str] = None                       # nếu dùng file trong queries/
    error: Optional[str] = None

    @model_validator(mode="after")
    def _shape_is_consistent(self) -> "QueryResult":
        if self.error is None:
            if len(self.rows) > MAX_RESULT_ROWS:
                raise ValueError(f"quá {MAX_RESULT_ROWS} dòng, phải cắt trước khi đưa vào state")
            for r in self.rows:
                if len(r) != len(self.columns):
                    raise ValueError("số ô của dòng không khớp số cột")
            if self.row_count < len(self.rows):
                raise ValueError("row_count nhỏ hơn số dòng thực tế")
        return self

    @property
    def ok(self) -> bool:
        return self.error is None


class ChartSpec(_Base):
    """Designer mô tả một biểu đồ; chart_tools dựng ảnh từ đây."""

    kind: Literal["bar", "line", "pie", "table"]
    title: str = Field(min_length=1, max_length=120)
    x: str                                                  # tên cột trục X (hoặc nhãn)
    y: list[str] = Field(min_length=1, max_length=4)        # một hoặc vài cột số
    data: list[dict[str, Any]] = Field(min_length=1, max_length=MAX_CHART_POINTS)
    unit: Optional[Literal["vnd", "count", "percent"]] = None

    @model_validator(mode="after")
    def _columns_exist_and_numeric(self) -> "ChartSpec":
        for row in self.data:
            for col in [self.x, *self.y]:
                if col not in row:
                    raise ValueError(f"thiếu cột {col!r} trong data")
        if self.kind != "table":
            for row in self.data:
                for col in self.y:
                    v = row[col]
                    if v is not None and not isinstance(v, (int, float, Decimal)) or isinstance(v, bool):
                        raise ValueError(f"cột {col!r} phải là số")
        if self.kind == "pie" and len(self.y) != 1:
            raise ValueError("biểu đồ tròn chỉ có một cột giá trị")
        return self


class ReportSection(_Base):
    """Một mục của báo cáo tự động (dùng chung nếu builder.py cần kiểu chuẩn)."""

    key: str
    title: str
    columns: list[str]
    rows: list[list[Any]]
    error: Optional[str] = None


class ReportResult(_Base):
    manager_id: int
    sections: list[ReportSection]
    skipped_sections: list[str] = Field(default_factory=list)  # bị bỏ do thiếu quyền
    text: str

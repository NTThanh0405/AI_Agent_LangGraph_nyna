# Dockerfile
# Đóng gói dịch vụ LangGraph (FastAPI) thành Docker image để chạy trong docker compose.

FROM python:3.11-slim

WORKDIR /code

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install -r requirements.txt

RUN useradd -m appuser

# Thư mục chứa dashboard HTML (gắn volume ở docker-compose), appuser phải ghi được
RUN mkdir -p /data/dashboards && chown appuser:appuser /data/dashboards

COPY --chown=appuser:appuser app ./app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/v1/health')" || exit 1

# Chỉ một worker: sessions.py giữ phiên và khoá trong RAM, không thêm --workers
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
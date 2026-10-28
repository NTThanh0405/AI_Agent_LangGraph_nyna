# app/api/deps.py
import secrets

from fastapi import Header, HTTPException, status

from app.config import get_settings


async def verify_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = get_settings().service_api_key.get_secret_value()
    if not x_api_key or not secrets.compare_digest(
        x_api_key.encode(), expected.encode()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key"
        )
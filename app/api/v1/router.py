# app/api/v1/router.py
# Gom các route của API v1 dưới tiền tố /api/v1 và quyết định route nào cần x-api-key.

from fastapi import APIRouter, Depends

from app.api.deps import verify_api_key
from app.api.v1 import agent_route, health

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)  # không cần key
api_router.include_router(agent_route.router, dependencies=[Depends(verify_api_key)])
# app/api/v1/router.py
from fastapi import APIRouter, Depends

from app.api.deps import verify_api_key
from app.api.v1 import agent_route, health

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)  # không cần key
api_router.include_router(agent_route.router, dependencies=[Depends(verify_api_key)])
from fastapi import APIRouter

from app.api.v1 import agent_route, health

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(agent_route.router)
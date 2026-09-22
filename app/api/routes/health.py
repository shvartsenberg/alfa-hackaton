"""Health and readiness routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.api.dependencies import get_policy_provider, get_restoration_store
from app.api.schemas.process import HealthResponse
from app.policies.provider import PolicyProvider
from app.restoration.base import RestorationStore

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/health/ready")
async def readiness(
    policy_provider: PolicyProvider = Depends(get_policy_provider),
    restoration_store: RestorationStore = Depends(get_restoration_store),
) -> JSONResponse:
    """Readiness probe: verifies core dependencies are available."""
    try:
        policy_provider.get_policy("default")
        restoration_store.get("__readiness_probe__")
    except Exception:
        return JSONResponse(status_code=503, content={"status": "not_ready"})
    return JSONResponse(status_code=200, content={"status": "ready"})
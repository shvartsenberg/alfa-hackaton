"""GET /metrics route exposing the Prometheus text format.

This is a technical endpoint and does not change the POST /process contract.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST

from app.api.dependencies import get_metrics
from app.observability.metrics import Metrics, render_prometheus

router = APIRouter()


@router.get("/metrics", response_class=Response)
async def metrics(metrics: Metrics = Depends(get_metrics)) -> Response:
    return Response(
        render_prometheus(metrics),
        media_type=CONTENT_TYPE_LATEST,
    )

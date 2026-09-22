"""POST /process route."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request

from app.api.dependencies import (
    get_default_consumer_id,
    get_policy_provider,
    get_process_service,
)
from app.api.schemas.process import ProcessRequest, ProcessResponse
from app.core.security import hash_identifier
from app.observability.logging import get_logger
from app.policies.provider import PolicyProvider
from app.processing.process_service import ProcessService

router = APIRouter()
logger = get_logger("api.process")

_CONSUMER_HEADER = "X-Consumer-ID"


@router.post("/process", response_model=ProcessResponse)
async def process(
    request: Request,
    body: ProcessRequest,
    process_service: ProcessService = Depends(get_process_service),
    policy_provider: PolicyProvider = Depends(get_policy_provider),
    default_consumer_id: str = Depends(get_default_consumer_id),
    x_consumer_id: str | None = Header(default=None, alias=_CONSUMER_HEADER),
) -> ProcessResponse:
    consumer_id = _normalize_consumer(x_consumer_id, default_consumer_id)
    policy = policy_provider.get_policy(consumer_id)
    outcome = process_service.process(body.payload, body.payload_id, policy)
    logger.info(
        "process_completed",
        extra={
            "event": "process_completed",
            "operation": outcome.operation.value,
            "payload_id_hash": hash_identifier(body.payload_id),
            "consumer_id": consumer_id,
            "detected_types": outcome.detected_types,
            "entity_count": outcome.entity_count,
            "duration_ms": round(outcome.duration_ms, 3),
        },
    )
    return ProcessResponse(result=outcome.result)


def _normalize_consumer(value: str | None, default: str) -> str:
    """Return a validated consumer id, falling back to the default.

    The header value is normalized (trimmed, lowercased) before use so an
    unvalidated value is never logged or used as-is.
    """
    if value is None:
        return default
    normalized = value.strip().lower()
    return normalized or default
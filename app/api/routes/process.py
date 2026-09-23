"""POST /process route."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.dependencies import (
    get_consumer_id,
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


@router.post("/process", response_model=ProcessResponse)
def process(
    request: Request,
    body: ProcessRequest,
    process_service: ProcessService = Depends(get_process_service),
    policy_provider: PolicyProvider = Depends(get_policy_provider),
    consumer_id: str = Depends(get_consumer_id),
) -> ProcessResponse:
    # A plain ``def`` handler lets FastAPI run the synchronous CPU/storage
    # pipeline in its thread pool, so a slow request does not block the async
    # event loop and other requests can proceed concurrently.
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
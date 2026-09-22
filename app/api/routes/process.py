"""POST /process route."""

from __future__ import annotations

from fastapi import APIRouter, Depends

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


@router.post("/process", response_model=ProcessResponse)
async def process(
    request: ProcessRequest,
    process_service: ProcessService = Depends(get_process_service),
    policy_provider: PolicyProvider = Depends(get_policy_provider),
    consumer_id: str = Depends(get_default_consumer_id),
) -> ProcessResponse:
    policy = policy_provider.get_policy(consumer_id)
    outcome = process_service.process(request.payload, request.payload_id, policy)
    logger.info(
        "process operation=%s payload_id_hash=%s consumer=%s "
        "detected_types=%s entity_count=%s duration_ms=%.2f",
        outcome.operation.value,
        hash_identifier(request.payload_id),
        consumer_id,
        outcome.detected_types,
        outcome.entity_count,
        outcome.duration_ms,
    )
    return ProcessResponse(result=outcome.result)
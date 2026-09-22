"""API request/response schemas.

The /process contract is fixed: only ``payload`` and ``payload_id`` in, only
``result`` out. Do not add required fields that would break the automated
checker.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ProcessRequest(BaseModel):
    payload: str = Field(min_length=1)
    payload_id: str = Field(min_length=1)


class ProcessResponse(BaseModel):
    result: str


class HealthResponse(BaseModel):
    status: str
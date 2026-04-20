from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SubscriptionTokenPath(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    token: str = Field(min_length=32, max_length=256)


class ErrorResponse(BaseModel):
    error: str
    code: int


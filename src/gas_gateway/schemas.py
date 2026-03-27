"""Pydantic schemas for Gas Gateway Agent."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class GasState(str, Enum):
    COUNTING = "COUNTING"
    LOW_STOCK = "LOW_STOCK"


class CountSnapshot(BaseModel):
    """Snapshot document sent to Firebase and/or customer DB."""
    device_id: str
    app_id: str
    total_count: int = Field(ge=0)
    low_stock: bool = False
    location: str = ""
    operator_id: str = ""
    trigger: str = Field("periodic", pattern=r"^(periodic|manual|alert)$")
    timestamp: str = ""


class ManualSnapshotRequest(BaseModel):
    """POST /snapshot — operator-triggered manual count snapshot."""
    note: str = Field("", max_length=256)


class GasHealthResponse(BaseModel):
    status: str
    module: str
    app_id: str
    device_id: str
    state: str
    total_count: int
    low_stock_threshold: int
    location: str

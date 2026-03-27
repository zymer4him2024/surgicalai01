"""Pydantic schemas for Gas Display Agent."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class GasBorderColor(str, Enum):
    GREEN = "green"   # COUNTING — normal inventory
    RED = "red"       # LOW_STOCK — below threshold


class GasHUDUpdate(BaseModel):
    """POST /hud — update the gas inventory display panel."""
    total_count: Optional[int] = None
    state: Optional[str] = None  # "COUNTING" or "LOW_STOCK"
    location: Optional[str] = None
    operator_id: Optional[str] = None
    ai_fps: Optional[float] = None
    npu_temp_celsius: Optional[float] = None
    cpu_temp_celsius: Optional[float] = None
    thermal_status: Optional[str] = None
    inference_ready: Optional[bool] = None


class GasHealthResponse(BaseModel):
    status: str
    module: str
    headless: bool
    render_fps: float
    display_resolution: str

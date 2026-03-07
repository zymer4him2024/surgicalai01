"""
schemas.py — Inference Agent API schemas
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ThermalStatus(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"   # >= 85°C
    CRITICAL = "critical" # >= 95°C — inference refused


class Detection(BaseModel):
    class_id: int = Field(..., description="YOLO class index")
    class_name: str = Field(..., description="Class label (e.g. 'scalpel')")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence score")
    bbox: list[float] = Field(
        ...,
        min_length=4,
        max_length=4,
        description="[x_min, y_min, x_max, y_max] in absolute pixel coords",
    )
    keypoints: Optional[list[list[float]]] = Field(
        None, description="YOLO Pose [x, y] coordinates (12 keypoints)"
    )


class PredictResponse(BaseModel):
    detections: list[Detection] = Field(default_factory=list)
    inference_time_ms: float = Field(..., description="NPU inference time (ms)")
    npu_temp_celsius: Optional[float] = Field(None, description="NPU temperature (°C)")
    thermal_status: ThermalStatus = Field(ThermalStatus.NORMAL)
    warning: Optional[str] = Field(None, description="Overheat warning message")


class HealthResponse(BaseModel):
    status: str
    module: str
    npu_ready: bool
    npu_temp_celsius: Optional[float] = None
    thermal_status: ThermalStatus = ThermalStatus.NORMAL


class MetricsResponse(BaseModel):
    npu_temp_celsius: Optional[float] = None
    thermal_status: ThermalStatus
    total_inferences: int
    avg_inference_time_ms: float
    inference_process_alive: bool

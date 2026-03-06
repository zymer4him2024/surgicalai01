"""
schemas.py — Inference Agent API 입출력 스키마 정의
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ThermalStatus(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"       # 85 ℃ 이상
    CRITICAL = "critical"     # 95 ℃ 이상 → 추론 거부


class Detection(BaseModel):
    """단일 객체 탐지 결과"""

    class_id: int = Field(..., description="YOLO 클래스 인덱스")
    class_name: str = Field(..., description="클래스 레이블 (예: 'scalpel')")
    confidence: float = Field(..., ge=0.0, le=1.0, description="신뢰도 점수")
    bbox: list[float] = Field(
        ...,
        min_length=4,
        max_length=4,
        description="[x_min, y_min, x_max, y_max] — 픽셀 절대 좌표",
    )
    keypoints: Optional[list[list[float]]] = Field(
        None, description="YOLO Pose [x, y] 좌표 목록 (12 keypoints)"
    )


class PredictResponse(BaseModel):
    """POST /predict 응답"""

    detections: list[Detection] = Field(default_factory=list)
    inference_time_ms: float = Field(..., description="NPU 추론 소요 시간 (ms)")
    npu_temp_celsius: Optional[float] = Field(None, description="NPU 온도 (℃)")
    thermal_status: ThermalStatus = Field(ThermalStatus.NORMAL)
    warning: Optional[str] = Field(None, description="과열 경고 메시지")


class HealthResponse(BaseModel):
    """GET /health 응답"""

    status: str
    module: str
    npu_ready: bool
    npu_temp_celsius: Optional[float] = None
    thermal_status: ThermalStatus = ThermalStatus.NORMAL


class MetricsResponse(BaseModel):
    """GET /metrics 응답"""

    npu_temp_celsius: Optional[float] = None
    thermal_status: ThermalStatus
    total_inferences: int
    avg_inference_time_ms: float
    inference_process_alive: bool

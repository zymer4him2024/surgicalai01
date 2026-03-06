"""
schemas.py — Device Master Agent API 스키마
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel


class DeviceLookupResponse(BaseModel):
    """GET /device/lookup 응답 — FDA 표준 기기 정보"""

    yolo_label: str                      # 원본 YOLO 레이블 (e.g. "forceps")
    device_name: str                     # FDA 표준 기기명 (e.g. "Tissue Forceps, Ring")
    product_code: Optional[str] = None   # FDA 3자리 제품 코드 (e.g. "GZY")
    device_class: Optional[str] = None   # FDA 등급: "I", "II", "III"
    medical_specialty: Optional[str] = None  # 진료 전문과 분류
    data_source: Literal["cache", "openfda_live", "fallback"] = "fallback"


class CacheStatus(BaseModel):
    loaded: bool
    label_count: int
    cache_age_hours: Optional[float] = None   # None이면 파일 없음 (인메모리 전용)
    openfda_reachable: bool = False


class HealthResponse(BaseModel):
    status: str
    module: str
    cache: CacheStatus

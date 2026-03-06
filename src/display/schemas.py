"""
schemas.py — Display Agent API 입출력 스키마
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class BorderColor(str, Enum):
    GREEN = "green"      # 정상: 트레이 완성, 시스템 이상 없음
    YELLOW = "yellow"    # 경고: 불확실, 확인 필요
    RED = "red"          # 위험: 기기 누락, NPU 과열 등 크리티컬


class Detection(BaseModel):
    """단일 탐지 결과 (화면 오버레이용)"""
    class_name: str
    confidence: float = Field(ge=0.0, le=1.0)
    bbox: list[float] = Field(min_length=4, max_length=4)  # [x1,y1,x2,y2] 640×640 기준
    keypoints: Optional[list[list[float]]] = Field(None, description="[x, y] 640x640 기준")


class AIStatus(BaseModel):
    inference_ready: bool = True
    fps: float = 0.0
    npu_temp_celsius: Optional[float] = None
    thermal_status: str = "normal"         # normal / warning / critical


class NetworkStatus(BaseModel):
    gateway: bool = True
    inference: bool = True
    camera: bool = True


class TrayItem(BaseModel):
    class_name: str
    count: int = Field(ge=0)
    device_name: Optional[str] = None    # FDA 표준 기기명 (device_master 연동 시)
    product_code: Optional[str] = None   # FDA 제품 코드 (e.g. "GZY")
    fda_class: Optional[str] = None      # FDA 등급 (e.g. "I", "II")


class FrameUpdate(BaseModel):
    """POST /frame — 카메라 프레임 갱신 (base64 인코딩)"""
    image_b64: str = Field(..., description="JPEG 이미지 base64 문자열")
    detections: list[Detection] = Field(default_factory=list)


class ScanInfo(BaseModel):
    """바코드 스캔 시 기록되는 작업 메타데이터 (DATA INFO 패널용)"""
    job_id: str
    scanned_at: str                          # "HH:MM:SS" 형식
    target: dict[str, int] = Field(default_factory=dict)


class HUDUpdate(BaseModel):
    """POST /hud — HUD 데이터 부분 갱신 (None 필드는 유지)"""
    ai_status: Optional[AIStatus] = None
    network_status: Optional[NetworkStatus] = None
    tray_items: Optional[list[TrayItem]] = None
    border_color: Optional[BorderColor] = None
    scan_info: Optional[ScanInfo] = None
    flash_text: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    module: str
    headless: bool
    render_fps: float
    display_resolution: str

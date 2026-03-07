"""
schemas.py — Display Agent API schemas
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class BorderColor(str, Enum):
    GREEN = "green"   # match confirmed, no issues
    YELLOW = "yellow" # standby / uncertain
    RED = "red"       # mismatch / critical error


class Detection(BaseModel):
    class_name: str
    confidence: float = Field(ge=0.0, le=1.0)
    bbox: list[float] = Field(min_length=4, max_length=4)  # [x1,y1,x2,y2] in 640x640 space
    keypoints: Optional[list[list[float]]] = Field(None, description="[x, y] in 640x640 space")


class AIStatus(BaseModel):
    inference_ready: bool = True
    fps: float = 0.0
    npu_temp_celsius: Optional[float] = None
    cpu_temp_celsius: Optional[float] = None
    thermal_status: str = "normal"  # normal / warning / critical


class NetworkStatus(BaseModel):
    gateway: bool = True
    inference: bool = True
    camera: bool = True


class TrayItem(BaseModel):
    class_name: str
    count: int = Field(ge=0)
    device_name: Optional[str] = None
    product_code: Optional[str] = None
    fda_class: Optional[str] = None


class FrameUpdate(BaseModel):
    """POST /frame — camera frame update (base64 encoded)"""
    image_b64: str = Field(..., description="JPEG image as base64 string")
    detections: list[Detection] = Field(default_factory=list)


class ScanInfo(BaseModel):
    """Job metadata recorded at QR scan time (DATA INFO panel)"""
    job_id: str
    scanned_at: str  # "HH:MM:SS" format
    target: dict[str, int] = Field(default_factory=dict)


class HUDUpdate(BaseModel):
    """POST /hud — partial HUD update (None fields are preserved)"""
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

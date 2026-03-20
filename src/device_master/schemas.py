"""
schemas.py — Device Master Agent API schemas
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel


class DeviceLookupResponse(BaseModel):
    """GET /device/lookup response — device info from Digioptics Firebase catalog"""

    yolo_label: str                      # original YOLO label (e.g. "forceps")
    device_name: str                     # standard device name (e.g. "Tissue Forceps, Ring")
    product_code: Optional[str] = None   # FDA 3-char product code (e.g. "GZY")
    device_class: Optional[str] = None   # FDA class: "I", "II", "III"
    medical_specialty: Optional[str] = None  # medical specialty classification
    data_source: Literal["cache", "fallback", "firebase_catalog"] = "fallback"


class CacheStatus(BaseModel):
    loaded: bool
    label_count: int
    cache_age_hours: Optional[float] = None   # None if no file (in-memory only)
    firebase_reachable: bool = False


class HealthResponse(BaseModel):
    status: str
    module: str
    app_id: str = "unknown"
    device_id: str = "unknown"
    cache: CacheStatus

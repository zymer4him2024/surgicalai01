"""schemas.py — Pydantic models for Model Converter Agent."""

from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field

ConversionStatusT = Literal[
    "pending_conversion", "processing", "ready", "failed"
]


class ConversionJobRequest(BaseModel):
    model_id: str
    storage_raw_path: str           # Firebase Storage path to uploaded file
    original_format: Literal["pt", "onnx", "har"]
    model_name: str
    hw_arch: str = "hailo8"
    class_names: Optional[list[str]] = None


class ConversionJobStatus(BaseModel):
    model_id: str
    conversion_status: ConversionStatusT
    conversion_log: list[str] = Field(default_factory=list)
    hef_download_url: Optional[str] = None
    error_message: Optional[str] = None
    class_names: Optional[list[str]] = None
    input_resolution: Optional[int] = None


class HealthResponse(BaseModel):
    status: str
    hailo_available: bool
    ultralytics_available: bool
    firebase_connected: bool
    active_jobs: int

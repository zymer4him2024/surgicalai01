"""
schemas.py — Firebase Sync Agent API 스키마
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class EventType(str, Enum):
    MISMATCH = "mismatch"      # 탐지 수량 불일치 (스냅샷 3장 촬영)
    ALERT = "alert"            # 크리티컬 경고 (NPU 과열 등)
    PERIODIC = "periodic"      # 주기적 정상 상태 기록


class ItemStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class SyncRequest(BaseModel):
    """POST /sync — 동기화 이벤트 요청"""

    event_type: EventType
    expected_count: int = Field(0, ge=0, description="예상 기구 수")
    actual_count: int = Field(0, ge=0, description="탐지된 기구 수")
    missing_items: list[str] = Field(default_factory=list, description="누락 기구 목록")
    detected_items: list[dict] = Field(default_factory=list, description="탐지 결과 (detections)")
    metadata: dict = Field(default_factory=dict, description="추가 메타데이터")


class SyncResponse(BaseModel):
    """POST /sync 응답"""

    event_id: int
    status: str = "queued"
    message: str


class QueueItemDetail(BaseModel):
    """GET /queue/item/{event_id} 응답"""

    event_id: int
    event_type: str
    status: ItemStatus
    retry_count: int
    firestore_doc_id: Optional[str] = None
    storage_urls: list[str] = Field(default_factory=list)
    created_at: str
    error_message: Optional[str] = None


class QueueStatusResponse(BaseModel):
    """GET /queue/status 응답"""

    total_pending: int
    total_processing: int
    total_done: int
    total_failed: int
    firebase_reachable: bool
    simulation_mode: bool


class HealthResponse(BaseModel):
    """GET /health 응답"""

    status: str
    module: str
    firebase_configured: bool
    simulation_mode: bool
    queue_depth: int
    firebase_reachable: bool

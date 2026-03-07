"""
schemas.py — Firebase Sync Agent API schemas
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class EventType(str, Enum):
    MISMATCH = "mismatch"      # detection count mismatch (3 snapshots taken)
    ALERT = "alert"            # critical warning (NPU overheat, etc.)
    PERIODIC = "periodic"      # periodic normal-state recording


class ItemStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class SyncRequest(BaseModel):
    """POST /sync — sync event request"""

    event_type: EventType
    expected_count: int = Field(0, ge=0, description="Expected instrument count")
    actual_count: int = Field(0, ge=0, description="Detected instrument count")
    missing_items: list[str] = Field(default_factory=list, description="Missing instrument list")
    detected_items: list[dict] = Field(default_factory=list, description="Detection results")
    metadata: dict = Field(default_factory=dict, description="Additional metadata")


class SyncResponse(BaseModel):
    """POST /sync response"""

    event_id: int
    status: str = "queued"
    message: str


class QueueItemDetail(BaseModel):
    """GET /queue/item/{event_id} response"""

    event_id: int
    event_type: str
    status: ItemStatus
    retry_count: int
    firestore_doc_id: Optional[str] = None
    storage_urls: list[str] = Field(default_factory=list)
    created_at: str
    error_message: Optional[str] = None


class QueueStatusResponse(BaseModel):
    """GET /queue/status response"""

    total_pending: int
    total_processing: int
    total_done: int
    total_failed: int
    firebase_reachable: bool
    simulation_mode: bool


class HealthResponse(BaseModel):
    """GET /health response"""

    status: str
    module: str
    firebase_configured: bool
    simulation_mode: bool
    queue_depth: int
    firebase_reachable: bool

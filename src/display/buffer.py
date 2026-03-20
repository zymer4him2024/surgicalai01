"""
buffer.py — Software double buffer + shared display state

Double buffering:
  Back Buffer (write) → flip() → Front Buffer (read) → cv2.imshow()

- Render thread draws a complete frame to the back buffer, then calls flip()
- imshow() always reads the completed front buffer — no partial frames exposed
- No tearing or flicker on border color transitions
"""

from __future__ import annotations

import base64
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np

from src.display.schemas import (
    AIStatus,
    BorderColor,
    Detection,
    NetworkStatus,
    ScanInfo,
    TrayItem,
)


# ─────────────────────────────────────────────────────────────────────────────
# Double Buffer
# ─────────────────────────────────────────────────────────────────────────────

class DoubleBuffer:
    """
    Software double buffer backed by numpy arrays.
    Render thread writes to back and calls flip().
    Display reads from get_front() for the completed frame.
    """

    def __init__(self, width: int, height: int) -> None:
        shape = (height, width, 3)
        self._a = np.zeros(shape, dtype=np.uint8)
        self._b = np.zeros(shape, dtype=np.uint8)
        self._back: np.ndarray = self._a
        self._front: np.ndarray = self._b
        self._lock = threading.Lock()

    @property
    def back(self) -> np.ndarray:
        return self._back

    def flip(self) -> None:
        with self._lock:
            self._back, self._front = self._front, self._back

    def get_front(self) -> np.ndarray:
        with self._lock:
            return self._front.copy()

    def peek_front(self) -> np.ndarray:
        """Return front buffer reference without copying.
        Only safe to call from the render thread between flip() calls."""
        return self._front

    @property
    def shape(self) -> tuple[int, int, int]:
        return self._back.shape  # type: ignore[return-value]


# ─────────────────────────────────────────────────────────────────────────────
# Shared Display State (shared between API thread and render thread)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _StateSnapshot:
    """Immutable state snapshot used by the render loop for one frame."""
    base_frame: Optional[np.ndarray]
    detections: list[Detection]
    ai_status: AIStatus
    network_status: NetworkStatus
    tray_items: list[TrayItem]
    border_color: BorderColor
    target_border_color: BorderColor
    transition_progress: float  # 0.0 → 1.0
    scan_info: Optional[ScanInfo] = None
    flash_text: Optional[str] = None
    center_text: Optional[str] = None


class DisplayState:
    """
    Shared state between the API thread and render thread.
    All reads/writes protected by an internal RLock.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._base_frame: Optional[np.ndarray] = None
        self._detections: list[Detection] = []
        self._ai_status = AIStatus()
        self._network_status = NetworkStatus()
        self._tray_items: list[TrayItem] = []
        self._border_color = BorderColor.YELLOW
        self._target_border_color = BorderColor.YELLOW
        self._transition_progress: float = 1.0
        self._scan_info: Optional[ScanInfo] = None
        self._flash_text: Optional[str] = None
        self._flash_expires_at: float = 0.0
        self._center_text: Optional[str] = None

        self.stop_requested: bool = False
        self.actual_fps: float = 0.0

    # ── API thread: writes ────────────────────────────────────────────────────

    def update_frame(self, image_bytes: bytes, detections: list[Detection]) -> None:
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        with self._lock:
            self._base_frame = frame
            self._detections = detections

    def update_camera_frame(self, image_bytes: bytes) -> None:
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        with self._lock:
            self._base_frame = frame

    def update_detections(self, detections: list[Detection]) -> None:
        with self._lock:
            self._detections = detections

    def update_hud(
        self,
        ai_status: Optional[AIStatus],
        network_status: Optional[NetworkStatus],
        tray_items: Optional[list[TrayItem]],
        border_color: Optional[BorderColor],
        scan_info: Optional[ScanInfo] = None,
        flash_text: Optional[str] = None,
        center_text: Optional[str] = None,
    ) -> None:
        with self._lock:
            if ai_status is not None:
                self._ai_status = ai_status
            if network_status is not None:
                self._network_status = network_status
            if tray_items is not None:
                self._tray_items = tray_items
            if scan_info is not None:
                self._scan_info = scan_info
            if flash_text is not None:
                self._flash_text = flash_text
                self._flash_expires_at = time.monotonic() + 3.0
            if center_text is not None:
                self._center_text = center_text or None  # empty string clears
            if border_color is not None and border_color != self._target_border_color:
                self._border_color = _interpolated_color(
                    self._border_color, self._target_border_color, self._transition_progress
                )
                self._target_border_color = border_color
                self._transition_progress = 0.0

    # ── Render thread: reads ──────────────────────────────────────────────────

    def snapshot(self, delta_progress: float = 0.08) -> _StateSnapshot:
        with self._lock:
            self._transition_progress = min(1.0, self._transition_progress + delta_progress)
            now = time.monotonic()
            flash = self._flash_text if now < self._flash_expires_at else None
            return _StateSnapshot(
                base_frame=self._base_frame,
                detections=list(self._detections),
                ai_status=self._ai_status,
                network_status=self._network_status,
                tray_items=list(self._tray_items),
                border_color=self._border_color,
                target_border_color=self._target_border_color,
                transition_progress=self._transition_progress,
                scan_info=self._scan_info,
                flash_text=flash,
                center_text=self._center_text,
            )


# ─────────────────────────────────────────────────────────────────────────────
# Color interpolation helpers
# ─────────────────────────────────────────────────────────────────────────────

_BGR_COLORS: dict[BorderColor, tuple[int, int, int]] = {
    BorderColor.GREEN:  (80, 220, 80),
    BorderColor.YELLOW: (0, 220, 255),
    BorderColor.RED:    (30, 30, 230),
}


def _interpolated_color(
    from_color: BorderColor,
    to_color: BorderColor,
    t: float,
) -> BorderColor:
    return from_color if t < 0.5 else to_color


def get_border_bgr(snap: _StateSnapshot) -> tuple[int, int, int]:
    t = snap.transition_progress
    src = _BGR_COLORS[snap.border_color]
    dst = _BGR_COLORS[snap.target_border_color]
    return tuple(int(s + (d - s) * t) for s, d in zip(src, dst))  # type: ignore[return-value]

"""
buffer.py — 소프트웨어 더블 버퍼 + 공유 디스플레이 상태

더블 버퍼링 원리:
  ┌─────────────┐   render   ┌─────────────┐
  │  Back Buffer│ ─────────→ │ Front Buffer│ → cv2.imshow()
  │  (쓰기 전용) │  flip()    │ (읽기 전용) │
  └─────────────┘←──────────└─────────────┘
                   다음 프레임

- 렌더 스레드가 백 버퍼에 완성된 프레임을 모두 그린 뒤 flip()
- imshow()는 항상 완성된 프론트 버퍼만 표시 → 중간 상태 절대 노출 안 됨
- 테두리 색상 변경 시도 화면 찢힘(tearing) / 깜빡임 없음
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
# 더블 버퍼
# ─────────────────────────────────────────────────────────────────────────────

class DoubleBuffer:
    """
    numpy 배열 기반 소프트웨어 더블 버퍼.
    렌더 스레드: back에 그리고 flip() 호출
    디스플레이:  get_front()로 완성된 프레임 획득
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
        """렌더링 대상 백 버퍼 (렌더 스레드 전용)."""
        return self._back

    def flip(self) -> None:
        """백 ↔ 프론트 원자적 교체. 렌더 완료 후 반드시 호출."""
        with self._lock:
            self._back, self._front = self._front, self._back

    def get_front(self) -> np.ndarray:
        """프론트 버퍼 복사본 반환 (스레드 안전)."""
        with self._lock:
            return self._front.copy()

    @property
    def shape(self) -> tuple[int, int, int]:
        return self._back.shape  # type: ignore[return-value]


# ─────────────────────────────────────────────────────────────────────────────
# 공유 디스플레이 상태 (API ↔ 렌더 스레드 공유)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _StateSnapshot:
    """렌더 루프가 한 프레임에 사용하는 상태 스냅샷 (불변)."""
    base_frame: Optional[np.ndarray]
    detections: list[Detection]
    ai_status: AIStatus
    network_status: NetworkStatus
    tray_items: list[TrayItem]
    border_color: BorderColor
    target_border_color: BorderColor    # 전환 목표색
    transition_progress: float          # 0.0 → 1.0 (색상 전환 진행률)
    scan_info: Optional[ScanInfo] = None
    flash_text: Optional[str] = None


class DisplayState:
    """
    API 스레드와 렌더 스레드 사이의 공유 상태.
    모든 쓰기/읽기는 내부 RLock으로 보호.
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
        self._transition_progress: float = 1.0  # 초기엔 전환 완료 상태
        self._scan_info: Optional[ScanInfo] = None
        self._flash_text: Optional[str] = None
        self._flash_expires_at: float = 0.0

        self.stop_requested: bool = False
        self.actual_fps: float = 0.0          # 렌더 스레드가 갱신

    # ── API 스레드: 쓰기 ──────────────────────────────────────────────────────

    def update_frame(self, image_bytes: bytes, detections: list[Detection]) -> None:
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        with self._lock:
            self._base_frame = frame
            self._detections = detections

    def update_hud(
        self,
        ai_status: Optional[AIStatus],
        network_status: Optional[NetworkStatus],
        tray_items: Optional[list[TrayItem]],
        border_color: Optional[BorderColor],
        scan_info: Optional[ScanInfo] = None,
        flash_text: Optional[str] = None,
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
            if border_color is not None and border_color != self._target_border_color:
                # 새 색상으로 부드럽게 전환 시작
                self._border_color = _interpolated_color(
                    self._border_color, self._target_border_color, self._transition_progress
                )
                self._target_border_color = border_color
                self._transition_progress = 0.0

    # ── 렌더 스레드: 읽기 ────────────────────────────────────────────────────

    def snapshot(self, delta_progress: float = 0.08) -> _StateSnapshot:
        """현재 상태 스냅샷 반환 + 색상 전환 진행."""
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
            )


# ─────────────────────────────────────────────────────────────────────────────
# 색상 전환 헬퍼
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
    """
    t(0~1) 기반 보간: 0이면 from, 1이면 to 반환.
    중간값은 가장 가까운 색으로 스냅.
    (실제 RGB 보간은 HUDRenderer에서 수행)
    """
    return from_color if t < 0.5 else to_color


def get_border_bgr(snap: _StateSnapshot) -> tuple[int, int, int]:
    """전환 진행률에 따라 보간된 BGR 색상 반환."""
    t = snap.transition_progress
    src = _BGR_COLORS[snap.border_color]
    dst = _BGR_COLORS[snap.target_border_color]
    return tuple(int(s + (d - s) * t) for s, d in zip(src, dst))  # type: ignore[return-value]

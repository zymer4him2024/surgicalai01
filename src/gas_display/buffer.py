"""Double buffer and shared display state for Gas Display Agent."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

from src.gas_display.schemas import GasBorderColor


# ─────────────────────────────────────────────────────────────────────────────
# Double Buffer
# ─────────────────────────────────────────────────────────────────────────────

class DoubleBuffer:
    """Software double buffer backed by numpy arrays.
    Render thread writes to back, calls flip(). Display reads front."""

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
        return self._front


# ─────────────────────────────────────────────────────────────────────────────
# State snapshot (immutable, read by render thread)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GasStateSnapshot:
    total_count: int
    state: str  # "COUNTING" or "LOW_STOCK"
    location: str
    operator_id: str
    border_color: GasBorderColor
    ai_fps: float
    npu_temp_celsius: Optional[float]
    cpu_temp_celsius: Optional[float]
    thermal_status: str
    inference_ready: bool
    timestamp: str


# ─────────────────────────────────────────────────────────────────────────────
# Shared display state (API thread writes, render thread reads)
# ─────────────────────────────────────────────────────────────────────────────

class GasDisplayState:
    """Thread-safe state shared between API and render threads.
    All fields protected by _lock (RLock)."""

    def __init__(self) -> None:
        # _lock protects all fields below
        self._lock = threading.RLock()
        self._total_count: int = 0
        self._state: str = "COUNTING"
        self._location: str = ""
        self._operator_id: str = ""
        self._border_color: GasBorderColor = GasBorderColor.GREEN
        self._ai_fps: float = 0.0
        self._npu_temp: Optional[float] = None
        self._cpu_temp: Optional[float] = None
        self._thermal_status: str = "normal"
        self._inference_ready: bool = True
        self._timestamp: str = ""

        self.stop_requested: bool = False
        self.actual_fps: float = 0.0

    def update_hud(
        self,
        total_count: Optional[int] = None,
        state: Optional[str] = None,
        location: Optional[str] = None,
        operator_id: Optional[str] = None,
        ai_fps: Optional[float] = None,
        npu_temp_celsius: Optional[float] = None,
        cpu_temp_celsius: Optional[float] = None,
        thermal_status: Optional[str] = None,
        inference_ready: Optional[bool] = None,
    ) -> None:
        with self._lock:
            if total_count is not None:
                self._total_count = total_count
            if state is not None:
                self._state = state
                self._border_color = (
                    GasBorderColor.RED if state == "LOW_STOCK"
                    else GasBorderColor.GREEN
                )
            if location is not None:
                self._location = location
            if operator_id is not None:
                self._operator_id = operator_id
            if ai_fps is not None:
                self._ai_fps = ai_fps
            if npu_temp_celsius is not None:
                self._npu_temp = npu_temp_celsius
            if cpu_temp_celsius is not None:
                self._cpu_temp = cpu_temp_celsius
            if thermal_status is not None:
                self._thermal_status = thermal_status
            if inference_ready is not None:
                self._inference_ready = inference_ready
            self._timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

    def snapshot(self) -> GasStateSnapshot:
        with self._lock:
            return GasStateSnapshot(
                total_count=self._total_count,
                state=self._state,
                location=self._location,
                operator_id=self._operator_id,
                border_color=self._border_color,
                ai_fps=self._ai_fps,
                npu_temp_celsius=self._npu_temp,
                cpu_temp_celsius=self._cpu_temp,
                thermal_status=self._thermal_status,
                inference_ready=self._inference_ready,
                timestamp=self._timestamp or time.strftime("%Y-%m-%d %H:%M:%S"),
            )

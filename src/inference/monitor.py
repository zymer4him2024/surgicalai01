"""
monitor.py — NPU temperature monitoring thread

Reads Hailo-8 temperature via sysfs:
  /sys/class/hailo_chardev/hailo0/device_temperature
Returns None in simulation mode (file not found).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

_SYSFS_TEMP_PATH = "/sys/class/hailo_chardev/hailo0/device_temperature"
_POLL_INTERVAL = 5.0


class NPUTemperatureMonitor:

    def __init__(
        self,
        warning_threshold: float = 85.0,
        critical_threshold: float = 95.0,
        poll_interval: float = _POLL_INTERVAL,
    ) -> None:
        self.warning_threshold = warning_threshold
        self.critical_threshold = critical_threshold
        self.poll_interval = poll_interval

        self._temp: Optional[float] = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._poll_loop, name="npu-temp-monitor", daemon=True
        )

    def start(self) -> None:
        self._thread.start()
        logger.info("NPU temperature monitor started (interval=%.1fs)", self.poll_interval)

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=self.poll_interval + 1)
        logger.info("NPU temperature monitor stopped")

    @property
    def current_temp(self) -> Optional[float]:
        with self._lock:
            return self._temp

    @property
    def is_warning(self) -> bool:
        temp = self.current_temp
        return temp is not None and temp >= self.warning_threshold

    @property
    def is_critical(self) -> bool:
        temp = self.current_temp
        return temp is not None and temp >= self.critical_threshold

    def warning_message(self) -> Optional[str]:
        temp = self.current_temp
        if temp is None:
            return None
        if temp >= self.critical_threshold:
            return (
                f"CRITICAL: NPU temperature {temp:.1f}°C exceeds "
                f"critical threshold {self.critical_threshold}°C. "
                "Inference suspended to protect hardware."
            )
        if temp >= self.warning_threshold:
            return (
                f"WARNING: NPU temperature {temp:.1f}°C exceeds "
                f"warning threshold {self.warning_threshold}°C. "
                "Consider reducing workload."
            )
        return None

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            temp = self._read_temperature()
            with self._lock:
                self._temp = temp
            if temp is not None:
                self._log_if_threshold(temp)
            self._stop_event.wait(timeout=self.poll_interval)

    def _read_temperature(self) -> Optional[float]:
        return self._read_via_sysfs()

    @staticmethod
    def _read_via_sysfs() -> Optional[float]:
        try:
            with open(_SYSFS_TEMP_PATH) as f:
                raw = f.read().strip()
            value = float(raw)
            return value / 1000.0 if value > 1000 else value
        except (FileNotFoundError, ValueError, OSError):
            return None

    def _log_if_threshold(self, temp: float) -> None:
        if temp >= self.critical_threshold:
            logger.critical(
                "NPU CRITICAL OVERHEAT: %.1f°C (threshold=%.1f°C)",
                temp,
                self.critical_threshold,
            )
        elif temp >= self.warning_threshold:
            logger.warning(
                "NPU overheat warning: %.1f°C (threshold=%.1f°C)",
                temp,
                self.warning_threshold,
            )
        else:
            logger.debug("NPU temperature: %.1f°C", temp)

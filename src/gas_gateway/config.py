"""Startup configuration for Gas Gateway Agent."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass

_VALID_APP_IDS = {"surgical", "od", "inventory", "inventory_count"}
_DEVICE_ID_RE = re.compile(r"^[A-Z]{2}-[A-Za-z0-9]+-\d{3}$")


@dataclass(frozen=True)
class GasConfig:
    """Immutable configuration loaded once at startup from environment."""
    app_id: str
    device_id: str
    inference_url: str
    inference_endpoint: str
    camera_url: str
    display_url: str
    firebase_sync_url: str
    gateway_timeout: float
    health_timeout: float
    low_stock_threshold: int
    sync_interval_sec: float
    customer_db_url: str
    location_name: str
    operator_id: str
    gateway_api_key: str


def load_config() -> GasConfig:
    """Read and validate all environment variables. Exits on invalid config."""
    errors: list[str] = []

    app_id = os.getenv("APP_ID", "")
    if app_id not in _VALID_APP_IDS:
        errors.append(f"APP_ID={app_id!r} must be one of {_VALID_APP_IDS}")

    device_id = os.getenv("DEVICE_ID", "")
    if not _DEVICE_ID_RE.match(device_id):
        errors.append(f"DEVICE_ID={device_id!r} must match pattern XX-Name-NNN")

    low_stock = int(os.getenv("LOW_STOCK_THRESHOLD", "5"))
    if low_stock < 0:
        errors.append(f"LOW_STOCK_THRESHOLD={low_stock} must be >= 0")

    sync_interval = float(os.getenv("SYNC_INTERVAL_SEC", "60"))
    if sync_interval < 10:
        errors.append(f"SYNC_INTERVAL_SEC={sync_interval} must be >= 10")

    if errors:
        for e in errors:
            print(f"FATAL CONFIG: {e}", file=sys.stderr)
        sys.exit(1)

    return GasConfig(
        app_id=app_id,
        device_id=device_id,
        inference_url=os.getenv("INFERENCE_URL", "http://gas_inference_agent:8001"),
        inference_endpoint=os.getenv("INFERENCE_ENDPOINT", "/inference"),
        camera_url=os.getenv("CAMERA_URL", "http://gas_camera_agent:8002"),
        display_url=os.getenv("DISPLAY_URL", "http://gas_display_agent:8013"),
        firebase_sync_url=os.getenv("FIREBASE_SYNC_URL", "http://gas_firebase_sync_agent:8004"),
        gateway_timeout=float(os.getenv("GATEWAY_TIMEOUT_SEC", "15")),
        health_timeout=3.0,
        low_stock_threshold=low_stock,
        sync_interval_sec=sync_interval,
        customer_db_url=os.getenv("CUSTOMER_DB_URL", ""),
        location_name=os.getenv("LOCATION_NAME", ""),
        operator_id=os.getenv("OPERATOR_ID", ""),
        gateway_api_key=os.getenv("GATEWAY_API_KEY", ""),
    )

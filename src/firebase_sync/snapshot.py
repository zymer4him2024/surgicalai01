"""
snapshot.py — 3-shot snapshot capture module

Captures 3 frames at 0.1s intervals on mismatch events.
Different exposure values applied to each shot for best post-processing options.

Exposure strategy:
  Shot 1 — standard  (x1.0)
  Shot 2 — under     (x0.65) — prevent highlight clipping
  Shot 3 — over      (x1.45) — recover shadow detail

Source:
  DISPLAY_URL/snapshot endpoint (display_agent)
  Falls back to a simulation image if unavailable.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
from datetime import datetime

import cv2
import httpx
import numpy as np

logger = logging.getLogger("firebase_sync.snapshot")

DISPLAY_URL = os.getenv("DISPLAY_URL", "http://display_agent:8003")
SNAPSHOT_INTERVAL = 0.1
JPEG_QUALITY = 90

EXPOSURE_MULTIPLIERS = [1.0, 0.65, 1.45]
EXPOSURE_LABELS = ["standard", "underexposed", "overexposed"]


async def capture_snapshots(
    http_client: httpx.AsyncClient,
) -> list[dict]:
    """
    Capture 3 snapshots at 0.1s intervals.

    Returns:
        list of {
            "shot": int (1-3),
            "label": str,
            "timestamp": str,
            "jpeg_bytes": bytes,
        }
    """
    shots: list[dict] = []

    for i, (multiplier, label) in enumerate(
        zip(EXPOSURE_MULTIPLIERS, EXPOSURE_LABELS)
    ):
        if i > 0:
            await asyncio.sleep(SNAPSHOT_INTERVAL)

        timestamp = datetime.utcnow().isoformat()
        raw_jpeg = await _fetch_frame(http_client)
        adjusted = _apply_exposure(raw_jpeg, multiplier)

        shots.append({
            "shot": i + 1,
            "label": label,
            "exposure_multiplier": multiplier,
            "timestamp": timestamp,
            "jpeg_bytes": adjusted,
        })
        logger.info(
            "Shot %d/%d captured — %s (x%.2f), size=%dB",
            i + 1, len(EXPOSURE_MULTIPLIERS), label, multiplier, len(adjusted),
        )

    return shots


async def _fetch_frame(client: httpx.AsyncClient) -> bytes:
    try:
        resp = await client.get(f"{DISPLAY_URL}/snapshot", timeout=5.0)
        if resp.status_code == 200 and resp.content:
            return resp.content
    except Exception as exc:
        logger.warning("Snapshot fetch failed (%s) — using simulation image", exc)

    return _make_simulation_frame()


def _apply_exposure(jpeg_bytes: bytes, multiplier: float) -> bytes:
    arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return jpeg_bytes

    adjusted = np.clip(img.astype(np.float32) * multiplier, 0, 255).astype(np.uint8)

    ok, buf = cv2.imencode(".jpg", adjusted, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    return buf.tobytes() if ok else jpeg_bytes


def _make_simulation_frame() -> bytes:
    img = np.zeros((1080, 1920, 3), dtype=np.uint8)
    img[:] = (30, 30, 30)

    cv2.putText(
        img,
        "SIMULATION SNAPSHOT",
        (760, 520),
        cv2.FONT_HERSHEY_DUPLEX,
        1.5,
        (0, 200, 255),
        2,
        cv2.LINE_AA,
    )
    cv2.putText(
        img,
        datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        (800, 580),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (150, 150, 150),
        1,
        cv2.LINE_AA,
    )

    _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    return buf.tobytes()

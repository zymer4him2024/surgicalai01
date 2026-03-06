"""
snapshot.py — 스냅샷 3장 캡처 모듈

불일치 이벤트 발생 시 0.1초 간격으로 3장 촬영.
각 장에 다른 노출값을 적용해 후처리 시 최적 이미지 선택 가능.

노출 전략:
  Shot 1 — 표준 노출 (×1.0)
  Shot 2 — 언더 노출 (×0.65) → 하이라이트 클리핑 방지
  Shot 3 — 오버 노출  (×1.45) → 어두운 영역 디테일 확보

소스:
  DISPLAY_URL/snapshot 엔드포인트 (display_agent)
  카메라 없을 때 테스트 이미지 생성(시뮬레이션 모드 fallback)
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
SNAPSHOT_INTERVAL = 0.1         # 촬영 간격 (초)
JPEG_QUALITY = 90

# 노출 배율 (shot 1, 2, 3 순서)
EXPOSURE_MULTIPLIERS = [1.0, 0.65, 1.45]
EXPOSURE_LABELS = ["standard", "underexposed", "overexposed"]


async def capture_snapshots(
    http_client: httpx.AsyncClient,
) -> list[dict]:
    """
    0.1초 간격으로 스냅샷 3장 캡처.

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
            "Shot %d/%d captured — %s (×%.2f), size=%dB",
            i + 1, len(EXPOSURE_MULTIPLIERS), label, multiplier, len(adjusted),
        )

    return shots


async def _fetch_frame(client: httpx.AsyncClient) -> bytes:
    """display_agent /snapshot 호출. 실패 시 시뮬레이션 이미지 반환."""
    try:
        resp = await client.get(f"{DISPLAY_URL}/snapshot", timeout=5.0)
        if resp.status_code == 200 and resp.content:
            return resp.content
    except Exception as exc:
        logger.warning("Snapshot fetch failed (%s) — using simulation image", exc)

    return _make_simulation_frame()


def _apply_exposure(jpeg_bytes: bytes, multiplier: float) -> bytes:
    """JPEG 바이트에 노출 배율 적용 후 JPEG 재인코딩."""
    arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return jpeg_bytes

    # 노출 조정: 클리핑 방지를 위해 float32 변환 후 적용
    adjusted = np.clip(img.astype(np.float32) * multiplier, 0, 255).astype(np.uint8)

    # JPEG 재인코딩
    ok, buf = cv2.imencode(".jpg", adjusted, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    return buf.tobytes() if ok else jpeg_bytes


def _make_simulation_frame() -> bytes:
    """카메라/display 없을 때 사용하는 시뮬레이션 이미지."""
    img = np.zeros((1080, 1920, 3), dtype=np.uint8)
    img[:] = (30, 30, 30)

    # 시뮬레이션 텍스트
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

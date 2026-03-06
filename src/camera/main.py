"""
main.py — Camera Agent (Module: CameraAgent)

USB 카메라 프레임 캡처 담당.

엔드포인트:
  GET /health  — 카메라 상태 반환
  GET /frame   — 최신 JPEG 프레임 반환 (image/jpeg)
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import cv2
import uvicorn
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import Response

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("camera.main")

CAMERA_INDEX = int(os.getenv("CAMERA_INDEX", "0"))
CAMERA_WIDTH = int(os.getenv("CAMERA_WIDTH", "3840"))   # 4K — 카메라가 지원하는 최대값으로 자동 조정
CAMERA_HEIGHT = int(os.getenv("CAMERA_HEIGHT", "2160"))
CAMERA_FPS = int(os.getenv("CAMERA_FPS", "30"))
MODULE_NAME = os.getenv("MODULE_NAME", "CameraAgent")

_cap: cv2.VideoCapture | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global _cap
    _cap = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_V4L2)
    if _cap.isOpened():
        # 최대 해상도 요청 (카메라가 지원하는 최대값으로 자동 조정됨)
        _cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
        _cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
        _cap.set(cv2.CAP_PROP_FPS, CAMERA_FPS)
        # 항상 가장 최신 프레임 반환 (버퍼 1장)
        _cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        # 자동 초점 활성화
        _cap.set(cv2.CAP_PROP_AUTOFOCUS, 1)

        actual_w = int(_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = _cap.get(cv2.CAP_PROP_FPS)
        logger.info(
            "Camera %d opened — requested=%dx%d actual=%dx%d fps=%.0f",
            CAMERA_INDEX, CAMERA_WIDTH, CAMERA_HEIGHT, actual_w, actual_h, actual_fps
        )
    else:
        logger.warning("Camera %d not available — /frame will return 503", CAMERA_INDEX)
    yield
    if _cap is not None:
        _cap.release()
    logger.info("Camera released")


app = FastAPI(title="Camera Agent API", lifespan=lifespan)


@app.get("/health")
def health_check():
    available = _cap is not None and _cap.isOpened()
    actual_w = int(_cap.get(cv2.CAP_PROP_FRAME_WIDTH)) if available else 0
    actual_h = int(_cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) if available else 0
    return {
        "status": "healthy" if available else "degraded",
        "module": MODULE_NAME,
        "camera_index": CAMERA_INDEX,
        "camera_available": available,
        "resolution": f"{actual_w}x{actual_h}",
    }


@app.get("/frame", summary="최신 카메라 프레임 반환 (JPEG)")
def get_frame() -> Response:
    if _cap is None or not _cap.isOpened():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Camera not available",
        )
    ok, frame = _cap.read()
    if not ok or frame is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to capture frame",
        )
    # 고품질 JPEG 인코딩 (추론 정확도 향상)
    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
    return Response(content=buf.tobytes(), media_type="image/jpeg")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8002)

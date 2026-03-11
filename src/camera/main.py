"""
main.py — Camera Agent

Handles USB camera frame capture.

Endpoints:
  GET /health  — camera status
  GET /frame   — latest JPEG frame (image/jpeg)
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
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
CAMERA_WIDTH = int(os.getenv("CAMERA_WIDTH", "1920"))
CAMERA_HEIGHT = int(os.getenv("CAMERA_HEIGHT", "1080"))
CAMERA_FPS = int(os.getenv("CAMERA_FPS", "30"))
MODULE_NAME = os.getenv("MODULE_NAME", "CameraAgent")

_cap: cv2.VideoCapture | None = None
_latest_jpg: bytes | None = None
_lock = threading.Lock()
_stop_event = threading.Event()
_capture_thread: threading.Thread | None = None

def _capture_loop():
    global _latest_jpg
    logger.info("Camera capture loop started")
    while not _stop_event.is_set():
        if _cap is None or not _cap.isOpened():
            time.sleep(0.5)
            continue
        ok, frame = _cap.read()
        if not ok or frame is None:
            time.sleep(0.01)
            continue
        
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        with _lock:
            _latest_jpg = buf.tobytes()

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global _cap, _capture_thread
    _cap = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_V4L2)
    if _cap.isOpened():
        _cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
        _cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
        _cap.set(cv2.CAP_PROP_FPS, CAMERA_FPS)
        _cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        _cap.set(cv2.CAP_PROP_AUTOFOCUS, 1)

        actual_w = int(_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = _cap.get(cv2.CAP_PROP_FPS)
        logger.info(
            "Camera %d opened — requested=%dx%d actual=%dx%d fps=%.0f",
            CAMERA_INDEX, CAMERA_WIDTH, CAMERA_HEIGHT, actual_w, actual_h, actual_fps
        )
        _stop_event.clear()
        _capture_thread = threading.Thread(target=_capture_loop, daemon=True)
        _capture_thread.start()
    else:
        logger.warning("Camera %d not available — /frame will return 503", CAMERA_INDEX)
    yield
    _stop_event.set()
    if _capture_thread:
        _capture_thread.join(timeout=2.0)
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
        "has_frame": _latest_jpg is not None,
    }


@app.get("/frame", summary="Latest camera frame (JPEG)")
def get_frame() -> Response:
    with _lock:
        jpg_data = _latest_jpg
    if jpg_data is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Frame not yet available or camera failed",
        )
    return Response(content=jpg_data, media_type="image/jpeg")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8002)

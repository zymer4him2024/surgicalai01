"""
main.py — Display Agent

Architecture:
  FastAPI Thread (main, uvicorn asyncio loop)
  ├─ POST /frame    → receive camera frame + detection results
  ├─ POST /hud      → update AI/Network/Tray/BorderColor
  ├─ GET  /health   → render FPS, resolution
  └─ GET  /snapshot → current screen as JPEG (web preview)

  Render Thread (background)
  ├─ DisplayState snapshot → render to back buffer → flip() → imshow()
  ├─ 30 FPS target (time.sleep controlled)
  └─ HEADLESS=true skips imshow (Mac simulation)

Double buffer: full frame composited in back buffer, then flip() — zero flicker.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import cv2
import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse, Response

from src.display.buffer import DisplayState, DoubleBuffer
from src.display.hud import HUDRenderer
from src.display.schemas import (
    FrameUpdate,
    HealthResponse,
    HUDUpdate,
)

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("display.main")

MODULE_NAME = os.getenv("MODULE_NAME", "DisplayAgent")
DISPLAY_W = int(os.getenv("DISPLAY_WIDTH", "1920"))
DISPLAY_H = int(os.getenv("DISPLAY_HEIGHT", "1080"))
TARGET_FPS = int(os.getenv("DISPLAY_FPS", "30"))
WINDOW_NAME = "SurgicalAI"

HEADLESS: bool = (
    os.getenv("DISPLAY_HEADLESS", "false").lower() == "true"
    or not os.environ.get("DISPLAY")
)

# ─────────────────────────────────────────────────────────────────────────────
# Global instances
# ─────────────────────────────────────────────────────────────────────────────

_state = DisplayState()
_buffer = DoubleBuffer(DISPLAY_W, DISPLAY_H)
_hud = HUDRenderer()
_render_thread: threading.Thread | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Render loop (background thread)
# ─────────────────────────────────────────────────────────────────────────────

def _render_loop() -> None:
    frame_interval = 1.0 / TARGET_FPS
    fps_acc_start = time.monotonic()
    fps_frame_count = 0

    if not HEADLESS:
        cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
        cv2.setWindowProperty(
            WINDOW_NAME, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN
        )
        logger.info(
            "OpenCV window opened (%dx%d, FULLSCREEN)", DISPLAY_W, DISPLAY_H
        )
    else:
        logger.info(
            "Headless mode — rendering to buffer only (no display output)"
        )

    _consecutive_errors = 0

    while not _state.stop_requested:
        t0 = time.monotonic()

        try:
            snap = _state.snapshot()

            canvas = _buffer.back
            _hud.render(canvas, snap)

            _buffer.flip()

            if not HEADLESS:
                front = _buffer.get_front()
                cv2.imshow(WINDOW_NAME, front)
                key = cv2.waitKey(1) & 0xFF
                if key == 27:  # ESC
                    _state.stop_requested = True
                    break

            _consecutive_errors = 0

        except Exception:
            _consecutive_errors += 1
            logger.exception(
                "Render loop error (#%d)", _consecutive_errors
            )
            if _consecutive_errors >= 30:
                logger.critical(
                    "Render loop: %d consecutive errors — stopping",
                    _consecutive_errors,
                )
                break
            time.sleep(0.1)
            continue

        fps_frame_count += 1
        elapsed_acc = time.monotonic() - fps_acc_start
        if elapsed_acc >= 1.0:
            _state.actual_fps = fps_frame_count / elapsed_acc
            fps_frame_count = 0
            fps_acc_start = time.monotonic()

        elapsed = time.monotonic() - t0
        sleep_time = frame_interval - elapsed
        if sleep_time > 0:
            time.sleep(sleep_time)

    if not HEADLESS:
        cv2.destroyAllWindows()
    logger.info("Render loop exited")


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI lifespan
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global _render_thread

    _render_thread = threading.Thread(
        target=_render_loop, name="render-loop", daemon=True
    )
    _render_thread.start()
    logger.info(
        "Display Agent started — headless=%s, resolution=%dx%d, target_fps=%d",
        HEADLESS, DISPLAY_W, DISPLAY_H, TARGET_FPS,
    )

    yield

    _state.stop_requested = True
    if _render_thread:
        _render_thread.join(timeout=3.0)
    logger.info("Display Agent shutdown complete")


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Display Agent API",
    description="HDMI display output and HUD control service",
    version="1.0.0",
    lifespan=lifespan,
)


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/frame", summary="Update camera frame")
async def update_frame(body: FrameUpdate) -> JSONResponse:
    try:
        image_bytes = base64.b64decode(body.image_b64)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid base64 image data",
        )
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, _state.update_frame, image_bytes, body.detections
    )
    return JSONResponse({"status": "ok", "detections": len(body.detections)})


@app.post("/hud", summary="Update HUD data")
async def update_hud(body: HUDUpdate) -> JSONResponse:
    _state.update_hud(
        ai_status=body.ai_status,
        network_status=body.network_status,
        tray_items=body.tray_items,
        border_color=body.border_color,
        scan_info=body.scan_info,
        flash_text=body.flash_text,
    )
    return JSONResponse({"status": "ok"})


@app.get("/health", response_model=HealthResponse, summary="Health check")
async def health_check() -> HealthResponse:
    alive = _render_thread is not None and _render_thread.is_alive()
    return HealthResponse(
        status="healthy" if alive else "degraded",
        module=MODULE_NAME,
        headless=HEADLESS,
        render_fps=round(_state.actual_fps, 1),
        display_resolution=f"{DISPLAY_W}x{DISPLAY_H}",
    )


@app.get(
    "/snapshot",
    summary="Current screen snapshot (JPEG)",
    responses={200: {"content": {"image/jpeg": {}}}},
)
async def snapshot() -> Response:
    loop = asyncio.get_event_loop()
    jpg_bytes = await loop.run_in_executor(None, _encode_snapshot)
    return Response(content=jpg_bytes, media_type="image/jpeg")


def _encode_snapshot() -> bytes:
    frame = _buffer.get_front()
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes() if ok else b""


# ─────────────────────────────────────────────────────────────────────────────
# Global exception handler
# ─────────────────────────────────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "type": type(exc).__name__},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "src.display.main:app",
        host="0.0.0.0",
        port=8003,
        workers=1,
        log_level="info",
    )

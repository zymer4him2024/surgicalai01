"""Gas Display Agent — Info-only HUD for gas cylinder inventory.

Routing:
  POST /hud      → update inventory count and status
  GET  /health   → render FPS, resolution
  GET  /snapshot → current screen as JPEG (web preview)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import cv2
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response

from src.gas_display.buffer import DoubleBuffer, GasDisplayState
from src.gas_display.hud import GasHUDRenderer
from src.gas_display.schemas import GasHealthResponse, GasHUDUpdate

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("gas_display.main")

MODULE_NAME = os.getenv("MODULE_NAME", "GasDisplayAgent")
DISPLAY_W = int(os.getenv("DISPLAY_WIDTH", "1920"))
DISPLAY_H = int(os.getenv("DISPLAY_HEIGHT", "1080"))
TARGET_FPS = int(os.getenv("DISPLAY_FPS", "15"))
WINDOW_NAME = "GasInventory"

HEADLESS: bool = (
    os.getenv("DISPLAY_HEADLESS", "false").lower() == "true"
    or not os.environ.get("DISPLAY")
)

# ─────────────────────────────────────────────────────────────────────────────
# Global instances
# ─────────────────────────────────────────────────────────────────────────────

_state = GasDisplayState()
_buffer = DoubleBuffer(DISPLAY_W, DISPLAY_H)
_hud = GasHUDRenderer()
_render_thread: threading.Thread | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Render loop (background thread)
# ─────────────────────────────────────────────────────────────────────────────

def _render_loop() -> None:
    frame_interval = 1.0 / TARGET_FPS
    fps_start = time.monotonic()
    fps_count = 0

    if not HEADLESS:
        cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
        cv2.setWindowProperty(
            WINDOW_NAME, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN,
        )
        logger.info("OpenCV window opened (%dx%d, FULLSCREEN)", DISPLAY_W, DISPLAY_H)
    else:
        logger.info("Headless mode — rendering to buffer only")

    consecutive_errors = 0

    while not _state.stop_requested:
        t0 = time.monotonic()

        try:
            snap = _state.snapshot()
            _hud.render(_buffer.back, snap)
            _buffer.flip()

            if not HEADLESS:
                cv2.imshow(WINDOW_NAME, _buffer.peek_front())
                if cv2.waitKey(1) & 0xFF == 27:
                    _state.stop_requested = True
                    break

            consecutive_errors = 0

        except Exception:
            consecutive_errors += 1
            logger.exception("Render loop error (#%d)", consecutive_errors)
            if consecutive_errors >= 30:
                logger.critical("Render loop: %d errors — stopping", consecutive_errors)
                break
            time.sleep(0.1)
            continue

        fps_count += 1
        fps_elapsed = time.monotonic() - fps_start
        if fps_elapsed >= 1.0:
            _state.actual_fps = fps_count / fps_elapsed
            fps_count = 0
            fps_start = time.monotonic()

        elapsed = time.monotonic() - t0
        if (sleep_t := frame_interval - elapsed) > 0:
            time.sleep(sleep_t)

    if not HEADLESS:
        cv2.destroyAllWindows()
    logger.info("Render loop exited")


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global _render_thread
    _render_thread = threading.Thread(
        target=_render_loop, name="gas-render", daemon=True,
    )
    _render_thread.start()
    logger.info(
        "Gas Display started — headless=%s, %dx%d, %d fps",
        HEADLESS, DISPLAY_W, DISPLAY_H, TARGET_FPS,
    )
    yield
    _state.stop_requested = True
    if _render_thread:
        _render_thread.join(timeout=3.0)
    logger.info("Gas Display shutdown complete")


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Gas Display Agent API",
    description="HDMI info panel for gas cylinder inventory",
    version="1.0.0",
    lifespan=lifespan,
)


@app.post("/hud", summary="Update gas HUD")
async def update_hud(body: GasHUDUpdate) -> JSONResponse:
    _state.update_hud(
        total_count=body.total_count,
        state=body.state,
        location=body.location,
        operator_id=body.operator_id,
        ai_fps=body.ai_fps,
        npu_temp_celsius=body.npu_temp_celsius,
        cpu_temp_celsius=body.cpu_temp_celsius,
        thermal_status=body.thermal_status,
        inference_ready=body.inference_ready,
    )
    return JSONResponse({"status": "ok"})


@app.get("/health", response_model=GasHealthResponse)
async def health_check() -> GasHealthResponse:
    alive = _render_thread is not None and _render_thread.is_alive()
    return GasHealthResponse(
        status="healthy" if alive else "degraded",
        module=MODULE_NAME,
        headless=HEADLESS,
        render_fps=round(_state.actual_fps, 1),
        display_resolution=f"{DISPLAY_W}x{DISPLAY_H}",
    )


@app.get(
    "/snapshot",
    summary="Current screen as JPEG",
    responses={200: {"content": {"image/jpeg": {}}}},
)
async def snapshot() -> Response:
    frame = _buffer.get_front()
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return Response(content=buf.tobytes() if ok else b"", media_type="image/jpeg")


@app.exception_handler(Exception)
async def global_exception_handler(request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "type": type(exc).__name__},
    )


if __name__ == "__main__":
    uvicorn.run(
        "src.gas_display.main:app",
        host="0.0.0.0",
        port=8013,
        workers=1,
        log_level="info",
    )

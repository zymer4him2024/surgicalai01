"""
main.py — Display Agent

아키텍처:
  ┌──────────────────────────────────────────────────────────────────┐
  │  FastAPI Thread (메인, uvicorn asyncio loop)                     │
  │  ├─ POST /frame       → 카메라 프레임 + 탐지 결과 수신           │
  │  ├─ POST /hud         → AI/Network/Tray/BorderColor 갱신         │
  │  ├─ GET  /health      → 렌더 FPS, 해상도 반환                    │
  │  └─ GET  /snapshot    → 현재 화면 JPEG 반환 (웹 미리보기용)      │
  └──────────────────────────────────────────────────────────────────┘
  ┌──────────────────────────────────────────────────────────────────┐
  │  Render Thread (백그라운드)                                       │
  │  ├─ DisplayState 스냅샷 → 백 버퍼 렌더 → flip() → imshow()       │
  │  ├─ 30 FPS 타겟 (time.sleep 제어)                                │
  │  └─ HEADLESS=true 시 imshow 생략 (Mac 시뮬레이션)               │
  └──────────────────────────────────────────────────────────────────┘

더블 버퍼: 백 버퍼에 완전한 프레임 합성 후 flip() → 화면 깜빡임 Zero
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
# 설정
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

# DISPLAY 환경변수 없거나 명시적 헤드리스 모드 → 창 없이 버퍼만 렌더
HEADLESS: bool = (
    os.getenv("DISPLAY_HEADLESS", "false").lower() == "true"
    or not os.environ.get("DISPLAY")
)

# ─────────────────────────────────────────────────────────────────────────────
# 전역 인스턴스
# ─────────────────────────────────────────────────────────────────────────────

_state = DisplayState()
_buffer = DoubleBuffer(DISPLAY_W, DISPLAY_H)
_hud = HUDRenderer()
_render_thread: threading.Thread | None = None


# ─────────────────────────────────────────────────────────────────────────────
# 렌더 루프 (백그라운드 스레드)
# ─────────────────────────────────────────────────────────────────────────────

def _render_loop() -> None:
    """
    백 버퍼에 완성된 프레임을 렌더하고 flip() 후 표시.
    HEADLESS 모드에서는 imshow 생략 — /snapshot API로 프레임 제공.
    """
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

    while not _state.stop_requested:
        t0 = time.monotonic()

        # ── 1) 상태 스냅샷 (락 최소 보유) ────────────────────────────────────
        snap = _state.snapshot()

        # ── 2) 백 버퍼에 전체 씬 렌더링 ──────────────────────────────────────
        canvas = _buffer.back
        _hud.render(canvas, snap)          # 베이스프레임 + 박스 + HUD + 테두리

        # ── 3) 더블 버퍼 교체 ────────────────────────────────────────────────
        _buffer.flip()

        # ── 4) 화면 출력 (HEADLESS 아닐 때만) ────────────────────────────────
        if not HEADLESS:
            front = _buffer.get_front()
            cv2.imshow(WINDOW_NAME, front)
            key = cv2.waitKey(1) & 0xFF
            if key == 27:   # ESC
                _state.stop_requested = True
                break

        # ── 5) FPS 계산 ───────────────────────────────────────────────────────
        fps_frame_count += 1
        elapsed_acc = time.monotonic() - fps_acc_start
        if elapsed_acc >= 1.0:
            _state.actual_fps = fps_frame_count / elapsed_acc
            fps_frame_count = 0
            fps_acc_start = time.monotonic()

        # ── 6) 프레임 속도 제어 ───────────────────────────────────────────────
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
# FastAPI 앱
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Display Agent API",
    description="HDMI 디스플레이 출력 및 HUD 제어 서비스",
    version="1.0.0",
    lifespan=lifespan,
)


# ─────────────────────────────────────────────────────────────────────────────
# 엔드포인트
# ─────────────────────────────────────────────────────────────────────────────

@app.post(
    "/frame",
    summary="카메라 프레임 갱신",
    description=(
        "base64 인코딩된 JPEG 이미지와 탐지 결과를 받아 디스플레이를 갱신합니다.\n\n"
        "렌더 루프가 다음 사이클에서 반영합니다 (최대 1프레임 지연)."
    ),
)
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


@app.post(
    "/hud",
    summary="HUD 데이터 갱신",
    description=(
        "AI 상태, 네트워크 상태, 트레이 정보, 테두리 색상을 부분 갱신합니다.\n\n"
        "`null` 필드는 현재 값을 유지합니다.\n\n"
        "테두리 색상 변경 시 부드러운 보간 전환이 적용됩니다."
    ),
)
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


@app.get(
    "/health",
    response_model=HealthResponse,
    summary="헬스 체크",
)
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
    summary="현재 화면 스냅샷 (JPEG)",
    description=(
        "현재 렌더링된 프론트 버퍼를 JPEG으로 반환합니다.\n\n"
        "Firebase 웹 UI나 디버깅 목적으로 사용합니다."
    ),
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
# 전역 예외 핸들러
# ─────────────────────────────────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "type": type(exc).__name__},
    )


# ─────────────────────────────────────────────────────────────────────────────
# 엔트리포인트
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "src.display.main:app",
        host="0.0.0.0",
        port=8003,
        workers=1,      # 상태 공유 → 단일 워커 필수
        log_level="info",
    )

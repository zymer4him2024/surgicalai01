"""
main.py — Inference Agent FastAPI 서버

아키텍처:
  ┌──────────────────────────────────────────────────────────┐
  │  FastAPI Process (메인)                                   │
  │  ├─ POST /inference ─→ request_queue ─→ [InferWorker]    │
  │  │                  ←─ response_queue ←                  │
  │  ├─ GET  /health                                         │
  │  ├─ GET  /metrics                                        │
  │  └─ Thread: NPUTemperatureMonitor (5초 주기 폴링)        │
  └──────────────────────────────────────────────────────────┘
  ┌──────────────────────────────────────────────────────────┐
  │  InferenceWorker Process (별도)                          │
  │  └─ Hailo-8 SDK 단독 점유 / 메모리 누수 격리             │
  └──────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI, File, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse

from src.inference.monitor import NPUTemperatureMonitor
from src.inference.runner import inference_worker
from src.inference.schemas import (
    Detection,
    HealthResponse,
    MetricsResponse,
    PredictResponse,
    ThermalStatus,
)

# ─────────────────────────────────────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("inference.main")

HEF_PATH = os.getenv("HEF_PATH", "/app/models/yolov11.hef")
TEMP_WARNING = float(os.getenv("TEMP_WARNING_CELSIUS", "85.0"))
TEMP_CRITICAL = float(os.getenv("TEMP_CRITICAL_CELSIUS", "95.0"))
INFERENCE_TIMEOUT = float(os.getenv("INFERENCE_TIMEOUT_SEC", "10"))
MODULE_NAME = os.getenv("MODULE_NAME", "InferenceAgent")

# ─────────────────────────────────────────────────────────────────────────────
# 전역 상태 (프로세스 수명 동안 유지)
# ─────────────────────────────────────────────────────────────────────────────

request_queue: mp.Queue = mp.Queue(maxsize=16)   # 최대 16개 동시 요청 버퍼링
response_queue: mp.Queue = mp.Queue()
stop_event: mp.Event = mp.Event()
worker_process: mp.Process | None = None
temp_monitor: NPUTemperatureMonitor | None = None

# 누적 통계
_total_inferences: int = 0
_total_inference_time_ms: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan: 앱 시작/종료 시 리소스 관리
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global worker_process, temp_monitor

    # ── 추론 워커 프로세스 시작 ────────────────────────────────────────────────
    worker_process = mp.Process(
        target=inference_worker,
        args=(HEF_PATH, request_queue, response_queue, stop_event),
        name="hailo-inference-worker",
        daemon=True,
    )
    worker_process.start()
    logger.info("Inference worker process started (pid=%d)", worker_process.pid)

    # ── 온도 모니터 스레드 시작 ───────────────────────────────────────────────
    temp_monitor = NPUTemperatureMonitor(
        warning_threshold=TEMP_WARNING,
        critical_threshold=TEMP_CRITICAL,
    )
    temp_monitor.start()

    yield  # ── 서버 실행 중 ───────────────────────────────────────────────────

    # ── 종료 처리 ─────────────────────────────────────────────────────────────
    logger.info("Shutting down inference services...")
    stop_event.set()

    if worker_process and worker_process.is_alive():
        worker_process.join(timeout=5)
        if worker_process.is_alive():
            logger.warning("Worker process did not exit cleanly — terminating")
            worker_process.terminate()

    if temp_monitor:
        temp_monitor.stop()

    logger.info("Inference Agent shutdown complete")


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI 앱 정의
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Inference Agent API",
    description="Hailo-8 NPU 기반 YOLO 객체 탐지 서비스",
    version="1.0.0",
    lifespan=lifespan,
)


# ─────────────────────────────────────────────────────────────────────────────
# 헬퍼
# ─────────────────────────────────────────────────────────────────────────────

def _thermal_status() -> ThermalStatus:
    if temp_monitor is None:
        return ThermalStatus.NORMAL
    if temp_monitor.is_critical:
        return ThermalStatus.CRITICAL
    if temp_monitor.is_warning:
        return ThermalStatus.WARNING
    return ThermalStatus.NORMAL


def _npu_temp() -> float | None:
    return temp_monitor.current_temp if temp_monitor else None


def _worker_alive() -> bool:
    return worker_process is not None and worker_process.is_alive()


# ─────────────────────────────────────────────────────────────────────────────
# 엔드포인트
# ─────────────────────────────────────────────────────────────────────────────

@app.post(
    "/inference",
    response_model=PredictResponse,
    summary="이미지 추론 (YOLO)",
    description=(
        "JPEG/PNG 이미지 바이너리를 받아 Hailo-8 NPU로 추론 후 탐지 결과를 반환합니다.\n\n"
        "- 과열(≥95°C) 시 503 반환\n"
        "- 추론 프로세스 다운 시 503 반환\n"
        "- 타임아웃 시 504 반환"
    ),
    responses={
        503: {"description": "NPU 과열 또는 추론 프로세스 비정상"},
        504: {"description": "추론 타임아웃"},
    },
)
async def predict(
    image: UploadFile = File(..., description="추론할 이미지 파일 (JPEG / PNG)"),
) -> PredictResponse:
    global _total_inferences, _total_inference_time_ms

    # ── 과열 보호: CRITICAL 상태면 추론 거부 ─────────────────────────────────
    if _thermal_status() == ThermalStatus.CRITICAL:
        msg = temp_monitor.warning_message() if temp_monitor else "NPU critical overheat"
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=msg,
        )

    # ── 워커 프로세스 생존 확인 ───────────────────────────────────────────────
    if not _worker_alive():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Inference worker process is not running",
        )

    # ── 큐 포화 확인 ──────────────────────────────────────────────────────────
    if request_queue.full():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Inference queue is full — try again later",
        )

    # ── 이미지 바이너리 읽기 ──────────────────────────────────────────────────
    image_bytes = await image.read()
    if not image_bytes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Empty image file",
        )

    # ── 추론 요청 전송 ────────────────────────────────────────────────────────
    request_id = str(uuid.uuid4())
    request_queue.put({"request_id": request_id, "image_bytes": image_bytes})

    # ── 응답 수신 (동기 블로킹 — asyncio.get_event_loop.run_in_executor 대안) ─
    import asyncio

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, _wait_for_response, request_id
    )

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"Inference timeout after {INFERENCE_TIMEOUT}s",
        )
    if result.get("error"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Inference error: {result['error']}",
        )

    # ── 통계 갱신 ──────────────────────────────────────────────────────────────
    _total_inferences += 1
    _total_inference_time_ms += result["inference_time_ms"]

    # ── 과열 경고 첨부 (WARNING 수준) ─────────────────────────────────────────
    warning_msg = temp_monitor.warning_message() if temp_monitor else None

    return PredictResponse(
        detections=[Detection(**{**d, "confidence": min(float(d["confidence"]), 1.0)}) for d in result["detections"]],
        inference_time_ms=result["inference_time_ms"],
        npu_temp_celsius=_npu_temp(),
        thermal_status=_thermal_status(),
        warning=warning_msg,
    )


def _wait_for_response(request_id: str) -> dict | None:
    """response_queue를 폴링하여 해당 request_id의 결과를 반환."""
    import time

    deadline = time.monotonic() + INFERENCE_TIMEOUT
    pending: list[dict] = []

    while time.monotonic() < deadline:
        try:
            item = response_queue.get(timeout=0.1)
        except Exception:
            continue

        if item["request_id"] == request_id:
            # 다른 요청의 응답은 큐에 돌려놓기
            for p in pending:
                response_queue.put(p)
            return item
        else:
            pending.append(item)

    # 타임아웃: 대기 중이던 다른 응답 복원
    for p in pending:
        response_queue.put(p)
    return None


@app.get(
    "/health",
    response_model=HealthResponse,
    summary="헬스 체크",
)
async def health_check() -> HealthResponse:
    alive = _worker_alive()
    thermal = _thermal_status()
    return HealthResponse(
        status="healthy" if alive else "degraded",
        module=MODULE_NAME,
        npu_ready=alive,
        npu_temp_celsius=_npu_temp(),
        thermal_status=thermal,
    )


@app.get(
    "/metrics",
    response_model=MetricsResponse,
    summary="운영 메트릭",
    description="NPU 온도, 누적 추론 횟수, 평균 추론 시간을 반환합니다.",
)
async def metrics() -> MetricsResponse:
    avg_ms = (
        _total_inference_time_ms / _total_inferences
        if _total_inferences > 0
        else 0.0
    )
    return MetricsResponse(
        npu_temp_celsius=_npu_temp(),
        thermal_status=_thermal_status(),
        total_inferences=_total_inferences,
        avg_inference_time_ms=round(avg_ms, 2),
        inference_process_alive=_worker_alive(),
    )


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
    # spawn 방식: POSIX fork보다 안전 (PyTorch, Hailo SDK와 호환)
    mp.set_start_method("spawn", force=True)
    uvicorn.run(
        "src.inference.main:app",
        host="0.0.0.0",
        port=8001,
        workers=1,           # Hailo 디바이스는 단일 워커만 점유
        log_level="info",
    )

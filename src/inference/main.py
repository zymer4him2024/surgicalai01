"""
main.py — Inference Agent FastAPI server

Architecture:
  FastAPI Process (main)
  ├─ POST /inference → request_queue → [InferenceWorker]
  │                 ← response_queue ←
  ├─ GET  /health
  ├─ GET  /metrics
  └─ Thread: NPUTemperatureMonitor (5s polling)

  InferenceWorker Process (separate)
  └─ Sole owner of Hailo-8 SDK / memory leak isolation
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
# Config
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
# Global state
# ─────────────────────────────────────────────────────────────────────────────

request_queue: mp.Queue = mp.Queue(maxsize=16)
response_queue: mp.Queue = mp.Queue()
stop_event: mp.Event = mp.Event()
worker_process: mp.Process | None = None
temp_monitor: NPUTemperatureMonitor | None = None

_total_inferences: int = 0
_total_inference_time_ms: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global worker_process, temp_monitor

    worker_process = mp.Process(
        target=inference_worker,
        args=(HEF_PATH, request_queue, response_queue, stop_event),
        name="hailo-inference-worker",
        daemon=True,
    )
    worker_process.start()
    logger.info("Inference worker process started (pid=%d)", worker_process.pid)

    temp_monitor = NPUTemperatureMonitor(
        warning_threshold=TEMP_WARNING,
        critical_threshold=TEMP_CRITICAL,
    )
    temp_monitor.start()

    yield

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
# FastAPI app
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Inference Agent API",
    description="Hailo-8 NPU YOLO object detection service",
    version="1.0.0",
    lifespan=lifespan,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
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
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@app.post(
    "/inference",
    response_model=PredictResponse,
    summary="Image inference (YOLO)",
    responses={
        503: {"description": "NPU overheat or worker process down"},
        504: {"description": "Inference timeout"},
    },
)
async def predict(
    image: UploadFile = File(..., description="Image file to infer (JPEG / PNG)"),
) -> PredictResponse:
    global _total_inferences, _total_inference_time_ms

    if _thermal_status() == ThermalStatus.CRITICAL:
        msg = temp_monitor.warning_message() if temp_monitor else "NPU critical overheat"
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=msg,
        )

    if not _worker_alive():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Inference worker process is not running",
        )

    if request_queue.full():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Inference queue is full — try again later",
        )

    image_bytes = await image.read()
    if not image_bytes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Empty image file",
        )

    request_id = str(uuid.uuid4())
    request_queue.put({"request_id": request_id, "image_bytes": image_bytes})

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

    _total_inferences += 1
    _total_inference_time_ms += result["inference_time_ms"]

    warning_msg = temp_monitor.warning_message() if temp_monitor else None

    return PredictResponse(
        detections=[Detection(**{**d, "confidence": min(float(d["confidence"]), 1.0)}) for d in result["detections"]],
        inference_time_ms=result["inference_time_ms"],
        npu_temp_celsius=_npu_temp(),
        thermal_status=_thermal_status(),
        warning=warning_msg,
    )


def _wait_for_response(request_id: str) -> dict | None:
    import time

    deadline = time.monotonic() + INFERENCE_TIMEOUT
    pending: list[dict] = []

    while time.monotonic() < deadline:
        try:
            item = response_queue.get(timeout=0.1)
        except Exception:
            continue

        if item["request_id"] == request_id:
            for p in pending:
                response_queue.put(p)
            return item
        else:
            pending.append(item)

    for p in pending:
        response_queue.put(p)
    return None


@app.get("/health", response_model=HealthResponse, summary="Health check")
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


@app.get("/metrics", response_model=MetricsResponse, summary="Operational metrics")
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
    mp.set_start_method("spawn", force=True)
    uvicorn.run(
        "src.inference.main:app",
        host="0.0.0.0",
        port=8001,
        workers=1,
        log_level="info",
    )

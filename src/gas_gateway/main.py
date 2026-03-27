"""Gas Gateway Agent — Controller for gas cylinder inventory counting.

Routing:
  GET  /health    → gateway health with count and state
  GET  /status    → detailed status
  POST /snapshot  → manual operator-triggered count snapshot
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from src.gas_gateway.config import GasConfig, load_config
from src.gas_gateway.schemas import (
    GasHealthResponse,
    GasState,
    ManualSnapshotRequest,
)
from src.gas_gateway.service import (
    GasCountingState,
    build_snapshot,
    should_sync,
    update_count,
)

# ─────────────────────────────────────────────────────────────────────────────
# Config & logging
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("gas_gateway.main")

_cfg: GasConfig  # set in lifespan
_state: GasCountingState  # set in lifespan
_http: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=_cfg.gateway_timeout)
    return _http


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _read_cpu_temp() -> float | None:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read().strip()) / 1000.0
    except (FileNotFoundError, ValueError, OSError):
        return None


def _normalize_inference_response(data: dict) -> dict:
    """Adapt 3rd-party inference schema to native format."""
    if "success" in data and "items" in data:
        normalized = []
        for i, item in enumerate(data.get("items", [])):
            normalized.append({
                "class_id": i,
                "class_name": item.get("label", "unknown"),
                "confidence": item.get("score", 0.0),
                "bbox": item.get("box", [0, 0, 0, 0]),
            })
        return {
            "detections": normalized,
            "inference_time_ms": data.get("processing_time_ms", 0.0),
        }
    return data


# ─────────────────────────────────────────────────────────────────────────────
# Sync helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _send_snapshot(trigger: str, note: str = "") -> dict:
    """Send count snapshot to Firebase Sync and optionally to customer DB."""
    snapshot = build_snapshot(_state, trigger, _cfg.app_id, _cfg.device_id)
    payload = snapshot.model_dump()
    if note:
        payload["note"] = note

    try:
        await _client().post(
            f"{_cfg.firebase_sync_url}/sync",
            json={
                "event_type": "alert" if snapshot.low_stock else "periodic",
                "expected_count": _state.low_stock_threshold,
                "actual_count": _state.total_count,
                "missing_items": [],
                "detected_items": [],
                "metadata": {
                    "trigger": trigger,
                    "location": _cfg.location_name,
                    "operator_id": _cfg.operator_id,
                    "low_stock": snapshot.low_stock,
                },
            },
            timeout=_cfg.health_timeout,
        )
    except Exception as e:
        logger.error("Firebase sync failed: %s", e)

    if _cfg.customer_db_url:
        try:
            await _client().post(
                _cfg.customer_db_url, json=payload, timeout=10.0,
            )
            logger.info("Snapshot sent to customer DB")
        except Exception as e:
            logger.error("Customer DB push failed: %s", e)

    _state.last_sync_at = time.monotonic()
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# Background: counting loop
# ─────────────────────────────────────────────────────────────────────────────

async def _counting_loop() -> None:
    """Fetch frame -> inference -> total count -> low stock check -> HUD update.

    Thermal throttling:
      Normal   (< 75 C): 12 FPS
      Warm   (75-82 C):  5 FPS
      Hot    (82-88 C):  2 FPS
      Critical (> 88 C): paused (10s cooldown)
    """
    logger.info("Gas counting loop started")

    TEMP_NORMAL, TEMP_WARM, TEMP_HOT = 75.0, 82.0, 88.0
    INT_NORMAL, INT_WARM, INT_HOT, INT_CRIT = 0.08, 0.2, 0.5, 10.0

    last_temp_check = 0.0
    npu_temp = 0.0
    cpu_temp = 0.0
    fps_counter = 0
    fps_start = time.monotonic()
    current_fps = 0.0

    while True:
        t0 = time.monotonic()

        # Temperature sampling (every 5s)
        if t0 - last_temp_check >= 5.0:
            try:
                resp = await _client().get(
                    f"{_cfg.inference_url}/metrics", timeout=_cfg.health_timeout,
                )
                npu_temp = float(resp.json().get("npu_temp_celsius") or 0.0)
            except Exception:
                pass
            cpu_temp = float(_read_cpu_temp() or 0.0)
            last_temp_check = t0
        peak = max(npu_temp, cpu_temp)

        if peak >= TEMP_HOT:
            logger.warning("CRITICAL temp %.1f C — pausing 10s", peak)
            await asyncio.sleep(INT_CRIT)
            continue
        elif peak >= TEMP_WARM:
            interval = INT_HOT
        elif peak >= TEMP_NORMAL:
            interval = INT_WARM
        else:
            interval = INT_NORMAL

        try:
            # 1. Fetch frame
            frame_resp = await _client().get(
                f"{_cfg.camera_url}/frame", timeout=3.0,
            )
            if frame_resp.status_code != 200:
                await asyncio.sleep(0.5)
                continue

            # 2. Inference
            inf_resp = await _client().post(
                f"{_cfg.inference_url}{_cfg.inference_endpoint}",
                files={"image": ("frame.jpg", frame_resp.content, "image/jpeg")},
                timeout=_cfg.gateway_timeout,
            )
            if inf_resp.status_code != 200:
                await asyncio.sleep(0.5)
                continue

            data = _normalize_inference_response(inf_resp.json())
            detections = data.get("detections", [])

            # 3. Update count and state
            state_changed = await update_count(_state, detections)
            if state_changed and _state.state == GasState.LOW_STOCK:
                logger.warning(
                    "LOW STOCK: count=%d < threshold=%d",
                    _state.total_count, _state.low_stock_threshold,
                )
                asyncio.create_task(_send_snapshot("alert"))

            # 4. FPS tracking
            fps_counter += 1
            fps_elapsed = t0 - fps_start
            if fps_elapsed > 0:
                current_fps = fps_counter / fps_elapsed
            if fps_elapsed >= 5.0:
                fps_counter = 0
                fps_start = time.monotonic()

            # 5. Thermal status string
            if peak >= TEMP_HOT:
                thermal = "critical"
            elif peak >= TEMP_WARM:
                thermal = "warning"
            else:
                thermal = "normal"

            # 6. Update display HUD
            asyncio.create_task(_client().post(
                f"{_cfg.display_url}/hud",
                json={
                    "total_count": _state.total_count,
                    "state": _state.state.value,
                    "location": _cfg.location_name,
                    "operator_id": _cfg.operator_id,
                    "ai_fps": round(current_fps, 1),
                    "npu_temp_celsius": npu_temp if npu_temp > 0 else None,
                    "cpu_temp_celsius": cpu_temp if cpu_temp > 0 else None,
                    "thermal_status": thermal,
                    "inference_ready": True,
                },
                timeout=_cfg.health_timeout,
            ))

        except Exception as e:
            logger.error("Counting loop error: %s", e)
            await asyncio.sleep(1.0)
            continue

        elapsed = time.monotonic() - t0
        await asyncio.sleep(max(0.05, interval - elapsed))


# ─────────────────────────────────────────────────────────────────────────────
# Background: periodic sync loop
# ─────────────────────────────────────────────────────────────────────────────

async def _periodic_sync_loop() -> None:
    """Send count snapshot to Firebase at regular intervals."""
    logger.info("Periodic sync loop started (interval=%.0fs)", _cfg.sync_interval_sec)
    while True:
        await asyncio.sleep(_cfg.sync_interval_sec)
        if should_sync(_state, _cfg.sync_interval_sec):
            try:
                await _send_snapshot("periodic")
            except Exception as e:
                logger.error("Periodic sync error: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global _cfg, _state
    _cfg = load_config()
    _state = GasCountingState(
        low_stock_threshold=_cfg.low_stock_threshold,
        location=_cfg.location_name,
        operator_id=_cfg.operator_id,
    )
    logger.info(
        "Gas Gateway started — app_id=%s device_id=%s threshold=%d location=%r",
        _cfg.app_id, _cfg.device_id, _cfg.low_stock_threshold, _cfg.location_name,
    )

    count_task = asyncio.create_task(_counting_loop())
    sync_task = asyncio.create_task(_periodic_sync_loop())

    yield

    count_task.cancel()
    sync_task.cancel()
    if _http and not _http.is_closed:
        await _http.aclose()
    logger.info("Gas Gateway shutdown complete")


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Gas Gateway Agent API",
    description="Gas cylinder inventory counting controller",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", response_model=GasHealthResponse)
async def health_check() -> GasHealthResponse:
    return GasHealthResponse(
        status="healthy",
        module="GasGatewayAgent",
        app_id=_cfg.app_id,
        device_id=_cfg.device_id,
        state=_state.state.value,
        total_count=_state.total_count,
        low_stock_threshold=_state.low_stock_threshold,
        location=_cfg.location_name,
    )


@app.get("/status")
async def detailed_status() -> JSONResponse:
    return JSONResponse({
        "state": _state.state.value,
        "total_count": _state.total_count,
        "class_counts": _state.class_counts,
        "low_stock": _state.state == GasState.LOW_STOCK,
        "low_stock_threshold": _state.low_stock_threshold,
        "location": _cfg.location_name,
        "operator_id": _cfg.operator_id,
        "app_id": _cfg.app_id,
        "device_id": _cfg.device_id,
    })


@app.post("/snapshot")
async def manual_snapshot(body: ManualSnapshotRequest) -> JSONResponse:
    """Operator-triggered manual count snapshot."""
    result = await _send_snapshot("manual", note=body.note)
    return JSONResponse({"status": "ok", "snapshot": result})


@app.exception_handler(Exception)
async def global_exception_handler(request: Any, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "type": type(exc).__name__},
    )


if __name__ == "__main__":
    uvicorn.run(
        "src.gas_gateway.main:app",
        host="0.0.0.0",
        port=8010,
        workers=1,
        log_level="info",
    )

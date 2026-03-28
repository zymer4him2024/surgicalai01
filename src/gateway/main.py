"""
main.py — Gateway Agent / Main Controller (Module B)

Handles external client communication and system state machine.

Routing:
  POST /inference → inference_agent:8001/inference
  GET  /health   → gateway health + internal module aggregated status
  GET  /status   → per-module detailed status
  POST /job      → new tray QR scan simulation
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import json as _json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Any

import cv2
import httpx
import numpy as np
import uvicorn
from fastapi import BackgroundTasks, FastAPI, File, Header, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse
from pyzbar.pyzbar import decode as qr_decode

from src.gateway.tracker import SurgicalTracker

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("gateway.main")

MODULE_NAME = os.getenv("MODULE_NAME", "GatewayAgent")
APP_ID = os.getenv("APP_ID", "unknown")
DEVICE_ID = os.getenv("DEVICE_ID", "unknown")
INFERENCE_URL = os.getenv("INFERENCE_URL", "http://inference_agent:8001")
# For 3rd party integration, INFERENCE_ENDPOINT can be set to /predict
INFERENCE_ENDPOINT = os.getenv("INFERENCE_ENDPOINT", "/inference")
CAMERA_URL = os.getenv("CAMERA_URL", "http://camera_agent:8002")

DISPLAY_URL = os.getenv("DISPLAY_URL", "http://display_agent:8003")
FIREBASE_SYNC_URL = os.getenv("FIREBASE_SYNC_URL", "http://firebase_sync_agent:8004")
DEVICE_MASTER_URL = os.getenv("DEVICE_MASTER_URL", "http://device_master_agent:8005")

GATEWAY_TIMEOUT = float(os.getenv("GATEWAY_TIMEOUT_SEC", "15"))
HEALTH_TIMEOUT = 3.0
MATCH_TOLERANCE = int(os.getenv("MATCH_TOLERANCE", "1"))  # ±N count tolerance for unstable inference
# Optional API key enforcement. If unset (empty), /job endpoint is open (backwards-compatible).
# To enable: set GATEWAY_API_KEY=<secret> in .env and pass X-API-Key: <secret> header.
GATEWAY_API_KEY = os.getenv("GATEWAY_API_KEY", "")
# 3rd party AI authentication token (sent as Authorization: Bearer header)
EXTERNAL_AI_TOKEN = os.getenv("EXTERNAL_AI_TOKEN", "")
QR_SCAN_INTERVAL_SEC = float(os.getenv("QR_SCAN_INTERVAL_SEC", "1.0"))
QR_DEBOUNCE_SEC = float(os.getenv("QR_DEBOUNCE_SEC", "30.0"))
try:
    DEFAULT_TARGET: dict[str, int] = _json.loads(os.getenv("DEFAULT_TARGET", "{}"))
except (ValueError, TypeError):
    DEFAULT_TARGET = {}

# ─────────────────────────────────────────────────────────────────────────────
# Shared HTTP client
# ─────────────────────────────────────────────────────────────────────────────

_http_client: httpx.AsyncClient | None = None


# ─────────────────────────────────────────────────────────────────────────────
# State machine
# ─────────────────────────────────────────────────────────────────────────────

class SystemState:
    READY = "READY"  # scanning (target N items) - yellow
    MATCH = "MATCH"  # count matches - green
    ERROR = "ERROR"  # mismatch (5s → snapshot) - red

current_state: str = SystemState.READY
current_job: dict[str, Any] | None = None
mismatch_start_time: float | None = None
inference_running: bool = True
camera_active: bool = True
display_active: bool = True
latest_detections: list[dict] = []
_tracked_detections: list[dict] = []
last_scanned_qr: str | None = None
_match_achieved_at: float | None = None
_error_achieved_at: float | None = None
_pending_preset: dict | None = None
_latest_tracked_dets: list[dict] = []

_tracker = SurgicalTracker(
    max_age=15,        # longer persistence between inference frames
    min_hits=2,        # faster confirmation
    iou_threshold=0.3, # match threshold for track-detection association
    ema_alpha=0.25,    # low alpha = heavy smoothing (instruments are stationary)
)

# Temperature polling throttle — fetch /metrics at most every 5s
_last_temp_check: float = 0.0
_TEMP_POLL_SEC = 5.0
_last_metrics: dict = {}

# Device Master label cache — FDA mappings are stable, cache indefinitely
_device_info_cache: dict[str, dict] = {}


def _iou_boxes(a: list[float], b: list[float]) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _smooth_detections(new_dets: list[dict], prev_dets: list[dict], alpha: float = 0.5) -> list[dict]:
    """IoU matching + EMA to suppress bounding box jitter."""
    if not prev_dets:
        return new_dets
    smoothed = []
    for det in new_dets:
        best_iou, best_prev = 0.0, None
        for prev in prev_dets:
            if prev.get("class_name") != det.get("class_name"):
                continue
            iou = _iou_boxes(det["bbox"], prev["bbox"])
            if iou > best_iou:
                best_iou, best_prev = iou, prev
        if best_prev and best_iou > 0.15:
            nb, pb = det["bbox"], best_prev["bbox"]
            bbox = [round(alpha * n + (1 - alpha) * p, 2) for n, p in zip(nb, pb)]
            smoothed.append({**det, "bbox": bbox})
        else:
            smoothed.append(det)
    return smoothed


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global _http_client
    _http_client = httpx.AsyncClient(
        timeout=GATEWAY_TIMEOUT,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=30),
    )
    logger.info(
        "Gateway started — INFERENCE_URL=%s, CAMERA_URL=%s", INFERENCE_URL, CAMERA_URL
    )
    qr_task = asyncio.create_task(_qr_scan_loop())
    count_task = asyncio.create_task(_counting_loop())
    prompt_task = asyncio.create_task(_startup_qr_prompt())
    yield
    qr_task.cancel()
    count_task.cancel()
    prompt_task.cancel()
    try:
        await asyncio.gather(qr_task, count_task)
    except asyncio.CancelledError:
        pass
    await _http_client.aclose()
    logger.info("Gateway shutdown — HTTP client closed")


def client() -> httpx.AsyncClient:
    assert _http_client is not None, "HTTP client not initialized"
    return _http_client


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Gateway Agent API",
    description="Antigravity Surgical AI — single external entry point",
    version="1.0.0",
    lifespan=lifespan,
)


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

from pydantic import BaseModel


class JobRequest(BaseModel):
    job_id: str
    target: dict[str, int]


@app.post("/job/activate", summary="[DEBUG] Directly activate pending preset without QR scan")
async def force_activate() -> JSONResponse:
    global _pending_preset, current_job, current_state, mismatch_start_time, last_scanned_qr
    if not _pending_preset:
        return JSONResponse(status_code=400, content={"error": "No pending preset. Call /job first."})
    job = dict(_pending_preset)
    job["scan_info"] = {**job["scan_info"], "scanned_at": datetime.now().strftime("%H:%M:%S")}
    _pending_preset = None
    current_job = job
    current_state = SystemState.READY
    mismatch_start_time = None
    last_scanned_qr = None
    _tracker.reset()
    logger.info("Force-activated job: id=%r target=%s", job["id"], job["target"])
    return JSONResponse({"status": "activated", "job_id": job["id"], "target": job["target"]})


@app.post("/job", summary="Register new tray inspection job (QR scan simulation)")
async def create_job(
    req: JobRequest,
    background_tasks: BackgroundTasks,
    x_api_key: str = Header(default=""),
):
    if GATEWAY_API_KEY and x_api_key != GATEWAY_API_KEY:
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key header")
    global _pending_preset, current_job, current_state, mismatch_start_time, _match_achieved_at, _error_achieved_at
    scan_info = {
        "job_id": req.job_id,
        "scanned_at": "",
        "target": req.target,
    }
    _pending_preset = {"id": req.job_id, "target": req.target, "scan_info": scan_info}
    current_job = None
    current_state = SystemState.READY
    mismatch_start_time = None
    _match_achieved_at = None
    _error_achieved_at = None
    _tracker.reset()
    logger.info("Preset loaded (waiting for QR): id=%s target=%s", req.job_id, req.target)

    background_tasks.add_task(
        client().post,
        f"{DISPLAY_URL}/hud",
        json={
            "border_color": "yellow",
            "tray_items": [],
            "scan_info": scan_info,
            "center_text": "Por favor, escaneie o codigo QR",
        },
        timeout=HEALTH_TIMEOUT,
    )
    return {"status": "ok", "preset": _pending_preset, "state": current_state}


@app.post("/inference", summary="Image inference (YOLO) proxy and state checker")
async def predict(
    background_tasks: BackgroundTasks,
    image: UploadFile = File(..., description="Image to infer (JPEG / PNG)"),
) -> JSONResponse:
    image_bytes = await image.read()
    if not image_bytes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Empty image file",
        )

    try:
        inf_headers: dict[str, str] = {"X-App-ID": APP_ID, "X-Device-ID": DEVICE_ID}
        if EXTERNAL_AI_TOKEN:
            inf_headers["Authorization"] = f"Bearer {EXTERNAL_AI_TOKEN}"
        resp = await client().post(
            f"{INFERENCE_URL}{INFERENCE_ENDPOINT}",
            files={"image": (image.filename or "image.jpg", image_bytes, image.content_type or "image/jpeg")},
            headers=inf_headers,
            timeout=GATEWAY_TIMEOUT,
        )
    except Exception as exc:
        logger.error("Inference call failed: %s", exc)
        raise HTTPException(status_code=503, detail="Inference agent unreachable")

    data = _normalize_inference_response(resp.json())

    global current_state, mismatch_start_time
    if current_job:
        actual_counts: dict[str, int] = {}
        for det in data.get("detections", []):
            name = det.get("class_name")
            actual_counts[name] = actual_counts.get(name, 0) + 1

        target_counts = current_job.get("target", {})
        is_match = True
        for key, expected in target_counts.items():
            if actual_counts.get(key, 0) != expected:
                is_match = False
                break

        if is_match:
            current_state = SystemState.MATCH
            mismatch_start_time = None
        else:
            if current_state == SystemState.MATCH:
                current_state = SystemState.READY
            if mismatch_start_time is None:
                mismatch_start_time = time.monotonic()
            elif time.monotonic() - mismatch_start_time >= 5.0:
                if current_state != SystemState.ERROR:
                    background_tasks.add_task(_trigger_snapshot)
                current_state = SystemState.ERROR

        border_color = {"READY": "yellow", "MATCH": "green", "ERROR": "red"}.get(current_state, "yellow")
        tray_items = [{"class_name": k, "count": v} for k, v in actual_counts.items()]
        background_tasks.add_task(
            client().post,
            f"{DISPLAY_URL}/hud",
            json={"border_color": border_color, "tray_items": tray_items},
            timeout=HEALTH_TIMEOUT,
        )

    return JSONResponse(status_code=resp.status_code, content=data)


def _parse_qr_target(qr_data: str) -> dict[str, int]:
    try:
        parsed = _json.loads(qr_data)
        if isinstance(parsed, dict) and "target" in parsed:
            return parsed["target"]
    except (ValueError, TypeError) as exc:
        logger.debug("QR target parse failed: %s — raw=%r", exc, qr_data)
    return DEFAULT_TARGET.copy()


async def _qr_scan_loop() -> None:
    global current_job, current_state, mismatch_start_time, last_scanned_qr, _pending_preset
    last_trigger_time: float = 0.0
    loop = asyncio.get_running_loop()

    logger.info("QR scan loop started")

    while True:
        await asyncio.sleep(QR_SCAN_INTERVAL_SEC)
        try:
            resp = await client().get(f"{CAMERA_URL}/frame", timeout=3.0)
            if resp.status_code != 200:
                continue
            image_bytes = resp.content

            def _decode():
                nparr = np.frombuffer(image_bytes, np.uint8)
                frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                if frame is None:
                    return []
                small = cv2.resize(frame, (640, 360))
                return qr_decode(small)

            results = await loop.run_in_executor(None, _decode)
            if not results:
                continue

            qr_data = results[0].data.decode("utf-8").strip()
            now = time.monotonic()
            if qr_data == last_scanned_qr and now - last_trigger_time < 3.0:
                continue

            last_scanned_qr = qr_data
            last_trigger_time = now

            if _pending_preset is None:
                # Trigger Firebase load in background (non-blocking)
                asyncio.create_task(
                    client().post(f"{FIREBASE_SYNC_URL}/load_current_set", timeout=3.0)
                )
                # Fallback: parse target from QR content or use DEFAULT_TARGET
                fallback_target = _parse_qr_target(qr_data)
                job_id = f"QR-{datetime.now().strftime('%H%M%S')}"
                _pending_preset = {
                    "id": job_id,
                    "target": fallback_target,
                    "scan_info": {"job_id": job_id, "scanned_at": "", "target": fallback_target},
                }
                logger.info("QR fallback: no preset loaded, using target=%s", fallback_target)

            if _pending_preset:
                job = dict(_pending_preset)
                job["scan_info"] = {**job["scan_info"], "scanned_at": datetime.now().strftime("%H:%M:%S")}
                _pending_preset = None
                current_job = job
                current_state = SystemState.READY
                mismatch_start_time = None
                logger.info("QR detected → detection started: id=%r target=%s", job["id"], job["target"])

            await client().post(
                f"{DISPLAY_URL}/hud",
                json={
                    "border_color": "yellow",
                    "tray_items": [],
                    "scan_info": current_job["scan_info"],
                    "flash_text": "QR SCANNED",
                    "center_text": "",
                },
                timeout=HEALTH_TIMEOUT,
            )
        except Exception as e:
            logger.debug("QR loop error: %s", e)



async def _counting_loop() -> None:
    """Background real-time counting loop (active only when a job is set).

    Thermal throttling (uses max of NPU and CPU temps — whichever is hotter):
      Normal   (< 60°C): 12 FPS (0.08s interval)
      Warm   (60-70°C):  5 FPS  (0.2s interval)
      Hot    (70-78°C):  2 FPS  (0.5s interval)
      Critical (> 78°C): inference fully paused (10s cooldown loop)

    Temperature is sampled at the TOP of each cycle so throttle decisions
    are always based on current readings, not last cycle's stale data.
    """
    global current_state, mismatch_start_time, _tracked_detections, last_scanned_qr, _match_achieved_at, _error_achieved_at, current_job, _pending_preset, _latest_tracked_dets
    logger.info("Counting loop started (thermal-aware throttling enabled)")

    TEMP_NORMAL = 75.0
    TEMP_WARM = 82.0
    TEMP_HOT = 88.0

    INTERVAL_NORMAL = 0.08
    INTERVAL_WARM = 0.2
    INTERVAL_HOT = 0.5
    INTERVAL_CRITICAL = 10.0

    _last_npu_temp: float = 0.0
    _last_cpu_temp: float = 0.0
    _fps_counter = 0
    _fps_start = time.monotonic()
    _last_pushed_preset: dict | None = None  # track last scan_info pushed to display
    _idle_hud_tick: int = 0  # re-send scan_info every N idle cycles

    while True:
        if not camera_active:
            await asyncio.sleep(1.0)
            continue

        if not inference_running or not current_job:
            # Keep DATA INFO panel populated while waiting for QR scan.
            # Re-send every 10 idle cycles (~5s) so display recovers after restart.
            if _pending_preset and _pending_preset.get("scan_info"):
                _idle_hud_tick += 1
                if _pending_preset is not _last_pushed_preset or _idle_hud_tick >= 10:
                    _last_pushed_preset = _pending_preset
                    _idle_hud_tick = 0
                    try:
                        await client().post(
                            f"{DISPLAY_URL}/hud",
                            json={"scan_info": _pending_preset["scan_info"]},
                            timeout=HEALTH_TIMEOUT,
                        )
                    except Exception:
                        pass
            else:
                _last_pushed_preset = None
                _idle_hud_tick = 0
            await asyncio.sleep(0.5)
            continue

        # Read temps at top of cycle — throttled to once every 5s
        global _last_temp_check, _last_metrics
        now_temp = time.monotonic()
        if now_temp - _last_temp_check >= _TEMP_POLL_SEC:
            try:
                _last_metrics = await _fetch_json(f"{INFERENCE_URL}/metrics")
                _last_npu_temp = float(_last_metrics.get("npu_temp_celsius") or 0.0)
            except Exception:
                pass
            _last_cpu_temp = float(_read_cpu_temp() or 0.0)
            _last_temp_check = now_temp
        peak_temp = max(_last_npu_temp, _last_cpu_temp)

        start_time = time.monotonic()

        if peak_temp >= TEMP_HOT:
            logger.warning(
                "CRITICAL temp — NPU=%.1f°C CPU=%.1f°C — pausing inference for %.0fs",
                _last_npu_temp, _last_cpu_temp, INTERVAL_CRITICAL,
            )
            await asyncio.sleep(INTERVAL_CRITICAL)
            continue
        elif peak_temp >= TEMP_WARM:
            target_interval = INTERVAL_HOT
        elif peak_temp >= TEMP_NORMAL:
            target_interval = INTERVAL_WARM
        else:
            target_interval = INTERVAL_NORMAL

        try:
            resp = await client().get(f"{CAMERA_URL}/frame", timeout=3.0)
            if resp.status_code != 200:
                await asyncio.sleep(0.5); continue
            image_bytes = resp.content

            inf_headers: dict[str, str] = {"X-App-ID": APP_ID, "X-Device-ID": DEVICE_ID}
            if EXTERNAL_AI_TOKEN:
                inf_headers["Authorization"] = f"Bearer {EXTERNAL_AI_TOKEN}"
            inf_resp = await client().post(
                f"{INFERENCE_URL}{INFERENCE_ENDPOINT}",
                files={"image": ("image.jpg", image_bytes, "image/jpeg")},
                headers=inf_headers,
                timeout=GATEWAY_TIMEOUT
            )
            if inf_resp.status_code != 200:
                await asyncio.sleep(0.5); continue
            
            data = _normalize_inference_response(inf_resp.json())
            raw_dets = data.get("detections", [])

            tracked_dets = _tracker.update(raw_dets)
            if not tracked_dets and raw_dets:
                tracked_dets = _smooth_detections(raw_dets, _tracked_detections)
            _tracked_detections = tracked_dets

            actual_counts = _tracker.get_counts()

            target_counts = current_job.get("target", {})
            is_match = bool(target_counts) and all(
                abs(actual_counts.get(key, 0) - expected) <= MATCH_TOLERANCE
                for key, expected in target_counts.items()
            )

            enriched_items = await _enrich_with_device_info(actual_counts)
            global latest_detections
            latest_detections = enriched_items

            HOLD_SEC = 3.0

            if is_match:
                if current_state != SystemState.MATCH:
                    last_scanned_qr = None
                    _match_achieved_at = time.monotonic()
                    asyncio.create_task(_log_inspection_round("GOOD", enriched_items))
                    asyncio.create_task(_sync_match_event(enriched_items))
                    asyncio.create_task(client().post(
                        f"{DISPLAY_URL}/hud",
                        json={"border_color": "green", "center_text": "Good"},
                        timeout=HEALTH_TIMEOUT,
                    ))
                current_state = SystemState.MATCH
                mismatch_start_time = None
                if _match_achieved_at and time.monotonic() - _match_achieved_at >= HOLD_SEC:
                    logger.info("GOOD status held %.0fs — advancing to next set", HOLD_SEC)
                    current_job = None
                    _pending_preset = None
                    current_state = SystemState.READY
                    mismatch_start_time = None
                    _match_achieved_at = None
                    last_scanned_qr = None
                    _tracker.reset()
                    asyncio.create_task(_advance_to_next_set())
                    continue
            else:
                _match_achieved_at = None
                if current_state == SystemState.MATCH:
                    current_state = SystemState.READY
                if actual_counts:
                    if current_state != SystemState.ERROR:
                        # First NO MATCH: show result and trigger snapshot immediately
                        current_state = SystemState.ERROR
                        _error_achieved_at = time.monotonic()
                        last_scanned_qr = None
                        asyncio.create_task(_trigger_snapshot(enriched_items))
                        asyncio.create_task(_log_inspection_round("NO MATCH", enriched_items))
                        asyncio.create_task(client().post(
                            f"{DISPLAY_URL}/hud",
                            json={"border_color": "red", "center_text": "No Match"},
                            timeout=HEALTH_TIMEOUT,
                        ))
                    elif _error_achieved_at and time.monotonic() - _error_achieved_at >= HOLD_SEC:
                        logger.info("NO MATCH held %.0fs — re-arming same tray for re-scan", HOLD_SEC)
                        # Re-arm same preset so QR scan retries the same tray
                        _pending_preset = {
                            "id": current_job["id"],
                            "target": current_job["target"],
                            "scan_info": {
                                "job_id": current_job["scan_info"].get("job_id", current_job["id"]),
                                "scanned_at": "",
                                "target": current_job["target"],
                            },
                        }
                        current_job = None
                        current_state = SystemState.READY
                        mismatch_start_time = None
                        _error_achieved_at = None
                        last_scanned_qr = None
                        _tracker.reset()
                        asyncio.create_task(client().post(
                            f"{DISPLAY_URL}/hud",
                            json={"border_color": "yellow", "tray_items": [], "center_text": "Por favor, escaneie o codigo QR"},
                            timeout=HEALTH_TIMEOUT,
                        ))
                        continue
                else:
                    mismatch_start_time = None

            border_color = {"READY": "yellow", "MATCH": "green", "ERROR": "red"}.get(current_state, "yellow")
            tray_items = enriched_items

            # Forward tracker-smoothed bboxes to display (confirmed tracks only)
            display_dets = tracked_dets if tracked_dets else raw_dets
            if display_active and display_dets:
                det_payload = [
                    {
                        "class_name": d.get("class_name", ""),
                        "confidence": d.get("confidence", 0.0),
                        "bbox": d.get("bbox", [0, 0, 0, 0]),
                    }
                    for d in display_dets
                ]
                asyncio.create_task(client().post(
                    f"{DISPLAY_URL}/frame",
                    json={"detections": det_payload},
                    timeout=HEALTH_TIMEOUT,
                ))

            _fps_counter += 1
            fps_elapsed = time.monotonic() - _fps_start
            current_fps = _fps_counter / fps_elapsed if fps_elapsed > 0 else 0.0
            if fps_elapsed >= 5.0:
                _fps_counter = 0
                _fps_start = time.monotonic()

            global _latest_tracked_dets
            _latest_tracked_dets = tracked_dets

            if display_active:
                inf_ready = True
                try:
                    inf_ready = _last_metrics.get("inference_ready", True)
                except Exception:
                    pass

                if peak_temp >= TEMP_HOT:
                    thermal_str = "critical"
                elif peak_temp >= TEMP_WARM:
                    thermal_str = "warning"
                else:
                    thermal_str = "normal"

                npu_temp = _last_npu_temp if _last_npu_temp > 0 else None
                cpu_temp = _last_cpu_temp if _last_cpu_temp > 0 else None

                hud_payload: dict = {
                    "border_color": border_color,
                    "tray_items": tray_items,
                    "ai_status": {
                        "inference_ready": inf_ready,
                        "fps": round(current_fps, 1),
                        "npu_temp_celsius": npu_temp,
                        "cpu_temp_celsius": cpu_temp,
                        "thermal_status": thermal_str,
                    },
                }
                if current_job and current_job.get("scan_info"):
                    hud_payload["scan_info"] = current_job["scan_info"]
                await client().post(
                    f"{DISPLAY_URL}/hud",
                    json=hud_payload,
                    timeout=HEALTH_TIMEOUT,
                )
        except Exception as e:
            logger.error("Counting loop error: %s", e)
            await asyncio.sleep(1.0); continue

        elapsed = time.monotonic() - start_time
        await asyncio.sleep(max(0.005, target_interval - elapsed))


async def _enrich_with_device_info(
    actual_counts: dict[str, int],
) -> list[dict]:
    """Query Device Master Agent for FDA device info in parallel. Fail-open.
    Results are cached indefinitely — FDA label mappings are stable."""
    labels = list(actual_counts.keys())
    if not labels:
        return []

    async def _lookup(label: str) -> dict:
        if label in _device_info_cache:
            cached = _device_info_cache[label]
            return {**cached, "count": actual_counts[label]}
        fallback = {"class_name": label, "count": actual_counts[label]}
        try:
            resp = await client().get(
                f"{DEVICE_MASTER_URL}/device/lookup",
                params={"label": label},
                timeout=0.5,
            )
            if resp.status_code == 200:
                d = resp.json()
                result = {
                    "class_name": label,
                    "count": actual_counts[label],
                    "device_name": d.get("device_name"),
                    "product_code": d.get("product_code"),
                    "fda_class": d.get("device_class"),
                }
                _device_info_cache[label] = {k: v for k, v in result.items() if k != "count"}
                return result
        except Exception:
            pass
        return fallback

    results = await asyncio.gather(*[_lookup(l) for l in labels], return_exceptions=True)
    return [r if isinstance(r, dict) else {"class_name": labels[i], "count": actual_counts[labels[i]]}
            for i, r in enumerate(results)]


async def _startup_qr_prompt() -> None:
    """Show loading message immediately, then QR prompt once containers are ready."""
    try:
        await client().post(
            f"{DISPLAY_URL}/hud",
            json={
                "border_color": "yellow",
                "tray_items": [],
                "center_text": "O sistema estará pronto em breve",
            },
            timeout=HEALTH_TIMEOUT,
        )
        logger.info("Startup loading prompt sent to display")
    except Exception as exc:
        logger.debug("Startup loading prompt failed: %s", exc)
    await asyncio.sleep(10.0)
    # Pre-load preset from Firebase so QR scan activates immediately
    try:
        await client().post(f"{FIREBASE_SYNC_URL}/load_current_set", timeout=5.0)
        logger.info("Startup preset pre-load requested from Firebase")
    except Exception as exc:
        logger.debug("Startup preset pre-load failed: %s", exc)
    try:
        await client().post(
            f"{DISPLAY_URL}/hud",
            json={
                "border_color": "yellow",
                "tray_items": [],
                "center_text": "Por favor, escaneie o codigo QR",
            },
            timeout=HEALTH_TIMEOUT,
        )
        logger.info("Startup QR prompt sent to display")
    except Exception as exc:
        logger.debug("Startup QR prompt failed: %s", exc)


async def _advance_to_next_set() -> None:
    """Reset display to yellow and request next set transition from Firebase Sync."""
    try:
        await asyncio.gather(
            client().post(
                f"{DISPLAY_URL}/hud",
                json={
                    "border_color": "yellow",
                    "tray_items": [],
                    "center_text": "Por favor, escaneie o codigo QR",
                },
                timeout=HEALTH_TIMEOUT,
            ),
            client().post(
                f"{DISPLAY_URL}/frame",
                json={"detections": []},
                timeout=HEALTH_TIMEOUT,
            ),
        )
    except Exception:
        pass
    try:
        await client().post(f"{FIREBASE_SYNC_URL}/advance_set", timeout=5.0)
        logger.info("advance_set requested — next preset set incoming")
    except Exception as exc:
        logger.warning("advance_set error: %s", exc)

    # Fallback: if Firebase does not call back with the new preset within 5s,
    # request the current set directly (covers Firestore latency and partial failures)
    await asyncio.sleep(5.0)
    if _pending_preset is None and current_job is None:
        logger.warning("No preset received 5s after advance — falling back to load_current_set")
        try:
            await client().post(f"{FIREBASE_SYNC_URL}/load_current_set", timeout=5.0)
        except Exception as exc:
            logger.warning("Fallback load_current_set failed: %s", exc)


async def _log_inspection_round(result: str, detected_items: list[dict]) -> None:
    """Log GOOD / NO MATCH result to Firestore 5-slot circular buffer."""
    if not current_job:
        return
    try:
        scan_info = current_job.get("scan_info", {})
        round_data = {
            "job_id": current_job.get("id", ""),
            "scanned_at": scan_info.get("scanned_at", ""),
            "target": current_job.get("target", {}),
            "detected": {d["class_name"]: d["count"] for d in detected_items if "class_name" in d},
            "result": result,
        }
        await client().post(f"{FIREBASE_SYNC_URL}/log_round", json=round_data, timeout=3.0)
    except Exception as exc:
        logger.debug("log_inspection_round error: %s", exc)


async def _sync_match_event(detected_items: list[dict]) -> None:
    """Write a match (success) event to sync_events so the dashboard can track it."""
    if not current_job:
        return
    try:
        target = current_job.get("target", {})
        detected = {d["class_name"]: d["count"] for d in detected_items if "class_name" in d}
        payload = {
            "event_type": "match",
            "expected_count": sum(target.values()),
            "actual_count": sum(detected.values()),
            "missing_items": [],
            "detected_items": detected_items,
            "metadata": {
                "job_id": current_job.get("id", ""),
                "target": target,
                "detected": detected,
            },
        }
        await client().post(f"{FIREBASE_SYNC_URL}/sync", json=payload, timeout=3.0)
    except Exception as exc:
        logger.debug("sync match event error: %s", exc)


async def _trigger_snapshot(devices_resolved: list[dict] | None = None):
    if not current_job: return
    try:
        body: dict = {"job_id": current_job.get("id"), "reason": "timeout_mismatch"}
        if devices_resolved:
            body["devices_resolved"] = devices_resolved
        await client().post(f"{FIREBASE_SYNC_URL}/snap", json=body, timeout=3.0)
    except Exception as exc:
        logger.debug("Snapshot trigger failed (non-critical): %s", exc)


class ControlRequest(BaseModel):
    inference_running: bool | None = None
    camera_active: bool | None = None
    display_active: bool | None = None


@app.post("/control", summary="System control toggles (inference, camera, display)")
async def update_controls(req: ControlRequest):
    global inference_running, camera_active, display_active
    if req.inference_running is not None:
        inference_running = req.inference_running
    if req.camera_active is not None:
        camera_active = req.camera_active
    if req.display_active is not None:
        display_active = req.display_active

    logger.info("Controls updated: inference=%s, camera=%s, display=%s",
                inference_running, camera_active, display_active)

    return {
        "status": "updated",
        "inference_running": inference_running,
        "camera_active": camera_active,
        "display_active": display_active
    }


@app.post("/job/start", summary="Resume inference loop (legacy)")
async def start_inference():
    global inference_running
    inference_running = True
    logger.info("Inference loop started by legacy control command")
    return {"status": "started", "inference_running": True}


@app.post("/job/stop", summary="Pause inference loop (legacy)")
async def stop_inference():
    global inference_running
    inference_running = False
    logger.info("Inference loop stopped by legacy control command")
    return {"status": "stopped", "inference_running": False}


@app.get("/job/status", summary="Inference loop and system state")
async def job_status():
    return {
        "inference_running": inference_running,
        "camera_active": camera_active,
        "display_active": display_active,
        "current_job": current_job,
        "system_state": current_state,
        "latest_detections": latest_detections,
    }


@app.get("/health")
async def health_check() -> JSONResponse:
    inference_health = await _fetch_module_health(INFERENCE_URL, "inference_agent")
    camera_health = await _fetch_module_health(CAMERA_URL, "camera_agent")
    all_healthy = inference_health["reachable"] and camera_health["reachable"]
    return JSONResponse(status_code=200, content={
        "status": "healthy" if all_healthy else "degraded",
        "app_id": APP_ID,
        "device_id": DEVICE_ID,
        "modules": {"inference": inference_health, "camera": camera_health},
    })


@app.get("/status")
async def status_check() -> JSONResponse:
    inf_h, inf_m, cam_h = await asyncio.gather(_fetch_json(f"{INFERENCE_URL}/health"), _fetch_json(f"{INFERENCE_URL}/metrics"), _fetch_json(f"{CAMERA_URL}/health"))
    return JSONResponse(content={"gateway": "healthy", "inference": {"health": inf_h, "metrics": inf_m}, "camera": cam_h})


def _read_cpu_temp() -> float | None:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return float(f.read().strip()) / 1000.0
    except (FileNotFoundError, ValueError, OSError):
        return None


async def _fetch_module_health(base_url: str, name: str) -> dict[str, Any]:
    try:
        resp = await client().get(f"{base_url}/health", timeout=3.0)
        data = resp.json()
        data["reachable"] = resp.status_code == 200
        return data
    except Exception:
        return {"reachable": False, "status": "unreachable"}


async def _fetch_json(url: str) -> dict[str, Any]:
    try:
        resp = await client().get(url, timeout=3.0)
        return resp.json()
    except Exception:
        return {"error": "unreachable"}


def _normalize_inference_response(data: dict) -> dict:
    """Adapter pattern to normalize 3rd party AI response schemas."""
    # Check if this is the mock 3rd-party schema
    if "success" in data and "items" in data:
        normalized_detections = []
        for i, item in enumerate(data.get("items", [])):
            normalized_detections.append({
                "class_id": i,
                "class_name": item.get("label", "unknown"),
                "confidence": item.get("score", 0.0),
                "bbox": item.get("box", [0,0,0,0])
            })
        return {
            "detections": normalized_detections,
            "inference_time_ms": data.get("inference_ms", data.get("processing_time_ms", 0.0)),
            "npu_temp_celsius": data.get("device_temp_c", 0.0),
            "thermal_status": "normal"
        }
    return data # Return native system schema untouched


if __name__ == "__main__":
    uvicorn.run("src.gateway.main:app", host="0.0.0.0", port=8000, log_level="info")

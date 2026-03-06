"""
main.py — Gateway Agent / Main Controller (Module B)

외부 클라이언트 통신 담당 및 시스템 상태 머신(State Machine) 관리.

라우팅 구조:
  POST /inference → inference_agent:8001/inference
  GET  /health   → gateway 자체 + 내부 모듈 집계 상태 반환
  GET  /status   → 모듈별 상세 상태
  POST /job      → 신규 트레이 QR 스캔 시뮬레이션
"""

from __future__ import annotations

import asyncio
import base64
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
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse
from pyzbar.pyzbar import decode as qr_decode

from src.gateway.tracker import SurgicalTracker

# ─────────────────────────────────────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("gateway.main")

MODULE_NAME = os.getenv("MODULE_NAME", "GatewayAgent")
INFERENCE_URL = os.getenv("INFERENCE_URL", "http://inference_agent:8001")
CAMERA_URL = os.getenv("CAMERA_URL", "http://camera_agent:8002")
DISPLAY_URL = os.getenv("DISPLAY_URL", "http://display_agent:8003")
FIREBASE_SYNC_URL = os.getenv("FIREBASE_SYNC_URL", "http://firebase_sync_agent:8004")
DEVICE_MASTER_URL = os.getenv("DEVICE_MASTER_URL", "http://device_master_agent:8005")

# 게이트웨이 자체 타임아웃 (inference 내부 타임아웃 10s + 여유 5s)
GATEWAY_TIMEOUT = float(os.getenv("GATEWAY_TIMEOUT_SEC", "15"))
# 헬스 체크용 짧은 타임아웃
HEALTH_TIMEOUT = 3.0
# QR 스캔 루프 설정
QR_SCAN_INTERVAL_SEC = float(os.getenv("QR_SCAN_INTERVAL_SEC", "1.0"))
QR_DEBOUNCE_SEC = float(os.getenv("QR_DEBOUNCE_SEC", "30.0"))
# QR에 target 정보가 없을 때 사용할 기본 target
try:
    DEFAULT_TARGET: dict[str, int] = _json.loads(os.getenv("DEFAULT_TARGET", "{}"))
except (ValueError, TypeError):
    DEFAULT_TARGET = {}

# ─────────────────────────────────────────────────────────────────────────────
# 공유 HTTP 클라이언트
# ─────────────────────────────────────────────────────────────────────────────

_http_client: httpx.AsyncClient | None = None

# ── State Machine (Main Controller) ──────────────────────────────────────────

class SystemState:
    READY = "READY"    # 스캔 중 (Target N items) - Yellow
    MATCH = "MATCH"    # 수량 일치 - Green
    ERROR = "ERROR"    # 불일치 (5초 유지 시 스냅샷) - Red

current_state: str = SystemState.READY
current_job: dict[str, Any] | None = None
mismatch_start_time: float | None = None
inference_running: bool = True  # Start/Stop 제어 플래그
camera_active: bool = True      # 카메라 프레임 수신 제어 플래그
display_active: bool = True     # HDMI 출력 제어 플래그
latest_detections: list[dict] = []  # 최신 탐지 결과 (admin 실시간 상태용)
_tracked_detections: list[dict] = []  # EMA 스무딩용 이전 프레임 탐지 결과
last_scanned_qr: str | None = None   # QR 디바운스용 (판정 후 리셋)
_match_achieved_at: float | None = None  # YES MATCH 표시 시작 시각
_error_achieved_at: float | None = None  # NO MATCH ERROR 표시 시작 시각
_pending_preset: dict | None = None      # 프리셋 로드됨, QR 스캔 대기 중

# ByteTrack-style 트래커 (IoU 매칭 + EMA 스무딩 + 고유 ID 카운팅)
_tracker = SurgicalTracker(
    max_age=30,         # 30프레임 동안 미매칭 시 트랙 삭제
    min_hits=3,         # 3회 이상 매칭되어야 확정 트랙으로 간주
    iou_threshold=0.3,  # IoU 30% 이상이면 동일 객체로 매칭
    ema_alpha=0.6,      # EMA: 60% new + 40% old
)


def _iou_boxes(a: list[float], b: list[float]) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _smooth_detections(new_dets: list[dict], prev_dets: list[dict], alpha: float = 0.5) -> list[dict]:
    """IoU 매칭 + EMA로 바운딩 박스 떨림 억제.
    alpha=0.5: 현재 프레임 50% + 이전 프레임 50% 혼합.
    """
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
    yield
    qr_task.cancel()
    count_task.cancel()
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
# FastAPI 앱
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Gateway Agent API",
    description="Antigravity Surgical AI — 단일 외부 진입점",
    version="1.0.0",
    lifespan=lifespan,
)


# ─────────────────────────────────────────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────────────────────────────────────────

from pydantic import BaseModel

class JobRequest(BaseModel):
    job_id: str
    target: dict[str, int]

@app.post("/job", summary="신규 트레이 검수 작업 등록 (QR 스캔 시뮬레이션)")
async def create_job(req: JobRequest, background_tasks: BackgroundTasks):
    global _pending_preset, current_job, current_state, mismatch_start_time, _match_achieved_at, _error_achieved_at
    scan_info = {
        "job_id": req.job_id,
        "scanned_at": "",  # filled in when QR is actually scanned
        "target": req.target,
    }
    # Pre-load preset — detection does NOT start until QR is scanned
    _pending_preset = {"id": req.job_id, "target": req.target, "scan_info": scan_info}
    current_job = None
    current_state = SystemState.READY
    mismatch_start_time = None
    _match_achieved_at = None
    _error_achieved_at = None
    _tracker.reset()
    logger.info("Preset loaded (waiting for QR): id=%s target=%s", req.job_id, req.target)

    # Show DATA INFO on display — yellow border, awaiting QR scan
    background_tasks.add_task(
        client().post,
        f"{DISPLAY_URL}/hud",
        json={"border_color": "yellow", "tray_items": [], "scan_info": scan_info},
        timeout=HEALTH_TIMEOUT,
    )
    return {"status": "ok", "preset": _pending_preset, "state": current_state}

@app.post(
    "/inference",
    summary="이미지 추론 (YOLO) 프록시 및 State Checker",
)
async def predict(
    background_tasks: BackgroundTasks,
    image: UploadFile = File(..., description="추론할 이미지 (JPEG / PNG)"),
) -> JSONResponse:
    image_bytes = await image.read()
    if not image_bytes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Empty image file",
        )

    try:
        resp = await client().post(
            f"{INFERENCE_URL}/inference",
            files={"image": (image.filename or "image.jpg", image_bytes, image.content_type or "image/jpeg")},
            timeout=GATEWAY_TIMEOUT,
        )
    except Exception as exc:
        logger.error("Inference call failed: %s", exc)
        raise HTTPException(status_code=503, detail="Inference agent unreachable")

    data = resp.json()
    
    # State Machine Logic
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
                if current_state != SystemState.ERROR:  # Trigger only on transition
                    background_tasks.add_task(_trigger_snapshot)
                current_state = SystemState.ERROR

        # Update Display Agent HUD
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
    except: pass
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
                return qr_decode(frame) if frame is not None else []

            results = await loop.run_in_executor(None, _decode)
            if not results:
                continue

            qr_data = results[0].data.decode("utf-8").strip()
            now = time.monotonic()
            if qr_data == last_scanned_qr and now - last_trigger_time < 3.0:
                continue

            last_scanned_qr = qr_data
            last_trigger_time = now

            if _pending_preset:
                # Preset cycle mode: QR scan triggers detection using pre-loaded target
                job = dict(_pending_preset)
                job["scan_info"] = {**job["scan_info"], "scanned_at": datetime.now().strftime("%H:%M:%S")}
                _pending_preset = None
                current_job = job
                current_state = SystemState.READY
                mismatch_start_time = None
                logger.info("QR detected → detection started with preset: id=%r target=%s", job["id"], job["target"])
            else:
                # Legacy mode: use QR content as target
                target = _parse_qr_target(qr_data)
                scan_info = {
                    "job_id": qr_data,
                    "scanned_at": datetime.now().strftime("%H:%M:%S"),
                    "target": target,
                }
                current_job = {"id": qr_data, "target": target, "scan_info": scan_info}
                current_state = SystemState.READY
                mismatch_start_time = None
                logger.info("QR detected → new job: id=%r target=%s", qr_data, target)

            # Yellow border = detection active, awaiting verdict
            await client().post(
                f"{DISPLAY_URL}/hud",
                json={"border_color": "yellow", "tray_items": [], "scan_info": current_job["scan_info"], "flash_text": "QR SCANNED"},
                timeout=HEALTH_TIMEOUT,
            )
        except Exception as e:
            logger.debug("QR loop error: %s", e)


async def _counting_loop() -> None:
    """백그라운드 실시간 카운팅 루프 (Job이 있을 때만 활성화).
    
    열 관리 (Thermal Throttling):
      - Normal  (< 70°C): 2 FPS (0.5s interval)
      - Warm    (70-80°C): 1 FPS (1.0s interval)  
      - Hot     (80-85°C): 0.33 FPS (3.0s interval)
      - Critical(> 85°C):  추론 일시 중단 (10s 대기 후 재시도)
    """
    global current_state, mismatch_start_time, _tracked_detections, last_scanned_qr, _match_achieved_at, _error_achieved_at, current_job, _pending_preset
    logger.info("Counting loop started (thermal-aware throttling enabled)")

    # 열 관리 상수
    TEMP_NORMAL = 70.0
    TEMP_WARM = 80.0
    TEMP_HOT = 85.0
    
    INTERVAL_NORMAL = 0.5    # 2 FPS — 안정적 실시간
    INTERVAL_WARM = 1.0      # 1 FPS — 약간의 지연
    INTERVAL_HOT = 3.0       # 0.33 FPS — 냉각 우선
    INTERVAL_CRITICAL = 10.0  # 추론 중단, 냉각 대기
    
    _last_npu_temp = 0.0
    _fps_counter = 0
    _fps_start = time.monotonic()
    
    while True:
        if not camera_active:
            await asyncio.sleep(1.0)
            continue
            
        # Idle 상태: 잡이 없거나 추론 중지일 때도 디스플레이에 프레임은 전송하여 화면 프리징 방지
        if not inference_running or not current_job:
            try:
                resp = await client().get(f"{CAMERA_URL}/frame", timeout=3.0)
                if resp.status_code == 200 and display_active:
                    image_b64 = base64.b64encode(resp.content).decode("utf-8")
                    await client().post(
                        f"{DISPLAY_URL}/frame",
                        json={"image_b64": image_b64, "detections": []},
                        timeout=HEALTH_TIMEOUT
                    )
            except Exception:
                pass
            await asyncio.sleep(0.5)  # Idle 시 2 FPS
            continue

        start_time = time.monotonic()
        
        # ── 열 관리: 현재 온도에 따라 인터벌 결정 ─────────────────────────
        if _last_npu_temp >= TEMP_HOT:
            logger.warning("🔥 NPU temp %.1f°C — CRITICAL throttle (pausing inference)", _last_npu_temp)
            await asyncio.sleep(INTERVAL_CRITICAL)
            # 온도 재확인
            try:
                metrics = await _fetch_json(f"{INFERENCE_URL}/metrics")
                _last_npu_temp = metrics.get("npu_temp_celsius", 0.0) or 0.0
            except: pass
            continue
        elif _last_npu_temp >= TEMP_WARM:
            target_interval = INTERVAL_HOT
        elif _last_npu_temp >= TEMP_NORMAL:
            target_interval = INTERVAL_WARM
        else:
            target_interval = INTERVAL_NORMAL
        
        try:
            # 1. 카메라 프레임 획득
            resp = await client().get(f"{CAMERA_URL}/frame", timeout=3.0)
            if resp.status_code != 200:
                await asyncio.sleep(0.5); continue
            image_bytes = resp.content

            image_b64 = base64.b64encode(image_bytes).decode("utf-8")

            # 2. 추론 요청
            inf_resp = await client().post(
                f"{INFERENCE_URL}/inference",
                files={"image": ("image.jpg", image_bytes, "image/jpeg")},
                timeout=GATEWAY_TIMEOUT
            )
            if inf_resp.status_code != 200:
                await asyncio.sleep(0.5); continue
            data = inf_resp.json()
            
            # 3. ByteTrack 트래커로 안정적 탐지 + 고유 카운팅
            raw_dets = data.get("detections", [])
            
            # 트래커 업데이트: IoU 매칭 + EMA 스무딩 + track_id 부여
            tracked_dets = _tracker.update(raw_dets)
            # 폴백: 트래커 확정 전이면 raw 스무딩 사용
            if not tracked_dets and raw_dets:
                tracked_dets = _smooth_detections(raw_dets, _tracked_detections)
            _tracked_detections = tracked_dets

            # 트래커 기반 고유 카운팅 (Background 클래스 자동 제외)
            actual_counts = _tracker.get_counts()
            
            target_counts = current_job.get("target", {})
            # require non-empty target AND all items match
            is_match = bool(target_counts) and all(
                actual_counts.get(key, 0) == expected
                for key, expected in target_counts.items()
            )
            
            # 4. Device Master 조회 (병렬, fail-open)
            enriched_items = await _enrich_with_device_info(actual_counts)
            global latest_detections
            latest_detections = enriched_items

            if is_match:
                if current_state != SystemState.MATCH:
                    last_scanned_qr = None
                    _match_achieved_at = time.monotonic()
                    asyncio.create_task(_log_inspection_round("YES MATCH", enriched_items))
                current_state = SystemState.MATCH
                mismatch_start_time = None
                # Auto-advance to next set 5s after YES MATCH
                if _match_achieved_at and time.monotonic() - _match_achieved_at >= 5.0:
                    logger.info("YES MATCH held 5s — advancing to next set")
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
                if current_state == SystemState.MATCH: current_state = SystemState.READY
                if actual_counts:
                    # Items detected but count wrong — start/continue mismatch timer
                    if mismatch_start_time is None: mismatch_start_time = time.monotonic()
                    elif time.monotonic() - mismatch_start_time >= 5.0:
                        if current_state != SystemState.ERROR:  # Trigger only on transition
                            asyncio.create_task(_trigger_snapshot(enriched_items))
                            asyncio.create_task(_log_inspection_round("NO MATCH", enriched_items))
                            last_scanned_qr = None
                            _error_achieved_at = time.monotonic()
                        current_state = SystemState.ERROR
                        # Auto-advance to next set 5s after NO MATCH is displayed
                        if _error_achieved_at and time.monotonic() - _error_achieved_at >= 5.0:
                            logger.info("NO MATCH held 5s — advancing to next set")
                            current_job = None
                            _pending_preset = None
                            current_state = SystemState.READY
                            mismatch_start_time = None
                            _error_achieved_at = None
                            last_scanned_qr = None
                            _tracker.reset()
                            asyncio.create_task(_advance_to_next_set())
                            continue
                else:
                    # Nothing detected yet — stay yellow, reset timer
                    mismatch_start_time = None

            # 5. Display Agent 업데이트 (detections 포함하여 프레임 재전송)
            border_color = {"READY": "yellow", "MATCH": "green", "ERROR": "red"}.get(current_state, "yellow")
            tray_items = enriched_items  # device_name + fda_class 포함

            # FPS 계산
            _fps_counter += 1
            fps_elapsed = time.monotonic() - _fps_start
            current_fps = _fps_counter / fps_elapsed if fps_elapsed > 0 else 0.0
            if fps_elapsed >= 5.0:  # 5초마다 리셋
                _fps_counter = 0
                _fps_start = time.monotonic()

            if display_active:
                await client().post(
                    f"{DISPLAY_URL}/frame",
                    json={"image_b64": image_b64, "detections": tracked_dets},
                    timeout=HEALTH_TIMEOUT
                )

                # NPU 메트릭
                npu_temp, inf_ready = 0.0, True
                try:
                    metrics = await _fetch_json(f"{INFERENCE_URL}/metrics")
                    npu_temp = metrics.get("npu_temp_celsius", 0.0) or 0.0
                    inf_ready = metrics.get("inference_ready", True)
                    _last_npu_temp = npu_temp  # 다음 루프에서 열 관리에 사용
                except: pass

                # 열 상태 결정
                if npu_temp >= TEMP_HOT:
                    thermal_str = "critical"
                elif npu_temp >= TEMP_WARM:
                    thermal_str = "warning"
                else:
                    thermal_str = "normal"

                hud_payload: dict = {
                    "border_color": border_color,
                    "tray_items": tray_items,
                    "ai_status": {
                        "inference_ready": inf_ready,
                        "fps": round(current_fps, 1),
                        "npu_temp_celsius": npu_temp,
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
        await asyncio.sleep(max(0.05, target_interval - elapsed))


async def _enrich_with_device_info(
    actual_counts: dict[str, int],
) -> list[dict]:
    """Device Master Agent에서 FDA 표준 기기 정보를 병렬 조회.
    실패 시 raw class_name만 포함 (fail-open)."""
    labels = list(actual_counts.keys())
    if not labels:
        return []

    async def _lookup(label: str) -> dict:
        fallback = {"class_name": label, "count": actual_counts[label]}
        try:
            resp = await client().get(
                f"{DEVICE_MASTER_URL}/device/lookup",
                params={"label": label},
                timeout=0.5,  # 빠른 실패 — 디스플레이 차단 방지
            )
            if resp.status_code == 200:
                d = resp.json()
                return {
                    "class_name": label,
                    "count": actual_counts[label],
                    "device_name": d.get("device_name"),
                    "product_code": d.get("product_code"),
                    "fda_class": d.get("device_class"),
                }
        except Exception:
            pass
        return fallback

    results = await asyncio.gather(*[_lookup(l) for l in labels], return_exceptions=True)
    return [r if isinstance(r, dict) else {"class_name": labels[i], "count": actual_counts[labels[i]]}
            for i, r in enumerate(results)]


async def _advance_to_next_set() -> None:
    """Display를 노란색으로 초기화하고 Firebase Sync에 다음 셋 전환 요청."""
    try:
        await client().post(
            f"{DISPLAY_URL}/hud",
            json={"border_color": "yellow", "tray_items": []},
            timeout=HEALTH_TIMEOUT,
        )
    except Exception:
        pass
    try:
        await client().post(f"{FIREBASE_SYNC_URL}/advance_set", timeout=5.0)
        logger.info("advance_set requested — next preset set incoming")
    except Exception as exc:
        logger.debug("advance_set error: %s", exc)


async def _log_inspection_round(result: str, detected_items: list[dict]) -> None:
    """검수 결과(YES MATCH / NO MATCH)를 Firestore 5슬롯 순환 버퍼에 기록."""
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


async def _trigger_snapshot(devices_resolved: list[dict] | None = None):
    if not current_job: return
    try:
        body: dict = {"job_id": current_job.get("id"), "reason": "timeout_mismatch"}
        if devices_resolved:
            body["devices_resolved"] = devices_resolved
        await client().post(f"{FIREBASE_SYNC_URL}/snap", json=body, timeout=3.0)
    except: pass


class ControlRequest(BaseModel):
    inference_running: bool | None = None
    camera_active: bool | None = None
    display_active: bool | None = None

@app.post("/control", summary="시스템 제어 토글 (Inference, Camera, Display)")
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


@app.post("/job/start", summary="추론 루프 재개 (Legacy)")
async def start_inference():
    global inference_running
    inference_running = True
    logger.info("Inference loop started by legacy control command")
    return {"status": "started", "inference_running": True}


@app.post("/job/stop", summary="추론 루프 일시 정지 (Legacy)")
async def stop_inference():
    global inference_running
    inference_running = False
    logger.info("Inference loop stopped by legacy control command")
    return {"status": "stopped", "inference_running": False}


@app.get("/job/status", summary="추론 루프 및 시스템 상태 조회")
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
    return JSONResponse(status_code=200, content={"status": "healthy" if all_healthy else "degraded", "modules": {"inference": inference_health, "camera": camera_health}})


@app.get("/status")
async def status_check() -> JSONResponse:
    inf_h, inf_m, cam_h = await asyncio.gather(_fetch_json(f"{INFERENCE_URL}/health"), _fetch_json(f"{INFERENCE_URL}/metrics"), _fetch_json(f"{CAMERA_URL}/health"))
    return JSONResponse(content={"gateway": "healthy", "inference": {"health": inf_h, "metrics": inf_m}, "camera": cam_h})


async def _fetch_module_health(base_url: str, name: str) -> dict[str, Any]:
    try:
        resp = await client().get(f"{base_url}/health", timeout=3.0)
        data = resp.json()
        data["reachable"] = resp.status_code == 200
        return data
    except: return {"reachable": False, "status": "unreachable"}


async def _fetch_json(url: str) -> dict[str, Any]:
    try:
        resp = await client().get(url, timeout=3.0)
        return resp.json()
    except: return {"error": "unreachable"}


if __name__ == "__main__":
    uvicorn.run("src.gateway.main:app", host="0.0.0.0", port=8000, log_level="info")

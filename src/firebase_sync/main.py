"""
main.py — Firebase Cloud Sync Agent (Port 8004)

아키텍처:
  ┌─────────────────────────────────────────────────────────────────┐
  │  FastAPI Thread (메인)                                           │
  │  ├─ POST /sync         → 이벤트 큐에 삽입 (즉시 202 반환)       │
  │  ├─ GET  /queue/status → 큐 깊이, Firebase 도달 가능 여부       │
  │  ├─ GET  /queue/item/{id} → 개별 항목 상태 (doc_id 포함)        │
  │  ├─ POST /queue/flush  → 수동 즉시 처리 트리거                  │
  │  └─ GET  /health       → 모듈 상태                              │
  └─────────────────────────────────────────────────────────────────┘
  ┌─────────────────────────────────────────────────────────────────┐
  │  Queue Worker Thread (백그라운드)                                │
  │  ├─ 5초 주기로 큐 폴링                                          │
  │  ├─ 스냅샷 3장 캡처 (0.1초 간격, 노출 보정)                     │
  │  ├─ Firebase Storage 업로드                                     │
  │  ├─ Firestore 문서 생성                                         │
  │  └─ 실패 시 지수 백오프 재시도 (최대 10회)                      │
  └─────────────────────────────────────────────────────────────────┘

보안:
  - 모든 Firebase 자격증명은 .env / Docker secrets 경유
  - 코드에 API Key / 서비스 계정 정보 절대 하드코딩 금지
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse

from src.firebase_sync.queue_manager import QueueManager
from src.firebase_sync.schemas import (
    HealthResponse,
    ItemStatus,
    QueueItemDetail,
    QueueStatusResponse,
    SyncRequest,
    SyncResponse,
)
from src.firebase_sync.snapshot import capture_snapshots
from src.firebase_sync.uploader import BaseUploader, create_uploader

# ─────────────────────────────────────────────────────────────────────────────
# 설정 (.env 로드 → 환경변수 우선)
# ─────────────────────────────────────────────────────────────────────────────

load_dotenv()   # .env에서 환경변수 로드 (이미 설정된 변수는 덮어쓰지 않음)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("firebase_sync.main")

MODULE_NAME = os.getenv("MODULE_NAME", "FirebaseSyncAgent")
DB_PATH = os.getenv("QUEUE_DB_PATH", "/app/data/queue.db")
WORKER_POLL_SEC = float(os.getenv("QUEUE_POLL_SEC", "5"))
DISPLAY_URL = os.getenv("DISPLAY_URL", "http://display_agent:8003")
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://gateway_agent:8000")

# ─────────────────────────────────────────────────────────────────────────────
# 전역 인스턴스
# ─────────────────────────────────────────────────────────────────────────────

_queue: QueueManager
_uploader: BaseUploader
_http_client: httpx.AsyncClient
_flush_event = threading.Event()   # /queue/flush로 즉시 처리 트리거
_last_control_ts: float = 0.0     # 마지막으로 처리한 device_control 명령 타임스탬프
_last_job_config_ts: float = 0.0  # 마지막으로 처리한 job_config 타임스탬프


# ─────────────────────────────────────────────────────────────────────────────
# 큐 워커 스레드
# ─────────────────────────────────────────────────────────────────────────────

def _worker_loop() -> None:
    """백그라운드 큐 워커 (별도 스레드, 자체 이벤트 루프)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    logger.info("Queue worker started (poll=%.1fs)", WORKER_POLL_SEC)

    while True:
        _flush_event.wait(timeout=WORKER_POLL_SEC)
        _flush_event.clear()
        loop.run_until_complete(_process_queue())


def _control_loop() -> None:
    """device_control 폴링 전용 스레드 — 큐 백로그에 관계없이 5초 주기 실행."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    logger.info("Control loop started (poll=%.1fs)", WORKER_POLL_SEC)

    while True:
        time.sleep(WORKER_POLL_SEC)
        loop.run_until_complete(_process_device_control())
        loop.run_until_complete(_process_job_config())
        loop.run_until_complete(_update_system_status())


async def _process_device_control() -> None:
    """Firestore device_control/rpi 문서를 폴링하여 Gateway에 명령 릴레이.
    Firebase 자격증명 없는 시뮬레이션 모드에서는 건너뜀."""
    global _last_control_ts
    if _uploader.simulation_mode:
        return
    db = getattr(_uploader, "_db", None)
    if db is None:
        return
    try:
        loop = asyncio.get_event_loop()
        doc_snap = await loop.run_in_executor(
            None,
            lambda: db.collection("device_control").document("rpi").get(),
        )
        if not doc_snap.exists:
            return
        data = doc_snap.to_dict()
        ts_value = data.get("ts")
        if ts_value is None:
            return
        ts_seconds: float = ts_value.timestamp()
        if ts_seconds <= _last_control_ts:
            return
        _last_control_ts = ts_seconds
        
        # Handle legacy command string or new boolean toggles
        if "command" in data and "inference_running" not in data:
            payload = {"inference_running": data["command"] == "start"}
        else:
            payload = {
                "inference_running": data.get("inference_running", True),
                "camera_active": data.get("camera_active", True),
                "display_active": data.get("display_active", True)
            }
            
        async with httpx.AsyncClient(timeout=3.0) as http:
            resp = await http.post(f"{GATEWAY_URL}/control", json=payload)
            logger.info(
                "Device control relayed to gateway/control %d: %s",
                resp.status_code, payload,
            )
    except Exception as exc:
        logger.debug("Device control poll error: %s", exc)


async def _process_job_config() -> None:
    """Firestore job_config/rpi를 폴링하여 Gateway에 첫 번째 Set Job 릴레이.
    sets[] + cursor 형식 지원."""
    global _last_job_config_ts
    if _uploader.simulation_mode:
        return
    db = getattr(_uploader, "_db", None)
    if db is None:
        return
    try:
        import datetime as dt
        loop = asyncio.get_event_loop()
        doc_snap = await loop.run_in_executor(
            None,
            lambda: db.collection("job_config").document("rpi").get(),
        )
        if not doc_snap.exists:
            return
        data = doc_snap.to_dict()
        ts_value = data.get("ts")
        if ts_value is None:
            return
        ts_seconds: float = ts_value.timestamp()
        if ts_seconds <= _last_job_config_ts:
            return
        _last_job_config_ts = ts_seconds

        sets = data.get("sets")
        if sets:
            cursor = int(data.get("cursor", 0)) % len(sets)
            target = sets[cursor]
            job_id = f"ADMIN-SET{cursor + 1}-{dt.datetime.utcnow().strftime('%H%M%S')}"
        else:
            target = data.get("target", {})
            job_id = f"ADMIN-{dt.datetime.utcnow().strftime('%H%M%S')}"

        if not target:
            return

        async with httpx.AsyncClient(timeout=3.0) as http:
            resp = await http.post(
                f"{GATEWAY_URL}/job",
                json={"job_id": job_id, "target": target},
            )
            logger.info(
                "Job config relayed → gateway/job %d: job_id=%s target=%s",
                resp.status_code, job_id, target,
            )
    except Exception as exc:
        logger.debug("Job config poll error: %s", exc)


async def _do_advance_set() -> None:
    """Firestore job_config/rpi 커서를 +1 하고 다음 Set을 Gateway에 전송."""
    if _uploader.simulation_mode:
        return
    db = getattr(_uploader, "_db", None)
    if db is None:
        return
    try:
        import datetime as dt
        loop = asyncio.get_event_loop()

        def _advance():
            doc_ref = db.collection("job_config").document("rpi")
            snap = doc_ref.get()
            if not snap.exists:
                return None
            data = snap.to_dict()
            sets = data.get("sets", [])
            if not sets:
                return None
            old_cursor = int(data.get("cursor", 0)) % len(sets)
            new_cursor = (old_cursor + 1) % len(sets)
            doc_ref.update({"cursor": new_cursor})
            return {"sets": sets, "cursor": new_cursor}

        result = await loop.run_in_executor(None, _advance)
        if not result:
            return

        sets = result["sets"]
        cursor = result["cursor"]
        target = sets[cursor]
        if not target:
            return

        import datetime as dt
        job_id = f"ADMIN-SET{cursor + 1}-{dt.datetime.utcnow().strftime('%H%M%S')}"
        async with httpx.AsyncClient(timeout=3.0) as http:
            resp = await http.post(
                f"{GATEWAY_URL}/job",
                json={"job_id": job_id, "target": target},
            )
            logger.info(
                "Advanced → Set %d, gateway/job %d: job_id=%s",
                cursor + 1, resp.status_code, job_id,
            )
    except Exception as exc:
        logger.debug("_do_advance_set error: %s", exc)


async def _update_system_status() -> None:
    """Gateway 상태를 Firestore system_status/rpi에 주기적으로 동기화."""
    if _uploader.simulation_mode:
        return
    db = getattr(_uploader, "_db", None)
    if db is None:
        return
    try:
        from google.cloud.firestore_v1 import SERVER_TIMESTAMP  # type: ignore[import]
        async with httpx.AsyncClient(timeout=3.0) as http:
            resp = await http.get(f"{GATEWAY_URL}/job/status")
            if resp.status_code != 200:
                return
            data = resp.json()
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: db.collection("system_status").document("rpi").set({
                "inference_running": data.get("inference_running", True),
                "camera_active": data.get("camera_active", True),
                "display_active": data.get("display_active", True),
                "system_state": data.get("system_state"),
                "current_job": data.get("current_job"),
                "latest_detections": data.get("latest_detections", []),
                "updated_at": SERVER_TIMESTAMP,
            }),
        )
    except Exception as exc:
        logger.debug("System status update error: %s", exc)


async def _process_queue() -> None:
    """큐에서 ready 항목 꺼내 업로드 처리."""
    items = _queue.dequeue_ready()
    if not items:
        return

    async with httpx.AsyncClient(timeout=10.0) as client:
        for item in items:
            logger.info("Processing queue item #%d (%s)", item.id, item.event_type)
            try:
                # 스냅샷 캡처: MISMATCH / ALERT 이벤트만
                shots: list[dict] = []
                if item.event_type in ("mismatch", "alert"):
                    shots = await capture_snapshots(client)

                # Firebase 업로드
                doc_id, storage_urls = await _uploader.upload_event(
                    item.payload, shots
                )
                _queue.mark_done(item.id, doc_id, storage_urls)
                logger.info(
                    "Item #%d done — doc_id=%s, %d snapshots",
                    item.id, doc_id, len(shots),
                )
            except Exception as exc:
                logger.exception("Item #%d failed: %s", item.id, exc)
                _queue.mark_failed(item.id, str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global _queue, _uploader, _http_client

    _queue = QueueManager(db_path=DB_PATH)
    _uploader = create_uploader()
    _http_client = httpx.AsyncClient(timeout=10.0)

    # 워커 스레드 시작
    worker_thread = threading.Thread(
        target=_worker_loop, name="queue-worker", daemon=True
    )
    worker_thread.start()

    control_thread = threading.Thread(
        target=_control_loop, name="control-worker", daemon=True
    )
    control_thread.start()

    logger.info(
        "Firebase Sync Agent started — simulation=%s, db=%s",
        _uploader.simulation_mode, DB_PATH,
    )
    yield

    await _http_client.aclose()
    logger.info("Firebase Sync Agent shutdown")


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI 앱
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Firebase Sync Agent API",
    description="오프라인 내성 큐 + Firebase 클라우드 동기화",
    version="1.0.0",
    lifespan=lifespan,
)


# ─────────────────────────────────────────────────────────────────────────────
# 엔드포인트
# ─────────────────────────────────────────────────────────────────────────────

@app.post(
    "/sync",
    response_model=SyncResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="동기화 이벤트 큐에 삽입",
    description=(
        "탐지 불일치·경고 이벤트를 로컬 큐에 즉시 삽입합니다.\n\n"
        "네트워크가 끊겨도 데이터는 SQLite에 안전하게 보관되며 "
        "연결 복구 시 자동 재전송됩니다."
    ),
)
async def sync_event(body: SyncRequest) -> SyncResponse:
    payload = body.model_dump()
    event_id = _queue.enqueue(body.event_type.value, payload)
    _flush_event.set()   # 워커 즉시 깨우기

    return SyncResponse(
        event_id=event_id,
        status="queued",
        message=f"Event #{event_id} queued for Firebase upload",
    )


@app.get(
    "/queue/status",
    response_model=QueueStatusResponse,
    summary="큐 상태 조회",
)
async def queue_status() -> QueueStatusResponse:
    counts = _queue.counts()
    reachable = await _uploader.is_reachable()
    return QueueStatusResponse(
        total_pending=counts.get("pending", 0) + counts.get("processing", 0),
        total_processing=counts.get("processing", 0),
        total_done=counts.get("done", 0),
        total_failed=counts.get("failed", 0),
        firebase_reachable=reachable,
        simulation_mode=_uploader.simulation_mode,
    )


@app.get(
    "/queue/item/{event_id}",
    response_model=QueueItemDetail,
    summary="개별 큐 항목 상태 조회",
    description="event_id로 항목을 조회합니다. `status=done`이면 `firestore_doc_id`가 채워집니다.",
)
async def queue_item(event_id: int) -> QueueItemDetail:
    item = _queue.get_item(event_id)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Queue item #{event_id} not found",
        )
    import datetime as dt
    return QueueItemDetail(
        event_id=item.id,
        event_type=item.event_type,
        status=ItemStatus(item.status),
        retry_count=item.retry_count,
        firestore_doc_id=item.firestore_doc_id,
        storage_urls=item.storage_urls,
        created_at=dt.datetime.fromtimestamp(item.created_at).isoformat(),
        error_message=item.error_message,
    )


@app.post(
    "/log_round",
    status_code=status.HTTP_202_ACCEPTED,
    summary="검수 라운드 Firestore 기록 (5슬롯 순환 버퍼)",
)
async def log_round(body: dict) -> JSONResponse:
    asyncio.create_task(_write_inspection_round(body))
    return JSONResponse({"status": "queued"})


async def _write_inspection_round(round_data: dict) -> None:
    """Firestore inspection_log/rpi 문서에 5슬롯 순환 버퍼로 기록."""
    if _uploader.simulation_mode:
        return
    db = getattr(_uploader, "_db", None)
    if db is None:
        return
    try:
        import datetime as dt
        from google.cloud.firestore_v1 import SERVER_TIMESTAMP  # type: ignore[import]
        loop = asyncio.get_event_loop()

        def _write():
            doc_ref = db.collection("inspection_log").document("rpi")
            snap = doc_ref.get()
            if snap.exists:
                data = snap.to_dict()
                slots = list(data.get("slots", [None] * 5))
                cursor = int(data.get("cursor", 0)) % 5
            else:
                slots = [None] * 5
                cursor = 0
            # Ensure list is exactly 5 elements
            while len(slots) < 5:
                slots.append(None)

            slots[cursor] = {
                **round_data,
                "slot_index": cursor,
                "logged_at": dt.datetime.utcnow().isoformat() + "Z",
            }
            doc_ref.set({
                "slots": slots,
                "cursor": (cursor + 1) % 5,
                "updated_at": SERVER_TIMESTAMP,
            })
            logger.info("Inspection round logged → slot %d, result=%s", cursor, round_data.get("result"))

        await loop.run_in_executor(None, _write)
    except Exception as exc:
        logger.debug("_write_inspection_round error: %s", exc)


@app.post(
    "/advance_set",
    status_code=status.HTTP_202_ACCEPTED,
    summary="다음 프리셋 셋으로 커서 전진 및 Gateway에 새 Job 전송",
)
async def advance_set() -> JSONResponse:
    asyncio.create_task(_do_advance_set())
    return JSONResponse({"status": "advancing"})


@app.post(
    "/snap",
    status_code=status.HTTP_202_ACCEPTED,
    summary="ERROR 상태 스냅샷 즉시 촬영 트리거",
    description=(
        "Gateway가 5초 불일치(ERROR 상태) 감지 시 호출합니다.\n"
        "스냅샷 3장을 즉시 캡처하여 Firebase에 업로드합니다."
    ),
)
async def trigger_snap(body: dict) -> JSONResponse:
    job_id = body.get("job_id", "unknown")
    reason = body.get("reason", "timeout_mismatch")
    metadata: dict = {"job_id": job_id, "reason": reason, "trigger": "gateway_error_state"}
    if devices_resolved := body.get("devices_resolved"):
        metadata["devices_resolved"] = devices_resolved
    payload = {
        "event_type": "mismatch",
        "expected_count": 0,
        "actual_count": 0,
        "missing_items": [],
        "detected_items": [],
        "metadata": metadata,
    }
    event_id = _queue.enqueue("mismatch", payload)
    _flush_event.set()   # 워커 즉시 깨우기
    logger.info("Snap triggered — job_id=%s, reason=%s, event_id=%d", job_id, reason, event_id)
    return JSONResponse({"event_id": event_id, "status": "queued", "job_id": job_id})


@app.post(
    "/queue/flush",
    summary="큐 즉시 처리 트리거",
    description="대기 중인 큐 항목을 즉시 처리합니다 (폴링 주기 무시).",
)
async def flush_queue() -> JSONResponse:
    _flush_event.set()
    return JSONResponse({"status": "flush triggered"})


@app.get(
    "/health",
    response_model=HealthResponse,
    summary="헬스 체크",
)
async def health_check() -> HealthResponse:
    counts = _queue.counts()
    pending = counts.get("pending", 0) + counts.get("processing", 0)
    reachable = await _uploader.is_reachable()
    return HealthResponse(
        status="healthy",
        module=MODULE_NAME,
        firebase_configured=not _uploader.simulation_mode,
        simulation_mode=_uploader.simulation_mode,
        queue_depth=pending,
        firebase_reachable=reachable,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 엔트리포인트
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "src.firebase_sync.main:app",
        host="0.0.0.0",
        port=8004,
        workers=1,
        log_level="info",
    )

"""
main.py — Firebase Cloud Sync Agent (Port 8004)

Architecture:
  FastAPI Thread (main)
  ├─ POST /sync         → insert event to queue (immediate 202 return)
  ├─ GET  /queue/status → queue depth, Firebase reachability
  ├─ GET  /queue/item/{id} → individual item status (doc_id included)
  ├─ POST /queue/flush  → manual immediate processing trigger
  └─ GET  /health       → module status

  Queue Worker Thread (background)
  ├─ polls queue every 5s
  ├─ captures 3 snapshots (0.1s interval, exposure bracketed)
  ├─ uploads to Firebase Storage
  ├─ creates Firestore document
  └─ exponential backoff retry on failure (max 10 retries)

Security:
  - All Firebase credentials via .env / Docker secrets
  - No API keys or service account info hardcoded
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
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
# Config
# ─────────────────────────────────────────────────────────────────────────────

load_dotenv()

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
HEARTBEAT_SEC = float(os.getenv("HEARTBEAT_SEC", "30"))
VERSION = os.getenv("VERSION", "1.0.0")

# ── Device Identity ────────────────────────────────────────────────────────────
_VALID_APP_IDS = {"surgical", "od", "inventory", "inventory_count", "unassigned"}
_DEVICE_ID_PATTERN = re.compile(r'^[a-zA-Z0-9\-]{1,64}$')

APP_ID: str = os.getenv("APP_ID", "").strip()
DEVICE_ID: str = os.getenv("DEVICE_ID", "").strip()
PROJECT_ID: str = os.getenv("PROJECT_ID", "").strip()


def _validate_identity() -> bool:
    """Validate APP_ID and DEVICE_ID. Returns True if both are valid."""
    ok = True
    if APP_ID == "unassigned":
        logger.info("Bootstrap mode — device not yet assigned to a project (app_id=unassigned)")
    elif APP_ID not in _VALID_APP_IDS:
        logger.critical(
            "APP_ID=%r is missing or invalid. Valid values: %s. "
            "Firestore writes are DISABLED — running in simulation mode.",
            APP_ID, sorted(_VALID_APP_IDS),
        )
        ok = False
    if not DEVICE_ID or not _DEVICE_ID_PATTERN.match(DEVICE_ID):
        logger.critical(
            "DEVICE_ID=%r is missing or invalid. "
            "Must be 1-64 alphanumeric/hyphen characters. "
            "Firestore writes are DISABLED — running in simulation mode.",
            DEVICE_ID,
        )
        ok = False
    if ok:
        logger.info("Device identity validated — app_id=%r device_id=%r", APP_ID, DEVICE_ID)
    return ok


_identity_valid: bool = False

# ─────────────────────────────────────────────────────────────────────────────
# Global instances
# ─────────────────────────────────────────────────────────────────────────────

_queue: QueueManager
_uploader: BaseUploader
_http_client: httpx.AsyncClient
_flush_event = threading.Event()
_stop_heartbeat = threading.Event()
_last_control_ts: float = 0.0
_last_job_config_ts: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Queue worker thread
# ─────────────────────────────────────────────────────────────────────────────

def _worker_loop() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    logger.info("Queue worker started (poll=%.1fs)", WORKER_POLL_SEC)

    while True:
        _flush_event.wait(timeout=WORKER_POLL_SEC)
        _flush_event.clear()
        loop.run_until_complete(_process_queue())


def _control_loop() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    logger.info("Control loop started (poll=%.1fs)", WORKER_POLL_SEC)

    while True:
        time.sleep(WORKER_POLL_SEC)
        loop.run_until_complete(_process_device_control())
        loop.run_until_complete(_process_job_config())
        loop.run_until_complete(_update_system_status())


async def _process_device_control() -> None:
    """Poll Firestore device_control/{DEVICE_ID} and relay commands to Gateway. Skipped in simulation mode."""
    global _last_control_ts, PROJECT_ID
    if _uploader.simulation_mode:
        return
    db = getattr(_uploader, "_db", None)
    if db is None:
        return
    try:
        loop = asyncio.get_event_loop()
        doc_snap = await loop.run_in_executor(
            None,
            lambda: db.collection("device_control").document(DEVICE_ID).get(),
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

        # Project reassignment reset — clear gateway state and update system_status
        if data.get("reset") is True:
            new_project_id = data.get("project_id", "")
            new_app_id = data.get("app_id", "")
            new_compose_file = data.get("compose_file", "")
            new_hef_model = data.get("hef_model", "")

            # Propagate project_id so subsequent Firestore docs are stamped correctly
            PROJECT_ID = new_project_id
            from src.firebase_sync.uploader import set_project_id
            set_project_id(new_project_id)

            logger.info(
                "Project reassignment reset — project_id=%r app_id=%r compose=%r model=%r",
                new_project_id, new_app_id, new_compose_file, new_hef_model,
            )

            # In bootstrap mode (APP_ID=unassigned) write assignment file for launcher.sh;
            # skip gateway POST since no gateway is running in the bootstrap stack.
            if APP_ID == "unassigned" and new_app_id and new_compose_file:
                import json as _json
                assignment_path = os.path.join(
                    os.path.dirname(DB_PATH), "project_assignment.json"
                )
                assignment = {
                    "app_id": new_app_id,
                    "hef_model": new_hef_model,
                    "compose_file": new_compose_file,
                    "project_id": new_project_id,
                }
                try:
                    with open(assignment_path, "w") as _f:
                        _json.dump(assignment, _f)
                    logger.info("Project assignment written → %s", assignment_path)
                except OSError as exc:
                    logger.warning("Failed to write project_assignment.json: %s", exc)
            else:
                # Normal mode — gateway is running; send reset signal
                try:
                    async with httpx.AsyncClient(timeout=3.0) as http:
                        await http.post(
                            f"{GATEWAY_URL}/job",
                            json={"job_id": "PROJECT-RESET", "target": {}},
                        )
                        logger.info("Gateway reset: POST /job with empty target sent")
                except Exception as exc:
                    logger.warning("Gateway reset failed: %s", exc)

            if new_project_id:
                _db = getattr(_uploader, "_db", None)
                if _db:
                    try:
                        from google.cloud.firestore_v1 import SERVER_TIMESTAMP  # type: ignore[import]
                        _loop = asyncio.get_event_loop()
                        await _loop.run_in_executor(
                            None,
                            lambda: _db.collection("system_status").document(DEVICE_ID).set(
                                {"project_id": new_project_id, "updated_at": SERVER_TIMESTAMP},
                                merge=True,
                            ),
                        )
                    except Exception as exc:
                        logger.debug("project_id status update failed: %s", exc)
            return

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
    """Poll Firestore job_config/{DEVICE_ID} and relay preset to Gateway."""
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
            lambda: db.collection("job_config").document(DEVICE_ID).get(),
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
    """Increment Firestore job_config/{DEVICE_ID} cursor by 1 and send next Set to Gateway."""
    if _uploader.simulation_mode:
        return
    db = getattr(_uploader, "_db", None)
    if db is None:
        return
    try:
        import datetime as dt
        loop = asyncio.get_event_loop()

        def _advance():
            doc_ref = db.collection("job_config").document(DEVICE_ID)
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
        entry = sets[cursor]
        if not entry:
            return

        import datetime as dt
        if isinstance(entry, dict) and "target" in entry:
            job_id = entry.get("job_id") or f"SET{cursor + 1}-{dt.datetime.utcnow().strftime('%H%M%S')}"
            target = entry["target"]
        else:
            job_id = f"SET{cursor + 1}-{dt.datetime.utcnow().strftime('%H%M%S')}"
            target = entry
        if not target:
            return
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
    """Periodically sync Gateway status to Firestore system_status/{DEVICE_ID}."""
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
            lambda: db.collection("system_status").document(DEVICE_ID).set({
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


# ─────────────────────────────────────────────────────────────────────────────
# Device Registry
# ─────────────────────────────────────────────────────────────────────────────

async def _register_device() -> None:
    """Write/update devices/{DEVICE_ID} in Firestore. Preserves registered_at if doc exists."""
    if _uploader.simulation_mode:
        return
    db = getattr(_uploader, "_db", None)
    if db is None:
        return
    try:
        from google.cloud.firestore_v1 import SERVER_TIMESTAMP  # type: ignore[import]
        loop = asyncio.get_event_loop()

        def _write():
            doc_ref = db.collection("devices").document(DEVICE_ID)
            existing = doc_ref.get()
            payload: dict = {
                "app_id": APP_ID,
                "device_id": DEVICE_ID,
                "status": "pending" if APP_ID == "unassigned" else "online",
                "last_seen": SERVER_TIMESTAMP,
                "version": VERSION,
                "simulation_mode": _uploader.simulation_mode,
            }
            if not existing.exists:
                payload["registered_at"] = SERVER_TIMESTAMP
                doc_ref.set(payload)
                logger.info("Device registered → devices/%s", DEVICE_ID)
            else:
                doc_ref.set(payload, merge=True)
                logger.info("Device re-registered (online) → devices/%s", DEVICE_ID)

        await loop.run_in_executor(None, _write)
    except Exception as exc:
        logger.warning("Device registration failed: %s", exc)


async def _update_device_heartbeat() -> None:
    """Update last_seen and status for devices/{DEVICE_ID}."""
    if _uploader.simulation_mode:
        return
    db = getattr(_uploader, "_db", None)
    if db is None:
        return
    try:
        from google.cloud.firestore_v1 import SERVER_TIMESTAMP  # type: ignore[import]
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: db.collection("devices").document(DEVICE_ID).set(
                {"status": "online", "last_seen": SERVER_TIMESTAMP},
                merge=True,
            ),
        )
    except Exception as exc:
        logger.debug("Heartbeat update failed: %s", exc)


async def _set_device_offline() -> None:
    """Mark devices/{DEVICE_ID} status as offline on shutdown."""
    if _uploader.simulation_mode:
        return
    db = getattr(_uploader, "_db", None)
    if db is None:
        return
    try:
        from google.cloud.firestore_v1 import SERVER_TIMESTAMP  # type: ignore[import]
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: db.collection("devices").document(DEVICE_ID).set(
                {"status": "offline", "last_seen": SERVER_TIMESTAMP},
                merge=True,
            ),
        )
        logger.info("Device marked offline → devices/%s", DEVICE_ID)
    except Exception as exc:
        logger.debug("Device offline mark failed: %s", exc)


def _heartbeat_loop() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    logger.info("Heartbeat loop started (interval=%.0fs)", HEARTBEAT_SEC)
    while not _stop_heartbeat.wait(timeout=HEARTBEAT_SEC):
        loop.run_until_complete(_update_device_heartbeat())


async def _process_queue() -> None:
    items = _queue.dequeue_ready()
    if not items:
        return

    async with httpx.AsyncClient(timeout=10.0) as client:
        for item in items:
            logger.info("Processing queue item #%d (%s)", item.id, item.event_type)
            try:
                shots: list[dict] = []
                if item.event_type in ("mismatch", "alert"):
                    shots = await capture_snapshots(client)

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
    global _queue, _uploader, _http_client, _identity_valid

    _identity_valid = _validate_identity()

    _queue = QueueManager(db_path=DB_PATH)
    _uploader = create_uploader()

    # Force simulation mode if identity is invalid — prevents unidentified writes
    if not _identity_valid and not _uploader.simulation_mode:
        from src.firebase_sync.uploader import SimulationUploader
        _uploader = SimulationUploader()
        logger.critical("Identity invalid — overriding uploader to SimulationUploader")

    _http_client = httpx.AsyncClient(timeout=10.0)

    worker_thread = threading.Thread(
        target=_worker_loop, name="queue-worker", daemon=True
    )
    worker_thread.start()

    control_thread = threading.Thread(
        target=_control_loop, name="control-worker", daemon=True
    )
    control_thread.start()

    _stop_heartbeat.clear()
    heartbeat_thread = threading.Thread(
        target=_heartbeat_loop, name="heartbeat", daemon=True
    )
    heartbeat_thread.start()

    await _register_device()

    logger.info(
        "Firebase Sync Agent started — app_id=%r device_id=%r simulation=%s db=%s",
        APP_ID, DEVICE_ID, _uploader.simulation_mode, DB_PATH,
    )
    yield

    _stop_heartbeat.set()
    await _set_device_offline()
    await _http_client.aclose()
    logger.info("Firebase Sync Agent shutdown")


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Firebase Sync Agent API",
    description="Offline-resilient queue + Firebase cloud sync",
    version="1.0.0",
    lifespan=lifespan,
)


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@app.post(
    "/sync",
    response_model=SyncResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Insert sync event into queue",
)
async def sync_event(body: SyncRequest) -> SyncResponse:
    payload = body.model_dump()
    event_id = _queue.enqueue(body.event_type.value, payload)
    _flush_event.set()

    return SyncResponse(
        event_id=event_id,
        status="queued",
        message=f"Event #{event_id} queued for Firebase upload",
    )


@app.get("/queue/status", response_model=QueueStatusResponse, summary="Queue status")
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
    summary="Individual queue item status",
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
    summary="Log inspection round to Firestore (5-slot circular buffer)",
)
async def log_round(body: dict) -> JSONResponse:
    asyncio.create_task(_write_inspection_round(body))
    return JSONResponse({"status": "queued"})


async def _write_inspection_round(round_data: dict) -> None:
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
            doc_ref = db.collection("inspection_log").document(DEVICE_ID)
            snap = doc_ref.get()
            if snap.exists:
                data = snap.to_dict()
                slots = list(data.get("slots", [None] * 5))
                cursor = int(data.get("cursor", 0)) % 5
            else:
                slots = [None] * 5
                cursor = 0
            while len(slots) < 5:
                slots.append(None)

            logged_at = dt.datetime.utcnow().isoformat() + "Z"
            entry = {
                **round_data,
                "app_id": APP_ID,
                "device_id": DEVICE_ID,
                "project_id": PROJECT_ID,
                "slot_index": cursor,
                "logged_at": logged_at,
            }
            slots[cursor] = entry
            doc_ref.set({
                "app_id": APP_ID,
                "device_id": DEVICE_ID,
                "project_id": PROJECT_ID,
                "slots": slots,
                "cursor": (cursor + 1) % 5,
                "updated_at": SERVER_TIMESTAMP,
            })

            # Append to operations subcollection (unbounded history)
            db.collection("operations").add({
                **entry,
                "timestamp": SERVER_TIMESTAMP,
            })
            logger.info("Inspection round logged → slot %d, result=%s", cursor, round_data.get("result"))

        await loop.run_in_executor(None, _write)
    except Exception as exc:
        logger.debug("_write_inspection_round error: %s", exc)


@app.post(
    "/advance_set",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Advance cursor to next preset set and send new job to Gateway",
)
async def advance_set() -> JSONResponse:
    asyncio.create_task(_do_advance_set())
    return JSONResponse({"status": "advancing"})


@app.post(
    "/load_current_set",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Load current preset set and send to Gateway (no cursor advance)",
)
async def load_current_set() -> JSONResponse:
    asyncio.create_task(_do_load_current_set())
    return JSONResponse({"status": "loading"})


async def _do_load_current_set() -> None:
    """Read current Firestore preset set (without advancing cursor) and send to Gateway."""
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
            lambda: db.collection("job_config").document(DEVICE_ID).get(),
        )
        if not doc_snap.exists:
            return
        data = doc_snap.to_dict()
        sets = data.get("sets")
        if sets:
            cursor = int(data.get("cursor", 0)) % len(sets)
            entry = sets[cursor]
            # Support {"job_id": "TRAY-001", "target": {...}} or plain target dict
            if isinstance(entry, dict) and "target" in entry:
                job_id = entry.get("job_id") or f"SET{cursor + 1}-{dt.datetime.utcnow().strftime('%H%M%S')}"
                target = entry["target"]
            else:
                job_id = f"SET{cursor + 1}-{dt.datetime.utcnow().strftime('%H%M%S')}"
                target = entry
        else:
            target = data.get("target", {})
            job_id = f"JOB-{dt.datetime.utcnow().strftime('%H%M%S')}"
        if not target:
            return
        async with httpx.AsyncClient(timeout=3.0) as http:
            await http.post(f"{GATEWAY_URL}/job", json={"job_id": job_id, "target": target})
        logger.info("Current set loaded → gateway/job: job_id=%s target=%s", job_id, target)
    except Exception as exc:
        logger.debug("_do_load_current_set error: %s", exc)


@app.post(
    "/snap",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger immediate ERROR state snapshot capture",
)
async def trigger_snap(body: dict) -> JSONResponse:
    job_id = body.get("job_id", "unknown")
    reason = body.get("reason", "timeout_mismatch")
    target = body.get("target", {})
    detected_items = body.get("detected_items", [])

    # Build actual/expected counts from the data gateway sends
    actual_counts: dict[str, int] = {}
    for item in detected_items:
        name = item.get("class_name") or item.get("name", "")
        if name:
            actual_counts[name] = actual_counts.get(name, 0) + item.get("count", 1)
    expected_count = target.get("total", sum(target.values())) if target else 0
    actual_count = sum(actual_counts.values())

    metadata: dict = {
        "job_id": job_id,
        "reason": reason,
        "trigger": "gateway_error_state",
        "target": target,
        "detected": actual_counts,
    }
    if devices_resolved := body.get("devices_resolved"):
        metadata["devices_resolved"] = devices_resolved
    payload = {
        "event_type": "mismatch",
        "expected_count": expected_count,
        "actual_count": actual_count,
        "missing_items": [],
        "detected_items": detected_items,
        "metadata": metadata,
    }
    event_id = _queue.enqueue("mismatch", payload)
    _flush_event.set()
    logger.info("Snap triggered — job_id=%s, reason=%s, event_id=%d", job_id, reason, event_id)
    return JSONResponse({"event_id": event_id, "status": "queued", "job_id": job_id})


@app.post("/queue/flush", summary="Trigger immediate queue processing")
async def flush_queue() -> JSONResponse:
    _flush_event.set()
    return JSONResponse({"status": "flush triggered"})


@app.get("/health", response_model=HealthResponse, summary="Health check")
async def health_check() -> HealthResponse:
    counts = _queue.counts()
    pending = counts.get("pending", 0) + counts.get("processing", 0)
    reachable = await _uploader.is_reachable()
    return HealthResponse(
        status="healthy" if _identity_valid else "degraded",
        module=MODULE_NAME,
        firebase_configured=not _uploader.simulation_mode,
        simulation_mode=_uploader.simulation_mode,
        queue_depth=pending,
        firebase_reachable=reachable,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "src.firebase_sync.main:app",
        host="0.0.0.0",
        port=8004,
        workers=1,
        log_level="info",
    )

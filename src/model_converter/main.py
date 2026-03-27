"""main.py — Model Converter Agent FastAPI service.

Port: 8010
Runs ONLY on Ubuntu x86 with Hailo Dataflow Compiler installed.

Endpoints:
  GET  /health          — service health + dependency availability
  POST /convert/trigger — manually trigger a pending job by model_id
  GET  /convert/{model_id}/status — query conversion status
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import firebase_admin
import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException

from .agent import run_conversion
from .firebase_init import ensure_initialized
from .firestore_client import append_log, poll_pending_jobs, set_status, reset_stale_jobs
from .schemas import ConversionJobRequest, ConversionJobStatus, HealthResponse
from .storage_client import download_model, upload_hef

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

POLL_INTERVAL_SEC = float(os.environ.get("POLL_INTERVAL_SEC", "10"))
HAILO_SHARED_DIR  = os.environ.get("HAILO_SHARED_DIR", "").rstrip("/")

# Track which jobs are currently being processed (in-memory; single process)
_active_jobs: set[str] = set()


# ─────────────────────────────────────────────────────────────────────────────
# Background poller
# ─────────────────────────────────────────────────────────────────────────────

async def _poll_loop() -> None:
    logger.info("Conversion poller started (interval=%.0fs)", POLL_INTERVAL_SEC)
    while True:
        try:
            jobs = poll_pending_jobs()
            for job in jobs:
                model_id = job.get("model_id")
                if model_id and model_id not in _active_jobs:
                    _active_jobs.add(model_id)
                    asyncio.get_running_loop().run_in_executor(
                        None, _run_job, job
                    )
        except Exception as exc:
            logger.warning("Poller error: %s", exc)
        await asyncio.sleep(POLL_INTERVAL_SEC)


def _run_job(job: dict[str, Any]) -> None:
    model_id = job["model_id"]
    try:
        set_status(model_id, "processing")
        append_log(model_id, "Conversion started")

        storage_path = job.get("storage_raw_path", "")
        original_format = job.get("original_format", "pt")
        hw_arch = job.get("hw_arch", "hailo8")
        # model_name may be stored as either "model_name" or "name"
        display_name = job.get("model_name") or job.get("name") or model_id
        net_name = re.sub(r"[^a-zA-Z0-9_]", "_", display_name)[:32]

        # Use HAILO_SHARED_DIR as work_dir so files are accessible inside SW Suite container
        import shutil
        if HAILO_SHARED_DIR:
            work_dir = os.path.join(HAILO_SHARED_DIR, model_id)
            os.makedirs(work_dir, exist_ok=True)
            cleanup = lambda: shutil.rmtree(work_dir, ignore_errors=True)
        else:
            _tmp = tempfile.mkdtemp(prefix=f"conv_{model_id}_")
            work_dir = _tmp
            cleanup = lambda: shutil.rmtree(_tmp, ignore_errors=True)

        local_src = os.path.join(work_dir, f"original.{original_format}")
        append_log(model_id, f"Downloading from {storage_path}")
        download_model(storage_path, local_src)

        def on_log(msg: str) -> None:
            append_log(model_id, msg[:500])

        result = run_conversion(
            file_path=local_src,
            hw_arch=hw_arch,
            work_dir=work_dir,
            on_log=on_log,
        )

        if not result.get("ok"):
            error = result.get("error", "Unknown error")
            logger.error("Job %s FAILED: %s", model_id, error[:2000])
            append_log(model_id, f"FAILED: {error[:500]}")
            set_status(model_id, "failed", error_message=error[:1000])
            logger.info("Work dir preserved for debugging: %s", work_dir)
            return

        hef_path = result["hef_path"]
        append_log(model_id, f"Uploading HEF: {Path(hef_path).name}")
        hef_url = upload_hef(hef_path, model_id, net_name)

        set_status(
            model_id,
            "ready",
            hef_download_url=hef_url,
            class_names=result.get("class_names", []),
            input_resolution=result.get("input_resolution", 640),
        )
        append_log(model_id, "Conversion complete")
        logger.info("Job %s completed → %s", model_id, hef_url)
        cleanup()  # only cleanup on success

    except Exception as exc:
        logger.exception("Job %s failed with exception", model_id)
        append_log(model_id, f"EXCEPTION: {str(exc)[:500]}")
        set_status(model_id, "failed", error_message=str(exc)[:1000])
    finally:
        _active_jobs.discard(model_id)


# ─────────────────────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_initialized()
    # Reset any jobs left in "processing" from a previous converter crash
    reset_stale_jobs()
    task = asyncio.create_task(_poll_loop())
    yield
    task.cancel()


app = FastAPI(title="Model Converter Agent", lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
def health():
    hailo_ok = False
    try:
        import subprocess
        r = subprocess.run(["hailo", "--version"], capture_output=True, timeout=5)
        hailo_ok = r.returncode == 0
    except Exception:
        pass

    ultralytics_ok = False
    try:
        import ultralytics  # noqa: F401
        ultralytics_ok = True
    except ImportError:
        pass

    firebase_ok = bool(firebase_admin._apps)

    return HealthResponse(
        status="ok",
        hailo_available=hailo_ok,
        ultralytics_available=ultralytics_ok,
        firebase_connected=firebase_ok,
        active_jobs=len(_active_jobs),
    )


@app.post("/convert/trigger")
def trigger_job(req: ConversionJobRequest, background_tasks: BackgroundTasks):
    if req.model_id in _active_jobs:
        return {"queued": False, "reason": "already processing"}
    _active_jobs.add(req.model_id)
    job = req.model_dump()
    background_tasks.add_task(_run_job, job)
    return {"queued": True, "model_id": req.model_id}


@app.get("/convert/{model_id}/status", response_model=ConversionJobStatus)
def get_status(model_id: str):
    from .firestore_client import _db
    try:
        db = _db()
        doc = db.collection("models").document(model_id).get()
        if not doc.exists:
            raise HTTPException(status_code=404, detail="model not found")
        data = doc.to_dict()
        return ConversionJobStatus(
            model_id=model_id,
            conversion_status=data.get("conversion_status", "pending_conversion"),
            conversion_log=data.get("conversion_log", []),
            hef_download_url=data.get("hef_download_url"),
            error_message=data.get("error_message"),
            class_names=data.get("class_names"),
            input_resolution=data.get("input_resolution"),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


if __name__ == "__main__":
    uvicorn.run("src.model_converter.main:app", host="0.0.0.0", port=8010, reload=False)

"""
main.py — Device Master Agent (Port 8005)

Fetches device catalog from Digioptics Firebase (Application DB) and provides
YOLO detection label → standard device info lookup.

The Edge Device is fully decoupled from hospital-internal networks.
All device data flows: Digioptics Cloud DB → Firebase → this agent → Gateway.

Endpoints:
  GET  /health                — module status + cache info
  GET  /device/lookup?label=  — single label info lookup
  GET  /device/labels         — full cache list
  POST /device/refresh        — force Firebase re-fetch
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse

from src.device_master import cache
from src.device_master.schemas import CacheStatus, DeviceLookupResponse, HealthResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("device_master.main")

MODULE_NAME = os.getenv("MODULE_NAME", "DeviceMasterAgent")
APP_ID = os.getenv("APP_ID", "unknown")
DEVICE_ID = os.getenv("DEVICE_ID", "unknown")

# Customer MDM Configuration
CUSTOMER_MDM_URL = os.getenv("CUSTOMER_MDM_URL")
CUSTOMER_MDM_API_KEY = os.getenv("CUSTOMER_MDM_API_KEY")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Device Master Agent starting — building cache from Firebase catalog...")
    await cache.build()
    logger.info("Cache ready: %d labels", len(cache.all_entries()))
    yield
    logger.info("Device Master Agent shutdown")


app = FastAPI(
    title="Device Master Agent API",
    description=(
        "Surgical instrument info lookup via Digioptics Firebase catalog. "
        "Edge device is fully decoupled from hospital-internal networks."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    reachable = await _check_firebase()
    return HealthResponse(
        status="healthy" if cache.is_loaded() else "degraded",
        module=MODULE_NAME,
        app_id=APP_ID,
        device_id=DEVICE_ID,
        cache=CacheStatus(
            loaded=cache.is_loaded(),
            label_count=len(cache.all_entries()),
            cache_age_hours=cache.cache_age_hours(),
            firebase_reachable=reachable,
        ),
    )


@app.get(
    "/device/lookup",
    response_model=DeviceLookupResponse,
    summary="YOLO label → device info lookup",
)
async def lookup_device(label: str) -> DeviceLookupResponse:
    """
    label: YOLO detection label (e.g. "forceps", "scalpel")
    - Cache hit: return immediately (data_source="cache")
    - Cache miss: labels.json fallback or 404
    """
    normalized = label.lower().strip()

    entry = cache.get(normalized)
    if entry is not None:
        return entry

    # Tier 2: External Customer MDM Lookup
    logger.info("Cache miss for label=%r — trying external MDM", normalized)
    mdm_entry = await _lookup_mdm(normalized)
    if mdm_entry is not None:
        return mdm_entry

    # Tier 3: Local labels.json fallback
    logger.info("MDM miss for label=%r — using local fallback", normalized)
    from src.device_master.cache import _load_labels_json
    labels_config = _load_labels_json()
    cfg = labels_config.get(normalized)

    if cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Label '{label}' not in known device list. "
                   "Add it to labels.json and call POST /device/refresh.",
        )

    from src.device_master.schemas import DeviceLookupResponse as _DR
    return _DR(
        yolo_label=normalized,
        device_name=cfg.get("fallback_name", label),
        product_code=cfg.get("fallback_product_code"),
        device_class=cfg.get("fallback_class"),
        data_source="fallback",
    )


@app.get("/device/labels", summary="Full cache list")
async def list_labels() -> JSONResponse:
    entries = cache.all_entries()
    return JSONResponse({
        "count": len(entries),
        "labels": {k: v.model_dump() for k, v in entries.items()},
    })


@app.post(
    "/device/refresh",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Force Firebase re-fetch",
)
async def refresh_cache() -> JSONResponse:
    logger.info("Cache refresh requested")
    await cache.build(force_refresh=True)
    return JSONResponse({
        "status": "refreshed",
        "label_count": len(cache.all_entries()),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _check_firebase() -> bool:
    """Check if Firebase (Digioptics Application DB) is reachable."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get("https://firestore.googleapis.com/")
            return resp.status_code in (200, 400, 404)  # any HTTP response = reachable
    except Exception:
        return False


async def _lookup_mdm(label: str) -> DeviceLookupResponse | None:
    """Tier 2 lookup: Call the hospital-internal or mock MDM API."""
    if not CUSTOMER_MDM_URL:
        return None

    try:
        headers = {}
        if CUSTOMER_MDM_API_KEY:
            headers["Authorization"] = f"Bearer {CUSTOMER_MDM_API_KEY}"

        # Note: We use a tight timeout (2.0s) to avoid blocking the detection loop
        async with httpx.AsyncClient(timeout=2.0) as client:
            url = f"{CUSTOMER_MDM_URL.rstrip('/')}/device/lookup"
            resp = await client.get(url, params={"label": label}, headers=headers)

            if resp.status_code == 200:
                data = resp.json()
                logger.info("MDM hit for label=%r", label)
                return DeviceLookupResponse(
                    yolo_label=data.get("detection_label", label),
                    device_name=data.get("device_name", label),
                    product_code=data.get("product_code"),
                    device_class=data.get("device_class"),
                    medical_specialty=data.get("medical_specialty"),
                    data_source="mdm",
                )
            elif resp.status_code == 404:
                logger.info("MDM 404 for label=%r", label)
            else:
                logger.warning("MDM returned status=%d for label=%r", resp.status_code, label)

    except Exception as e:
        logger.error("Error during MDM lookup for label %r: %s", label, e)

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "src.device_master.main:app",
        host="0.0.0.0",
        port=8005,
        log_level="info",
    )

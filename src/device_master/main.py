"""
main.py — Device Master Agent (Port 8005)

Caches openFDA Device Classification API data and provides
YOLO detection label → FDA standard device info lookup.

In production, replace this container by pointing DEVICE_MASTER_URL
to the hospital's internal MDM server — no other architecture changes needed.

Endpoints:
  GET  /health                — module status + cache info
  GET  /device/lookup?label=  — single label FDA info lookup
  GET  /device/labels         — full cache list
  POST /device/refresh        — force openFDA re-fetch
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
HOSPITAL_API_URL = os.getenv("HOSPITAL_API_URL", "").rstrip("/")
HOSPITAL_API_KEY = os.getenv("HOSPITAL_API_KEY", "")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Device Master Agent starting — building cache from openFDA...")
    await cache.build()
    logger.info("Cache ready: %d labels", len(cache.all_entries()))
    yield
    logger.info("Device Master Agent shutdown")


app = FastAPI(
    title="Device Master Agent API",
    description=(
        "Surgical instrument standard info lookup via openFDA. "
        "Swap DEVICE_MASTER_URL to point at a real hospital MDM system."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    reachable = await _check_openfda()
    return HealthResponse(
        status="healthy" if cache.is_loaded() else "degraded",
        module=MODULE_NAME,
        cache=CacheStatus(
            loaded=cache.is_loaded(),
            label_count=len(cache.all_entries()),
            cache_age_hours=cache.cache_age_hours(),
            openfda_reachable=reachable,
        ),
    )


@app.get(
    "/device/lookup",
    response_model=DeviceLookupResponse,
    summary="YOLO label → FDA device info lookup",
)
async def lookup_device(label: str) -> DeviceLookupResponse:
    """
    label: YOLO detection label (e.g. "forceps", "scalpel")
    - Cache hit: return immediately (data_source="cache")
    - Cache miss: attempt live openFDA query (data_source="openfda_live")
    - Query failure: labels.json fallback or 404
    """
    normalized = label.lower().strip()

    entry = cache.get(normalized)
    if entry is not None:
        return entry

    if HOSPITAL_API_URL:
        hospital_entry = await _fetch_from_hospital_api(normalized)
        if hospital_entry is not None:
            cache.set(normalized, hospital_entry)
            logger.info("Hospital API hit: %r → %s", normalized, hospital_entry.device_name)
            return hospital_entry

    logger.info("Cache miss for label=%r — using local fallback", normalized)
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
    summary="Force openFDA re-fetch",
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

async def _fetch_from_hospital_api(label: str) -> DeviceLookupResponse | None:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.post(
                f"{HOSPITAL_API_URL}/instruments/lookup",
                json={"labels": [label]},
                headers={"X-API-Key": HOSPITAL_API_KEY},
            )
        if resp.status_code != 200:
            logger.warning("Hospital API returned %d for label=%r", resp.status_code, label)
            return None
        match = resp.json().get("matches", {}).get(label)
        if not match:
            return None
        return DeviceLookupResponse(
            yolo_label=label,
            device_name=match["name"],
            product_code=match.get("catalog_id"),
            device_class=match.get("device_class"),
            medical_specialty=match.get("tray_location"),
            data_source="hospital_api",
        )
    except Exception as exc:
        logger.warning("Hospital API unreachable for label=%r: %s", label, exc)
        return None


async def _check_openfda() -> bool:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(
                "https://api.fda.gov/device/classification.json",
                params={"search": 'device_name:"forceps"', "limit": "1"},
            )
            return resp.status_code == 200
    except Exception:
        return False


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

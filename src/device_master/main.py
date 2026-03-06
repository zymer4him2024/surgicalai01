"""
main.py — Device Master Agent (Port 8005)

openFDA Device Classification API 데이터를 캐시하여
YOLO 탐지 레이블 → FDA 표준 기기 정보 조회 서비스 제공.

실제 배포 시 이 컨테이너 대신 병원/업체 내부 MDM 서버를 바라보도록
DEVICE_MASTER_URL 환경변수만 변경하면 나머지 아키텍처는 동일하게 유지됩니다.

엔드포인트:
  GET  /health                — 모듈 상태 + 캐시 정보
  GET  /device/lookup?label=  — 단일 레이블 FDA 정보 조회
  GET  /device/labels         — 전체 캐시 목록
  POST /device/refresh        — openFDA 재조회 강제 실행
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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Device Master Agent starting — building cache from openFDA…")
    await cache.build()
    logger.info("Cache ready: %d labels", len(cache.all_entries()))
    yield
    logger.info("Device Master Agent shutdown")


app = FastAPI(
    title="Device Master Agent API",
    description=(
        "openFDA 기반 수술 기구 표준 정보 조회 서비스. "
        "실제 병원 MDM 시스템으로 교체 시 DEVICE_MASTER_URL 환경변수만 변경."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ─────────────────────────────────────────────────────────────────────────────
# 엔드포인트
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
    summary="YOLO 레이블 → FDA 기기 정보 조회",
)
async def lookup_device(label: str) -> DeviceLookupResponse:
    """
    label: YOLO 탐지 레이블 (e.g. "forceps", "scalpel")
    - 캐시 히트: 즉시 반환 (data_source="cache")
    - 캐시 미스: openFDA 실시간 조회 시도 (data_source="openfda_live")
    - 조회 실패: labels.json fallback 또는 404
    """
    normalized = label.lower().strip()

    # 1) 캐시 히트
    entry = cache.get(normalized)
    if entry is not None:
        return entry

    # 2) 캐시 미스 → openFDA 실시간 조회
    logger.info("Cache miss for label=%r — querying openFDA live", normalized)
    from src.device_master.cache import _load_labels_json, _fetch_label
    labels_config = _load_labels_json()
    cfg = labels_config.get(normalized)

    if cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Label '{label}' not in known device list. "
                   "Add it to labels.json and call POST /device/refresh.",
        )

    async with httpx.AsyncClient(timeout=8.0) as client:
        result = await _fetch_label(client, normalized, cfg)

    result.data_source = "openfda_live"
    return result


@app.get(
    "/device/labels",
    summary="전체 캐시 목록 조회",
)
async def list_labels() -> JSONResponse:
    entries = cache.all_entries()
    return JSONResponse({
        "count": len(entries),
        "labels": {k: v.model_dump() for k, v in entries.items()},
    })


@app.post(
    "/device/refresh",
    status_code=status.HTTP_202_ACCEPTED,
    summary="openFDA 재조회 강제 실행",
)
async def refresh_cache() -> JSONResponse:
    logger.info("Cache refresh requested")
    await cache.build(force_refresh=True)
    return JSONResponse({
        "status": "refreshed",
        "label_count": len(cache.all_entries()),
    })


# ─────────────────────────────────────────────────────────────────────────────
# 헬퍼
# ─────────────────────────────────────────────────────────────────────────────

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
# 엔트리포인트
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "src.device_master.main:app",
        host="0.0.0.0",
        port=8005,
        log_level="info",
    )

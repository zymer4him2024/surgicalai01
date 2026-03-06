"""
cache.py — openFDA Device Classification API 조회 및 로컬 캐시 관리

전략:
  1. 시작 시 labels.json 읽기 (YOLO 레이블 → FDA 검색어 매핑)
  2. /app/data/device_cache.json 존재하고 7일 이내면 파일에서 로드
  3. 그 외에는 openFDA API를 호출하여 캐시 구축
  4. openFDA 실패 시 labels.json fallback 값 사용
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore

from src.device_master.schemas import DeviceLookupResponse

logger = logging.getLogger("device_master.cache")

CACHE_FILE = Path(os.getenv("DEVICE_CACHE_PATH", "/app/data/device_cache.json"))
CACHE_TTL_HOURS = float(os.getenv("DEVICE_CACHE_TTL_HOURS", "168"))  # 7일
LABELS_FILE = Path(__file__).parent / "labels.json"

# 인메모리 캐시: yolo_label → DeviceLookupResponse
_cache: dict[str, DeviceLookupResponse] = {}
_cache_loaded_at: float = 0.0


def get(label: str) -> DeviceLookupResponse | None:
    """캐시에서 레이블 조회. 없으면 None."""
    return _cache.get(label.lower())


def all_entries() -> dict[str, DeviceLookupResponse]:
    return dict(_cache)


def is_loaded() -> bool:
    return len(_cache) > 0


def cache_age_hours() -> float | None:
    if _cache_loaded_at == 0.0:
        return None
    return (time.time() - _cache_loaded_at) / 3600


async def build(force_refresh: bool = False) -> None:
    """캐시 구축 진입점. 앱 시작 시 / POST /device/refresh 시 호출."""
    global _cache, _cache_loaded_at

    labels_config = _load_labels_json()
    if not labels_config:
        logger.error("labels.json not found or empty — no labels to cache")
        return

    # 파일 캐시 사용 가능 여부 확인
    if not force_refresh and _try_load_from_file(labels_config):
        return

    # Firestore 조회
    logger.info("Fetching device data from Firebase 'device_catalog' for %d labels…", len(labels_config))
    result: dict[str, DeviceLookupResponse] = {}

    try:
        # Initialize Firebase Admin if not already initialized
        if not firebase_admin._apps:
            cred_path = os.getenv("FIREBASE_CREDENTIALS_PATH", "/app/firebase-credentials.json")
            if os.path.exists(cred_path):
                cred = credentials.Certificate(cred_path)
                firebase_admin.initialize_app(cred)
            else:
                logger.warning("Firebase credentials not found at %s. Running without Firebase.", cred_path)
        
        if firebase_admin._apps:
            db = firestore.client()
            catalog_ref = db.collection("device_catalog")
            docs = await asyncio.to_thread(lambda: catalog_ref.get())
            catalog_data = {doc.id: doc.to_dict() for doc in docs}

            for label, cfg in labels_config.items():
                entry = _build_from_catalog(label, cfg, catalog_data.get(label))
                result[label] = entry
        else:
            # Fallback if no credentials
            for label, cfg in labels_config.items():
                result[label] = _build_from_catalog(label, cfg, None)

    except Exception as exc:
        logger.error("Failed to query Firebase: %s", exc)
        for label, cfg in labels_config.items():
            result[label] = _build_from_catalog(label, cfg, None)

    _cache = result
    _cache_loaded_at = time.time()
    _save_to_file(result)
    logger.info("Cache built: %d entries (source=firebase)", len(result))


def _load_labels_json() -> dict:
    try:
        return json.loads(LABELS_FILE.read_text())
    except Exception as exc:
        logger.error("Failed to read labels.json: %s", exc)
        return {}


def _try_load_from_file(labels_config: dict) -> bool:
    """파일 캐시가 유효하면 로드하고 True 반환."""
    global _cache, _cache_loaded_at

    if not CACHE_FILE.exists():
        return False

    age_hours = (time.time() - CACHE_FILE.stat().st_mtime) / 3600
    if age_hours > CACHE_TTL_HOURS:
        logger.info("Cache file expired (%.1f h) — refreshing from openFDA", age_hours)
        return False

    try:
        raw = json.loads(CACHE_FILE.read_text())
        loaded: dict[str, DeviceLookupResponse] = {}
        for label, data in raw.items():
            if label in labels_config:
                loaded[label] = DeviceLookupResponse(**data)
        _cache = loaded
        _cache_loaded_at = CACHE_FILE.stat().st_mtime
        logger.info(
            "Cache loaded from file: %d entries (age=%.1fh)", len(loaded), age_hours
        )
        return True
    except Exception as exc:
        logger.warning("Cache file corrupt (%s) — rebuilding", exc)
        return False


import asyncio

def _build_from_catalog(label: str, cfg: dict, catalog_entry: dict | None) -> DeviceLookupResponse:
    """Build a DeviceLookupResponse from a Firebase catalog entry, or fallback to static mapping."""
    if catalog_entry:
        logger.debug("Firebase hit for %r", label)
        return DeviceLookupResponse(
            yolo_label=label,
            device_name=catalog_entry.get("device_name", cfg.get("fallback_name", label)),
            product_code=cfg.get("fallback_product_code"),
            device_class=catalog_entry.get("fda_class", cfg.get("fallback_class")),
            medical_specialty=catalog_entry.get("material", None), # Mapping material to specialty for now
            data_source="firebase_catalog",
        )
    
    logger.warning("No Firebase match for %r — using fallback", label)
    return DeviceLookupResponse(
        yolo_label=label,
        device_name=cfg.get("fallback_name", label),
        product_code=cfg.get("fallback_product_code"),
        device_class=cfg.get("fallback_class"),
        medical_specialty=None,
        data_source="fallback",
    )


def _save_to_file(entries: dict[str, DeviceLookupResponse]) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {k: v.model_dump() for k, v in entries.items()}
        CACHE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        logger.info("Cache saved → %s", CACHE_FILE)
    except Exception as exc:
        logger.warning("Failed to save cache file: %s", exc)

"""
uploader.py — Firebase Storage & Firestore 업로더

인증 전략 (우선순위):
  1. FIREBASE_CREDENTIALS_JSON  (환경변수에 JSON 문자열)
  2. FIREBASE_CREDENTIALS_PATH  (환경변수에 서비스 계정 파일 경로)
  3. GOOGLE_APPLICATION_CREDENTIALS (GCP 표준 환경변수)
  위 모두 없으면 → 시뮬레이션 모드

Firestore 문서 구조:
  /sync_events/{doc_id}
    ├── event_type: str
    ├── expected_count: int
    ├── actual_count: int
    ├── missing_items: list
    ├── detected_items: list
    ├── snapshot_urls: list[str]   ← Firebase Storage URL
    ├── timestamp: Timestamp
    └── metadata: dict
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import random
import string
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("firebase_sync.uploader")

FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "surgicalai01")
FIREBASE_STORAGE_BUCKET = os.getenv(
    "FIREBASE_STORAGE_BUCKET", "surgicalai01.firebasestorage.app"
)
FIRESTORE_COLLECTION = os.getenv("FIRESTORE_COLLECTION", "sync_events")


# ─────────────────────────────────────────────────────────────────────────────
# 업로더 팩토리
# ─────────────────────────────────────────────────────────────────────────────

def create_uploader() -> "BaseUploader":
    """자격증명 유무에 따라 실제/시뮬레이션 업로더 반환."""
    cred = _load_credentials()
    if cred is not None:
        try:
            uploader = FirebaseUploader(cred)
            logger.info("Firebase uploader initialized (project=%s)", FIREBASE_PROJECT_ID)
            return uploader
        except Exception as exc:
            logger.warning("Firebase init failed (%s) — simulation mode", exc)

    logger.info("Simulation mode: uploads will be mocked")
    return SimulationUploader()


def _load_credentials():
    """환경변수에서 Firebase 자격증명 로드. 없으면 None 반환."""
    # 1) JSON 문자열
    cred_json = os.getenv("FIREBASE_CREDENTIALS_JSON")
    if cred_json:
        try:
            from firebase_admin import credentials  # type: ignore[import]
            data = json.loads(cred_json)
            return credentials.Certificate(data)
        except Exception as exc:
            logger.warning("FIREBASE_CREDENTIALS_JSON parse error: %s", exc)

    # 2) 파일 경로
    cred_path = os.getenv("FIREBASE_CREDENTIALS_PATH") or os.getenv(
        "GOOGLE_APPLICATION_CREDENTIALS"
    )
    if cred_path and os.path.exists(cred_path):
        try:
            from firebase_admin import credentials  # type: ignore[import]
            return credentials.Certificate(cred_path)
        except Exception as exc:
            logger.warning("Firebase credentials file error: %s", exc)

    return None


# ─────────────────────────────────────────────────────────────────────────────
# 베이스 클래스
# ─────────────────────────────────────────────────────────────────────────────

class BaseUploader:
    simulation_mode: bool = False

    async def upload_event(
        self,
        payload: dict,
        shots: list[dict],
    ) -> tuple[str, list[str]]:
        """
        이벤트 업로드.
        Returns: (firestore_doc_id, [storage_url, ...])
        """
        raise NotImplementedError

    async def is_reachable(self) -> bool:
        raise NotImplementedError


# ─────────────────────────────────────────────────────────────────────────────
# 실제 Firebase 업로더
# ─────────────────────────────────────────────────────────────────────────────

class FirebaseUploader(BaseUploader):
    simulation_mode = False

    def __init__(self, credential) -> None:
        import firebase_admin  # type: ignore[import]
        from firebase_admin import firestore, storage  # type: ignore[import]

        # 앱이 이미 초기화된 경우 재사용
        try:
            self._app = firebase_admin.get_app()
        except ValueError:
            self._app = firebase_admin.initialize_app(
                credential,
                {"storageBucket": FIREBASE_STORAGE_BUCKET},
            )

        self._db = firestore.client()
        self._bucket = storage.bucket()

    async def upload_event(
        self,
        payload: dict,
        shots: list[dict],
    ) -> tuple[str, list[str]]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._upload_sync, payload, shots
        )

    def _upload_sync(
        self, payload: dict, shots: list[dict]
    ) -> tuple[str, list[str]]:
        from google.cloud.firestore_v1 import SERVER_TIMESTAMP  # type: ignore[import]

        # 1) Storage: 스냅샷 3장 업로드
        storage_urls: list[str] = []
        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

        for shot in shots:
            blob_path = (
                f"snapshots/{timestamp_str}"
                f"/shot{shot['shot']}_{shot['label']}.jpg"
            )
            blob = self._bucket.blob(blob_path)
            blob.upload_from_string(
                shot["jpeg_bytes"],
                content_type="image/jpeg",
            )
            blob.make_public()
            storage_urls.append(blob.public_url)
            logger.info("Uploaded shot %d → %s", shot["shot"], blob.public_url)

        # 2) Firestore: 이벤트 문서 생성
        doc_ref = self._db.collection(FIRESTORE_COLLECTION).document()
        doc_ref.set({
            **payload,
            "snapshot_urls": storage_urls,
            "timestamp": SERVER_TIMESTAMP,
        })
        doc_id = doc_ref.id
        logger.info("Firestore document created: %s/%s", FIRESTORE_COLLECTION, doc_id)
        return doc_id, storage_urls

    async def is_reachable(self) -> bool:
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: self._db.collection(FIRESTORE_COLLECTION).limit(1).get(),
            )
            return True
        except Exception:
            return False


# ─────────────────────────────────────────────────────────────────────────────
# 시뮬레이션 업로더 (Firebase 자격증명 없을 때)
# ─────────────────────────────────────────────────────────────────────────────

class SimulationUploader(BaseUploader):
    simulation_mode = True

    async def upload_event(
        self,
        payload: dict,
        shots: list[dict],
    ) -> tuple[str, list[str]]:
        # 네트워크 업로드 지연 시뮬레이션
        await asyncio.sleep(0.3 + random.uniform(0, 0.2))

        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        doc_id = (
            "sim_"
            + timestamp_str
            + "_"
            + "".join(random.choices(string.ascii_lowercase, k=6))
        )
        storage_urls = [
            f"https://storage.googleapis.com/{FIREBASE_STORAGE_BUCKET}/"
            f"snapshots/{timestamp_str}/shot{s['shot']}_{s['label']}.jpg"
            for s in shots
        ]
        logger.info(
            "[SIMULATION] Uploaded event — doc_id=%s, %d snapshots",
            doc_id,
            len(shots),
        )
        return doc_id, storage_urls

    async def is_reachable(self) -> bool:
        return True   # 시뮬레이션 모드에서는 항상 True

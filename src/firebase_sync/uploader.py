"""
uploader.py — Firebase Storage & Firestore uploader

Credential resolution (priority order):
  1. FIREBASE_CREDENTIALS_JSON  (env var with JSON string)
  2. FIREBASE_CREDENTIALS_PATH  (env var with service account file path)
  3. GOOGLE_APPLICATION_CREDENTIALS (GCP standard env var)
  If none found → simulation mode

Firestore document structure:
  /sync_events/{doc_id}
    ├── event_type: str
    ├── expected_count: int
    ├── actual_count: int
    ├── missing_items: list
    ├── detected_items: list
    ├── snapshot_urls: list[str]
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

# Identity — stamped on every Firestore document
_APP_ID = os.getenv("APP_ID", "unknown")
_DEVICE_ID = os.getenv("DEVICE_ID", "unknown")


# ─────────────────────────────────────────────────────────────────────────────
# Uploader factory
# ─────────────────────────────────────────────────────────────────────────────

def create_uploader() -> "BaseUploader":
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
    cred_json = os.getenv("FIREBASE_CREDENTIALS_JSON")
    if cred_json:
        try:
            from firebase_admin import credentials  # type: ignore[import]
            data = json.loads(cred_json)
            return credentials.Certificate(data)
        except Exception as exc:
            logger.warning("FIREBASE_CREDENTIALS_JSON parse error: %s", exc)

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
# Base class
# ─────────────────────────────────────────────────────────────────────────────

class BaseUploader:
    simulation_mode: bool = False

    async def upload_event(
        self,
        payload: dict,
        shots: list[dict],
    ) -> tuple[str, list[str]]:
        raise NotImplementedError

    async def is_reachable(self) -> bool:
        raise NotImplementedError


# ─────────────────────────────────────────────────────────────────────────────
# Real Firebase uploader
# ─────────────────────────────────────────────────────────────────────────────

class FirebaseUploader(BaseUploader):
    simulation_mode = False

    def __init__(self, credential) -> None:
        import firebase_admin  # type: ignore[import]
        from firebase_admin import firestore, storage  # type: ignore[import]

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

        doc_ref = self._db.collection(FIRESTORE_COLLECTION).document()
        doc_ref.set({
            **payload,
            "app_id": _APP_ID,
            "device_id": _DEVICE_ID,
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
# Simulation uploader (no Firebase credentials)
# ─────────────────────────────────────────────────────────────────────────────

class SimulationUploader(BaseUploader):
    simulation_mode = True

    async def upload_event(
        self,
        payload: dict,
        shots: list[dict],
    ) -> tuple[str, list[str]]:
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
        return True

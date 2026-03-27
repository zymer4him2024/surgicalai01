"""firebase_init.py — Single Firebase app initialization shared across modules."""

from __future__ import annotations

import logging
import os

import firebase_admin
from firebase_admin import credentials

logger = logging.getLogger(__name__)

_initialized = False


def ensure_initialized() -> None:
    """Initialize the default Firebase app exactly once."""
    global _initialized
    if _initialized or firebase_admin._apps:
        _initialized = True
        return

    creds_path = os.environ.get("FIREBASE_CREDENTIALS_PATH", "")
    if not creds_path or not os.path.exists(creds_path):
        raise RuntimeError(
            f"FIREBASE_CREDENTIALS_PATH not set or file missing: {creds_path!r}"
        )

    bucket = os.environ.get("FIREBASE_STORAGE_BUCKET", "")
    if not bucket:
        raise RuntimeError("FIREBASE_STORAGE_BUCKET env var not set")

    cred = credentials.Certificate(creds_path)
    firebase_admin.initialize_app(cred, {"storageBucket": bucket})
    _initialized = True
    logger.info("Firebase app initialized (bucket=%s)", bucket)

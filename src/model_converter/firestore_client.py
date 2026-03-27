"""firestore_client.py — Firestore polling and status updates for Model Converter."""

from __future__ import annotations

import logging
from typing import Any

from firebase_admin import firestore
from google.cloud.firestore_v1 import ArrayUnion

from .firebase_init import ensure_initialized

logger = logging.getLogger(__name__)


def _db():
    ensure_initialized()
    return firestore.client()


def poll_pending_jobs() -> list[dict[str, Any]]:
    """Return all model docs with conversion_status == 'pending_conversion'."""
    try:
        docs = (
            _db().collection("models")
            .where("conversion_status", "==", "pending_conversion")
            .stream()
        )
        results = []
        for doc in docs:
            data = doc.to_dict()
            data["model_id"] = doc.id
            results.append(data)
        return results
    except Exception as exc:
        logger.warning("poll_pending_jobs error: %s", exc)
        return []


def set_status(model_id: str, status: str, **extra_fields) -> None:
    """Update conversion_status and any extra fields on the model doc."""
    try:
        update: dict[str, Any] = {"conversion_status": status, **extra_fields}
        _db().collection("models").document(model_id).update(update)
        logger.info("models/%s → conversion_status=%s", model_id, status)
    except Exception as exc:
        logger.warning("set_status error for %s: %s", model_id, exc)


def reset_stale_jobs() -> None:
    """On converter startup, reset any jobs stuck in 'processing' to 'pending_conversion'.

    A job gets stuck when the converter crashes mid-run. Without this, those
    jobs stay at 'processing' forever and are never retried.
    """
    try:
        db = _db()
        docs = (
            db.collection("models")
            .where("conversion_status", "==", "processing")
            .stream()
        )
        count = 0
        for doc in docs:
            db.collection("models").document(doc.id).update({
                "conversion_status": "pending_conversion",
                "conversion_log": ArrayUnion(["[RECOVERED] Converter restarted — requeueing"]),
            })
            count += 1
        if count:
            logger.info("Recovered %d stale job(s) → pending_conversion", count)
    except Exception as exc:
        logger.warning("reset_stale_jobs error: %s", exc)


def append_log(model_id: str, line: str) -> None:
    """Append a log line to the model doc's conversion_log array."""
    try:
        _db().collection("models").document(model_id).update(
            {"conversion_log": ArrayUnion([line])}
        )
    except Exception as exc:
        logger.warning("append_log error for %s: %s", model_id, exc)

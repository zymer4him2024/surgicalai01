#!/usr/bin/env python3
"""
cleanup_project.py — Cascade-delete all Firestore data linked to a project.

Cleans backend-only collections that the admin UI cannot write to
(sync_events, inspection_log, system_status, inventory_count_events).
Run this AFTER deleting the project from the admin dashboard.

Usage:
    python scripts/cleanup_project.py --project-id <PROJECT_ID> [--dry-run]

Requires:
    - GOOGLE_APPLICATION_CREDENTIALS or FIREBASE_CREDENTIALS_PATH env var
    - firebase-admin package
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("cleanup_project")

# Backend-only event collections that store project_id
EVENT_COLLECTIONS = ["sync_events", "inventory_count_events"]

# Per-device collections keyed by device_id
DEVICE_KEYED_COLLECTIONS = ["inspection_log", "system_status"]


def _init_firestore():
    """Initialize Firestore client via Admin SDK."""
    try:
        import firebase_admin  # type: ignore[import]
        from firebase_admin import credentials, firestore  # type: ignore[import]
    except ImportError:
        logger.critical("firebase-admin is not installed. Run: pip install firebase-admin")
        sys.exit(1)

    cred_path = os.getenv("FIREBASE_CREDENTIALS_PATH") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if not cred_path:
        logger.critical(
            "No Firebase credentials found. Set FIREBASE_CREDENTIALS_PATH or "
            "GOOGLE_APPLICATION_CREDENTIALS environment variable."
        )
        sys.exit(1)

    if not os.path.isfile(cred_path):
        logger.critical("Credentials file not found: %s", cred_path)
        sys.exit(1)

    cred = credentials.Certificate(cred_path)
    firebase_admin.initialize_app(cred)
    return firestore.client()


def _batch_delete(db, query, *, dry_run: bool, collection_name: str) -> int:
    """Delete all documents matching a query in batches of 100."""
    deleted = 0
    while True:
        docs = list(query.limit(100).stream())
        if not docs:
            break
        batch = db.batch()
        for doc_snap in docs:
            if dry_run:
                logger.info("  [DRY RUN] Would delete %s/%s", collection_name, doc_snap.id)
            else:
                batch.delete(doc_snap.reference)
            deleted += 1
        if not dry_run:
            batch.commit()
    return deleted


def _find_project_devices(db, project_id: str) -> list[str]:
    """Find all device_ids linked to the project."""
    device_ids: list[str] = []
    docs = db.collection("devices").where("project_id", "==", project_id).stream()
    for doc_snap in docs:
        data = doc_snap.to_dict()
        did = data.get("device_id", "")
        if did:
            device_ids.append(did)
    return device_ids


def cleanup(project_id: str, *, dry_run: bool = False) -> None:
    """Run cascade cleanup for a project."""
    db = _init_firestore()
    prefix = "[DRY RUN] " if dry_run else ""
    total_deleted = 0

    logger.info("%sStarting cleanup for project_id=%r", prefix, project_id)

    # 1. Delete event collections by project_id
    for coll_name in EVENT_COLLECTIONS:
        query = db.collection(coll_name).where("project_id", "==", project_id)
        count = _batch_delete(db, query, dry_run=dry_run, collection_name=coll_name)
        logger.info("%s%s: deleted %d documents", prefix, coll_name, count)
        total_deleted += count

    # 2. Find devices that were linked to this project
    device_ids = _find_project_devices(db, project_id)
    if not device_ids:
        # Fallback: also check sync_events for device_ids (in case devices already unlinked)
        logger.info("No devices found with project_id=%r in devices collection. "
                     "Trying sync_events for device_id references...", project_id)
        seen: set[str] = set()
        for coll_name in EVENT_COLLECTIONS:
            docs = db.collection(coll_name).where("project_id", "==", project_id).limit(50).stream()
            for doc_snap in docs:
                did = doc_snap.to_dict().get("device_id", "")
                if did:
                    seen.add(did)
        device_ids = list(seen)

    logger.info("Linked device_ids: %s", device_ids or "(none)")

    # 3. Clean per-device collections
    for device_id in device_ids:
        for coll_name in DEVICE_KEYED_COLLECTIONS:
            doc_ref = db.collection(coll_name).document(device_id)
            doc_snap = doc_ref.get()
            if doc_snap.exists:
                if coll_name == "system_status":
                    # Clear project_id but keep the document (device still exists)
                    if dry_run:
                        logger.info("  [DRY RUN] Would clear project_id on %s/%s", coll_name, device_id)
                    else:
                        doc_ref.update({"project_id": ""})
                        logger.info("  Cleared project_id on %s/%s", coll_name, device_id)
                else:
                    if dry_run:
                        logger.info("  [DRY RUN] Would delete %s/%s", coll_name, device_id)
                    else:
                        doc_ref.delete()
                        logger.info("  Deleted %s/%s", coll_name, device_id)
                total_deleted += 1

    logger.info("%sCleanup complete. Total documents affected: %d", prefix, total_deleted)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cascade-delete all Firestore data linked to a project."
    )
    parser.add_argument(
        "--project-id", required=True,
        help="The Firestore project document ID to clean up.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview what would be deleted without making changes.",
    )
    args = parser.parse_args()
    cleanup(args.project_id, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

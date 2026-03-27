"""storage_client.py — Firebase Storage upload/download for Model Converter."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from urllib.parse import quote

from firebase_admin import storage

from .firebase_init import ensure_initialized

logger = logging.getLogger(__name__)


def _bucket():
    ensure_initialized()
    return storage.bucket()


def download_model(storage_path: str, local_dest: str) -> str:
    """Download a file from Firebase Storage to local_dest. Returns local path."""
    blob = _bucket().blob(storage_path)
    Path(local_dest).parent.mkdir(parents=True, exist_ok=True)
    blob.download_to_filename(local_dest)
    logger.info("Downloaded gs://%s → %s", storage_path, local_dest)
    return local_dest


def upload_hef(local_hef_path: str, model_id: str, net_name: str) -> str:
    """Upload HEF to Firebase Storage. Returns authenticated download URL.

    Uses Firebase download token instead of make_public() — compatible with
    buckets that have Uniform Bucket-Level Access enabled (the default).
    The token is stored in blob metadata and never expires unless manually revoked.
    """
    dest_path = f"models/hef/{model_id}/{net_name}.hef"
    bucket = _bucket()
    blob = bucket.blob(dest_path)

    # Attach download token BEFORE upload so metadata is set in one operation.
    token = str(uuid.uuid4())
    blob.metadata = {"firebaseStorageDownloadTokens": token}
    blob.upload_from_filename(local_hef_path, content_type="application/octet-stream")
    # patch() persists the metadata (token) to the object after upload
    blob.patch()

    encoded_path = quote(dest_path, safe="")
    url = (
        f"https://firebasestorage.googleapis.com/v0/b/{bucket.name}/o/"
        f"{encoded_path}?alt=media&token={token}"
    )
    logger.info("Uploaded HEF → gs://%s (%s)", dest_path, url)
    return url

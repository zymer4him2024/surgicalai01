"""
seed_models.py — Scan models/ directory and upsert each file as a Firestore
document in the 'models' collection.

Safe to re-run: existing documents are updated only for file-level fields
(hef_path, file_size_mb, updated_at). Admin-edited metadata fields (name,
version, description, class_labels, status) are left untouched on subsequent
runs via merge=True + sentinel logic.

Run inside firebase_sync_agent container:
  docker exec firebase_sync_agent python3 /tmp/seed_models.py

Or locally with credentials:
  python3 scripts/seed_models.py
"""

import json
import os
import re
from pathlib import Path

from google.cloud import firestore
from google.oauth2 import service_account

MODELS_DIR = Path(os.getenv("MODELS_DIR", "./models"))
SUPPORTED_EXTS = {".hef", ".onnx", ".pt", ".tflite", ".engine", ".bin"}

FRAMEWORK_MAP = {
    ".hef": "hailo-hef",
    ".onnx": "onnx",
    ".pt": "pytorch",
    ".tflite": "tflite",
    ".engine": "tensorrt",
    ".bin": "openvino",
}

FRAMEWORK_GUESS = {
    "yolov11": "yolov11",
    "yolo11": "yolov11",
    "yolov10": "yolov10",
    "yolo10": "yolov10",
    "yolov9": "yolov9",
    "yolo9": "yolov9",
    "yolov8": "yolov8",
    "yolo8": "yolov8",
    "yolov5": "yolov5",
    "yolo5": "yolov5",
    "rtdetr": "rt-detr",
    "rt-detr": "rt-detr",
    "efficientdet": "efficientdet",
    "ssd": "ssd-mobilenet",
    "mobilenet": "ssd-mobilenet",
}


def _get_credentials():
    cred_json = os.getenv("FIREBASE_CREDENTIALS_JSON")
    if cred_json:
        data = json.loads(cred_json)
        return service_account.Credentials.from_service_account_info(data)
    cred_path = os.getenv("FIREBASE_CREDENTIALS_PATH") or os.getenv(
        "GOOGLE_APPLICATION_CREDENTIALS"
    )
    if cred_path and os.path.exists(cred_path):
        return service_account.Credentials.from_service_account_file(cred_path)
    return None


def _slug(filename: str) -> str:
    """Convert filename (including extension) to a stable Firestore doc ID."""
    name = Path(filename).name  # e.g. SurgeoNet_byme.onnx
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", name).lower()
    return slug.strip("_")


def _default_name(stem: str) -> str:
    """Best-guess human-readable name from file stem."""
    return re.sub(r"[_\-]+", " ", stem).title()


def main():
    creds = _get_credentials()
    db = firestore.Client(credentials=creds) if creds else firestore.Client()
    models_col = db.collection("models")

    if not MODELS_DIR.exists():
        print(f"Models directory not found: {MODELS_DIR.resolve()}")
        return

    files = sorted(
        f for f in MODELS_DIR.iterdir() if f.suffix.lower() in SUPPORTED_EXTS
    )

    if not files:
        print(f"No model files found in {MODELS_DIR.resolve()}")
        return

    print(f"Found {len(files)} model file(s) in {MODELS_DIR.resolve()}\n")

    for f in files:
        doc_id = _slug(f.name)
        doc_ref = models_col.document(doc_id)
        existing = doc_ref.get()

        file_size_mb = round(f.stat().st_size / 1_048_576, 1)
        framework = FRAMEWORK_MAP.get(f.suffix.lower(), "unknown")
        # Try to guess architecture from filename (e.g. yolov11n.hef -> yolov11)
        stem_lower = f.stem.lower()
        for pattern, arch in FRAMEWORK_GUESS.items():
            if pattern in stem_lower:
                framework = arch
                break

        if existing.exists:
            # Preserve admin edits — only refresh file-level fields
            doc_ref.update(
                {
                    "hef_path": f"/app/models/{f.name}",
                    "file_size_mb": file_size_mb,
                    "updated_at": firestore.SERVER_TIMESTAMP,
                }
            )
            print(f"  [updated]  {doc_id}  ({f.name}, {file_size_mb} MB)")
        else:
            # First-time seed — set sensible defaults that admin can edit later
            doc_ref.set(
                {
                    "name": _default_name(f.stem),
                    "version": "1.0.0",
                    "description": "",
                    "type": "internal",
                    "framework": framework,
                    "input_resolution": 640,
                    "class_labels": [],
                    "class_count": 0,
                    "hef_path": f"/app/models/{f.name}",
                    "file_size_mb": file_size_mb,
                    "status": "active",
                    "created_at": firestore.SERVER_TIMESTAMP,
                    "updated_at": firestore.SERVER_TIMESTAMP,
                }
            )
            print(f"  [created]  {doc_id}  ({f.name}, {file_size_mb} MB)")

    print("\nDone. Edit model metadata in the Admin Dashboard → Models tab.")


if __name__ == "__main__":
    main()

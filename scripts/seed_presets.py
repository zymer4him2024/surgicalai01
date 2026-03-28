"""
seed_presets.py — Push 10 total-count presets to Firestore job_config/{DEVICE_ID}.

Each preset has a "total" target — match is based on total detected object count.

Usage:
  FIREBASE_CREDENTIALS_PATH=./firebase-credentials.json DEVICE_ID=US-Gas-001 python3 scripts/seed_presets.py
"""

import json
import os

from google.cloud import firestore
from google.oauth2 import service_account


def _get_credentials():
    cred_json = os.getenv("FIREBASE_CREDENTIALS_JSON")
    if cred_json:
        data = json.loads(cred_json)
        return service_account.Credentials.from_service_account_info(data)
    cred_path = os.getenv("FIREBASE_CREDENTIALS_PATH") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if cred_path and os.path.exists(cred_path):
        return service_account.Credentials.from_service_account_file(cred_path)
    return None


APP_ID = os.getenv("APP_ID", "surgical")
DEVICE_ID = os.getenv("DEVICE_ID", "US-Gas-001")

PRESETS = [
    {"job_id": "SET-01", "target": {"total": 3}},
    {"job_id": "SET-02", "target": {"total": 4}},
    {"job_id": "SET-03", "target": {"total": 5}},
    {"job_id": "SET-04", "target": {"total": 6}},
    {"job_id": "SET-05", "target": {"total": 7}},
    {"job_id": "SET-06", "target": {"total": 3}},
    {"job_id": "SET-07", "target": {"total": 4}},
    {"job_id": "SET-08", "target": {"total": 5}},
    {"job_id": "SET-09", "target": {"total": 6}},
    {"job_id": "SET-10", "target": {"total": 7}},
]

sets = [{"job_id": p["job_id"], "target": p["target"]} for p in PRESETS]

creds = _get_credentials()
db = firestore.Client(credentials=creds) if creds else firestore.Client()
doc_ref = db.collection("job_config").document(DEVICE_ID)
doc_ref.set({
    "app_id": APP_ID,
    "device_id": DEVICE_ID,
    "sets": sets,
    "cursor": 0,
    "ts": firestore.SERVER_TIMESTAMP,
}, merge=False)

print(f"Written {len(sets)} presets to Firestore job_config/{DEVICE_ID} (app_id={APP_ID}):")
for p in PRESETS:
    print(f"  {p['job_id']}: total={p['target']['total']}")

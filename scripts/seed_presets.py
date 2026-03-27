"""
seed_presets.py — Push 20 surgical tray presets to Firestore job_config/{DEVICE_ID}.

Run inside firebase_sync_agent container:
  docker exec firebase_sync_agent python3 /tmp/seed_presets.py
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
DEVICE_ID = os.getenv("DEVICE_ID", "rpi-001")

_CYCLE = [3, 4, 5]  # all match with 4 scissors + MATCH_TOLERANCE=1
PRESETS = [
    {"job_id": f"TRAY-{i:03d}", "target": {"Sur. Scissor": _CYCLE[(i - 1) % len(_CYCLE)]}}
    for i in range(1, 21)
]

# Each set: {"job_id": "TRAY-001", "target": {...}}
# _do_load_current_set in firebase_sync unpacks this structure.
sets = [{"job_id": p["job_id"], "target": p["target"]} for p in PRESETS]

creds = _get_credentials()
db = firestore.Client(credentials=creds) if creds else firestore.Client()
doc_ref = db.collection("job_config").document(DEVICE_ID)
doc_ref.set({
    "app_id": APP_ID,
    "device_id": DEVICE_ID,
    "sets": sets,
    "cursor": 0,
}, merge=False)

print(f"Written {len(sets)} presets to Firestore job_config/{DEVICE_ID} (app_id={APP_ID}):")
for i, p in enumerate(PRESETS):
    items = ", ".join(f"{k}:{v}" for k, v in p["target"].items())
    print(f"  {p['job_id']}: {items}")

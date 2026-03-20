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

PRESETS = [
    {"job_id": "TRAY-001", "target": {"Scalpel": 1, "Needle Holder": 2, "Sur. Scissor": 1}},
    {"job_id": "TRAY-002", "target": {"Overholt Clamp": 2, "Sur. Forceps": 1, "Hook": 1, "Bowl": 1}},
    {"job_id": "TRAY-003", "target": {"Metz. Scissor": 1, "Atr. Forceps": 2, "Retractor": 2}},
    {"job_id": "TRAY-004", "target": {"Lig. Clamp": 3, "Peri. Clamp": 1, "Scalpel": 2, "Tong": 1}},
    {"job_id": "TRAY-005", "target": {"Needle Holder": 1, "Hook": 2, "Sur. Scissor": 1}},
    {"job_id": "TRAY-006", "target": {"Bowl": 2, "Sur. Forceps": 3, "Overholt Clamp": 1, "Retractor": 1}},
    {"job_id": "TRAY-007", "target": {"Scalpel": 1, "Metz. Scissor": 1, "Atr. Forceps": 1, "Tong": 2}},
    {"job_id": "TRAY-008", "target": {"Lig. Clamp": 2, "Needle Holder": 2, "Peri. Clamp": 1}},
    {"job_id": "TRAY-009", "target": {"Sur. Scissor": 2, "Hook": 1, "Bowl": 1, "Sur. Forceps": 2}},
    {"job_id": "TRAY-010", "target": {"Retractor": 1, "Scalpel": 2, "Overholt Clamp": 3}},
    {"job_id": "TRAY-011", "target": {"Metz. Scissor": 2, "Tong": 1, "Lig. Clamp": 1, "Needle Holder": 1}},
    {"job_id": "TRAY-012", "target": {"Peri. Clamp": 2, "Atr. Forceps": 2, "Sur. Scissor": 1}},
    {"job_id": "TRAY-013", "target": {"Hook": 3, "Bowl": 1, "Sur. Forceps": 1, "Scalpel": 1}},
    {"job_id": "TRAY-014", "target": {"Overholt Clamp": 1, "Retractor": 2, "Metz. Scissor": 1}},
    {"job_id": "TRAY-015", "target": {"Needle Holder": 3, "Lig. Clamp": 2, "Tong": 1}},
    {"job_id": "TRAY-016", "target": {"Sur. Scissor": 1, "Atr. Forceps": 1, "Peri. Clamp": 2, "Bowl": 1}},
    {"job_id": "TRAY-017", "target": {"Scalpel": 2, "Hook": 1, "Sur. Forceps": 2}},
    {"job_id": "TRAY-018", "target": {"Retractor": 1, "Overholt Clamp": 2, "Needle Holder": 1, "Lig. Clamp": 1}},
    {"job_id": "TRAY-019", "target": {"Metz. Scissor": 1, "Tong": 2, "Peri. Clamp": 1}},
    {"job_id": "TRAY-020", "target": {"Bowl": 2, "Sur. Scissor": 2, "Atr. Forceps": 1, "Hook": 2}},
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

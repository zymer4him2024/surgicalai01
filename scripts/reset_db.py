"""
reset_db.py — Delete all Firestore data except devices and job_config.

Clears: projects, customers, applications, models, app_types,
        sync_events, inspection_log, system_status, device_control

Preserves: devices/{DEVICE_ID}, job_config/{DEVICE_ID}

Run inside firebase_sync_agent container:
  docker exec firebase_sync_agent python3 /tmp/reset_db.py

Optional: set KEEP_DEVICE_ID env var to confirm which device to protect.
  docker exec -e KEEP_DEVICE_ID=US-RPi-002 firebase_sync_agent python3 /tmp/reset_db.py
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


DEVICE_ID = os.getenv("KEEP_DEVICE_ID") or os.getenv("DEVICE_ID", "")

# Collections to fully wipe
WIPE_COLLECTIONS = [
    "projects",
    "customers",
    "applications",
    "app_types",
    "sync_events",
    "inspection_log",
    "system_status",
    "device_control",
]

creds = _get_credentials()
db = firestore.Client(credentials=creds) if creds else firestore.Client()


def delete_collection(col_name: str) -> int:
    col_ref = db.collection(col_name)
    docs = list(col_ref.stream())
    count = 0
    for doc in docs:
        doc.reference.delete()
        count += 1
    return count


print("=" * 60)
print("Firestore DB Reset")
print(f"Protected device: {DEVICE_ID!r}")
print("=" * 60)

total = 0
for col in WIPE_COLLECTIONS:
    n = delete_collection(col)
    total += n
    print(f"  {col}: deleted {n} doc(s)")

print("-" * 60)
print(f"Total deleted: {total} documents")
print()

# Verify preserved collections
devices = list(db.collection("devices").stream())
job_configs = list(db.collection("job_config").stream())
print(f"Preserved devices: {[d.id for d in devices]}")
print(f"Preserved job_config: {[j.id for j in job_configs]}")
print()
print("Reset complete. Ready to re-create project/customer/model/application.")

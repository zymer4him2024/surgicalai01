"""
seed_presets.py — Push 5 test presets to Firestore job_config/rpi.
Each set: scissors = random(0, 1, 2), all other classes = 0.

Run inside firebase_sync_agent container:
  docker exec firebase_sync_agent python3 /app/scripts/seed_presets.py
"""

import json
import os
import random
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

ALL_CLASSES = [
    "forceps", "scissors", "needle_holder", "clamp",
    "scalpel", "retractor", "suction", "bovie",
    "sponge", "gauze", "towel_clip", "other_instrument",
]

random.seed()

sets = []
for i in range(5):
    preset = {cls: 0 for cls in ALL_CLASSES}
    preset["scissors"] = random.randint(0, 2)
    sets.append(preset)

creds = _get_credentials()
db = firestore.Client(credentials=creds) if creds else firestore.Client()
doc_ref = db.collection("job_config").document("rpi")
doc_ref.set({
    "sets": sets,
    "cursor": 0,
}, merge=False)

print("Presets written to Firestore job_config/rpi:")
for i, s in enumerate(sets):
    print(f"  Set {i+1}: scissors={s['scissors']}")

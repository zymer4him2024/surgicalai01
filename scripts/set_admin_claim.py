"""
set_admin_claim.py — Set Firebase Custom Claim admin=true on a user account.

Usage:
    FIREBASE_CREDENTIALS_PATH=./firebase-credentials.json python3 scripts/set_admin_claim.py <uid>

Or run inside the firebase_sync_agent container:
    docker exec firebase_sync_agent python3 /tmp/set_admin_claim.py <uid>

To find the UID: Firebase Console → Authentication → Users → copy UID column.
After running, the user must sign out and sign back in for the new claim to take effect.
"""

import os
import sys

import firebase_admin
from firebase_admin import auth, credentials


def _init_firebase() -> None:
    cred_path = os.getenv("FIREBASE_CREDENTIALS_PATH") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if not cred_path:
        print("FATAL: FIREBASE_CREDENTIALS_PATH is not set", file=sys.stderr)
        sys.exit(1)
    cred = credentials.Certificate(cred_path)
    firebase_admin.initialize_app(cred)


def set_admin(uid: str) -> None:
    auth.set_custom_user_claims(uid, {"admin": True})
    user = auth.get_user(uid)
    print(f"OK: admin=true set on {user.email} (uid={uid})")
    print("The user must sign out and sign back in for the claim to take effect.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: python3 {sys.argv[0]} <firebase-uid>", file=sys.stderr)
        sys.exit(1)

    _init_firebase()
    set_admin(sys.argv[1])

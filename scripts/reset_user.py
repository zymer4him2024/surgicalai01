"""
reset_user.py — Delete a user's Firestore doc and optionally set admin claim.

Usage:
    FIREBASE_CREDENTIALS_PATH=./firebase-credentials.json python3 scripts/reset_user.py --delete gil4him@gmail.com
    FIREBASE_CREDENTIALS_PATH=./firebase-credentials.json python3 scripts/reset_user.py --set-admin your@email.com
    FIREBASE_CREDENTIALS_PATH=./firebase-credentials.json python3 scripts/reset_user.py --delete gil4him@gmail.com --set-admin your@email.com
"""

import argparse
import os
import sys

import firebase_admin
from firebase_admin import auth, credentials, firestore


def _init_firebase() -> None:
    cred_path = os.getenv("FIREBASE_CREDENTIALS_PATH") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if not cred_path:
        print("FATAL: FIREBASE_CREDENTIALS_PATH is not set", file=sys.stderr)
        sys.exit(1)
    cred = credentials.Certificate(cred_path)
    firebase_admin.initialize_app(cred)


def delete_user_doc(email: str) -> None:
    """Find user by email in Firebase Auth, then delete their Firestore /users/{uid} doc."""
    try:
        user = auth.get_user_by_email(email)
    except auth.UserNotFoundError:
        print(f"No Firebase Auth user found for {email}")
        return

    uid = user.uid
    print(f"Found user: {email} (uid={uid})")

    db = firestore.client()
    doc_ref = db.collection("users").document(uid)
    snap = doc_ref.get()
    if snap.exists:
        doc_ref.delete()
        print(f"Deleted Firestore doc: users/{uid}")
    else:
        print(f"No Firestore doc found at users/{uid} (already clean)")


def set_admin(email: str) -> None:
    """Set admin=true custom claim on the given user."""
    try:
        user = auth.get_user_by_email(email)
    except auth.UserNotFoundError:
        print(f"No Firebase Auth user found for {email}")
        return

    auth.set_custom_user_claims(user.uid, {"admin": True})
    print(f"OK: admin=true set on {email} (uid={user.uid})")
    print("Sign out and sign back in for the claim to take effect.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reset user docs and set admin claims")
    parser.add_argument("--delete", metavar="EMAIL", help="Delete Firestore user doc for this email")
    parser.add_argument("--set-admin", metavar="EMAIL", help="Set admin=true claim on this email")
    args = parser.parse_args()

    if not args.delete and not args.set_admin:
        parser.print_help()
        sys.exit(1)

    _init_firebase()

    if args.delete:
        delete_user_doc(args.delete)
    if args.set_admin:
        set_admin(args.set_admin)

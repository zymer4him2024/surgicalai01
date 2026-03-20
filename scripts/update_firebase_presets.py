import os
import json
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
FIREBASE_CREDS = os.getenv("FIREBASE_CREDENTIALS_PATH", "firebase-credentials.json")
DEVICE_ID = os.getenv("DEVICE_ID", "rpi")

def sync_presets_to_firebase():
    if not os.path.exists(FIREBASE_CREDS):
        print(f"Error: Firebase credentials not found at {FIREBASE_CREDS}")
        print("Please ensure your firebase-credentials.json is in the project root.")
        return

    # Initialize Firebase
    cred = credentials.Certificate(FIREBASE_CREDS)
    try:
        firebase_admin.get_app()
    except ValueError:
        firebase_admin.initialize_app(cred)

    db = firestore.client()
    
    # Read the 20 generated JSON files
    preset_dir = "test_data/surgeonet_presets"
    sets_data = []
    
    files = sorted([f for f in os.listdir(preset_dir) if f.endswith('.json')])
    for filename in files:
        with open(os.path.join(preset_dir, filename), 'r') as f:
            data = json.load(f)
            # The 'sets' array in Firestore expects the raw target/job object
            sets_data.append(data)
            
    if not sets_data:
        print("No preset JSON files found. Run generate_surgeonet_mocks.py first.")
        return

    # Update Firestore
    doc_ref = db.collection('job_config').document(DEVICE_ID)
    
    print(f"Uploading {len(sets_data)} presets to Firestore: job_config/{DEVICE_ID}...")
    
    doc_ref.set({
        "sets": sets_data,
        "cursor": 0  # Reset the cycle cursor back to the first set
    }, merge=True)
    
    print("Success! The Gateway Agent will now auto-cycle through these 20 presets.")
    print("The display will advance to the next preset 5 seconds after a MATCH or ERROR.")

if __name__ == "__main__":
    sync_presets_to_firebase()

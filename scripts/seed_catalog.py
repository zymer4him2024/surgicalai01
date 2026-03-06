import os
import json
import firebase_admin
from firebase_admin import credentials, firestore

# Determine credential path
cred_path = os.getenv("FIREBASE_CREDENTIALS_PATH", "firebase-credentials.json")
if not os.path.exists(cred_path):
    print(f"Error: Could not find {cred_path}. Please ensure your Firebase service account JSON is present.")
    exit(1)

# Initialize Firebase
cred = credentials.Certificate(cred_path)
firebase_admin.initialize_app(cred)
db = firestore.client()

# Data extracted based on generic surgical catalog formats
catalog_data = {
    "forceps": {
        "device_name": "Surgical Forceps",
        "description": "Used for grasping, holding, or manipulating body tissue.",
        "material": "High-Grade Stainless Steel",
        "common_sizes": ["12cm", "15cm", "20cm"],
        "catalog_page": 24,
        "fda_class": "I"
    },
    "scissors": {
        "device_name": "Surgical Scissors",
        "description": "Used for cutting tissue, sutures, and other materials.",
        "material": "Surgical Stainless Steel / Tungsten Carbide (TC)",
        "common_sizes": ["11cm", "14cm"],
        "catalog_page": 42,
        "fda_class": "I"
    },
    "needle_holder": {
        "device_name": "Needle Holder",
        "description": "Used by surgeons to hold a suturing needle for closing wounds.",
        "material": "Stainless Steel with TC inserts",
        "common_sizes": ["13cm", "15cm", "18cm"],
        "catalog_page": 56,
        "fda_class": "I"
    },
    "clamp": {
        "device_name": "Hemostatic Clamp",
        "description": "Surgical tool used to control bleeding by clamping and holding blood vessels.",
        "material": "Stainless Steel",
        "common_sizes": ["10cm", "14cm (Kelly)"],
        "catalog_page": 18,
        "fda_class": "I"
    },
    "scalpel": {
        "device_name": "Scalpel Handle",
        "description": "Reusable handle for disposable surgical blades.",
        "material": "Stainless Steel",
        "common_sizes": ["No. 3", "No. 4", "No. 7"],
        "catalog_page": 12,
        "fda_class": "II"
    },
    "retractor": {
        "device_name": "Surgical Retractor",
        "description": "Used to separate the edges of a surgical incision or wound, or to hold back underlying organs and tissues.",
        "material": "Stainless Steel",
        "common_sizes": ["Various (Army-Navy, Senn)"],
        "catalog_page": 80,
        "fda_class": "I"
    },
    "suction": {
        "device_name": "Suction Tube",
        "description": "Used to remove blood, fluids, and debris from the surgical site.",
        "material": "Stainless Steel",
        "common_sizes": ["Frazier 8Fr-12Fr", "Yankauer"],
        "catalog_page": 95,
        "fda_class": "II"
    },
    "bovie": {
        "device_name": "Electrosurgical Pencil (Bovie)",
        "description": "Used for cutting tissue and controlling bleeding using high-frequency electrical current.",
        "material": "Medical Grade Plastic / Stainless Tips",
        "common_sizes": ["Standard"],
        "catalog_page": 110,
        "fda_class": "II"
    },
    "sponge": {
        "device_name": "Surgical Sponge",
        "description": "Sterile, absorbent pads used for cleaning and absorbing blood.",
        "material": "100% Cotton",
        "common_sizes": ["4x4 inch", "Lap Sponges"],
        "catalog_page": 125,
        "fda_class": "II"
    },
    "gauze": {
        "device_name": "Surgical Gauze",
        "description": "Thin, translucent fabric used as a dressing or swab.",
        "material": "Cotton Fabric",
        "common_sizes": ["2x2 inch", "4x4 inch"],
        "catalog_page": 126,
        "fda_class": "II"
    },
    "towel_clip": {
        "device_name": "Towel Clip",
        "description": "Used to hold surgical drapes in place.",
        "material": "Stainless Steel",
        "common_sizes": ["9cm", "13cm (Backhaus)"],
        "catalog_page": 35,
        "fda_class": "I"
    },
    "other_instrument": {
        "device_name": "General Instrument",
        "description": "Generic fallback for an unrecognized surgical instrument.",
        "material": "Varies",
        "common_sizes": ["Varies"],
        "catalog_page": 0,
        "fda_class": "Unknown"
    }
}

def seed_database():
    print("Starting to seed database 'device_catalog' in Firestore...")
    collection_ref = db.collection("device_catalog")
    
    for item_key, data in catalog_data.items():
        print(f"Uploading catalog data for shape: {item_key}...")
        doc_ref = collection_ref.document(item_key)
        
        # Add a timestamp to know when it was last updated
        data["last_updated"] = firestore.SERVER_TIMESTAMP
        doc_ref.set(data)
        
    print("Database seeding completed successfully! 🎉")

if __name__ == "__main__":
    seed_database()

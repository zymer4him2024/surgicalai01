import os
import logging
from typing import Optional
from fastapi import FastAPI, HTTPException, Header, Depends
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("mock_customer_mdm")

app = FastAPI(title="Mock Customer MDM API")

# Mock Database
DEVICE_CATALOG = {
    "forceps": {
        "detection_label": "forceps",
        "device_name": "Tissue Forceps, Ring (FDA Class I)",
        "product_code": "GZY",
        "device_class": "I",
        "medical_specialty": "General Surgery",
        "data_source": "mdm"
    },
    "scalpel": {
        "detection_label": "scalpel",
        "device_name": "Surgical Scalpel, Disposable",
        "product_code": "KRO",
        "device_class": "II",
        "medical_specialty": "General Surgery",
        "data_source": "mdm"
    },
    "scissors": {
        "detection_label": "scissors",
        "device_name": "Metzenbaum Scissors, Curved",
        "product_code": "LWI",
        "device_class": "I",
        "medical_specialty": "General Surgery",
        "data_source": "mdm"
    },
    "needle_holder": {
        "detection_label": "needle_holder",
        "device_name": "Mayo-Hegar Needle Holder",
        "product_code": "GPR",
        "device_class": "I",
        "medical_specialty": "General Surgery",
        "data_source": "mdm"
    },
    "retractor": {
        "detection_label": "retractor",
        "device_name": "Senn Retractor, 3-Prong",
        "product_code": "GCL",
        "device_class": "I",
        "medical_specialty": "General Surgery",
        "data_source": "mdm"
    },
    "clamp": {
        "detection_label": "clamp",
        "device_name": "Kelly Hemostatic Clamp, Curved",
        "product_code": "GEZ",
        "device_class": "I",
        "medical_specialty": "General Surgery",
        "data_source": "mdm"
    }
}

API_KEY = os.getenv("CUSTOMER_MDM_API_KEY", "harness_secret_token_123")

async def verify_token(authorization: str = Header(None)):
    if not authorization:
        # In this mock, we'll log a warning but allow it if not strictly enforced
        logger.warning("Missing Authorization header")
        return
    
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header format")
    
    token = authorization.split(" ")[1]
    if token != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API Key")

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.get("/device/lookup")
async def lookup(label: str, _ = Depends(verify_token)):
    normalized = label.lower().strip()
    logger.info(f"Lookup request for label: {normalized}")
    
    device = DEVICE_CATALOG.get(normalized)
    if not device:
        logger.warning(f"Label not found: {normalized}")
        raise HTTPException(status_code=404, detail="Device not found")
    
    return device

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8007)

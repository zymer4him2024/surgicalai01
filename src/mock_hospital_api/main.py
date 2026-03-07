"""
mock_hospital_api/main.py — Hospital/vendor instrument catalog mock API (Port 8006)

Simulates a real hospital MDM system.
Replace HOSPITAL_API_URL env var with the real hospital server after integration testing.

Contract:
  POST /instruments/lookup
  Header: X-API-Key: <key>
  Body:   {"labels": ["scissors", "cautery pen"]}
  200:    {"matches": {"scissors": {...}, "cautery pen": null}, "hits": 1}

  GET  /health   — API status (no auth required)
  GET  /catalog  — full catalog (auth required)
"""

from __future__ import annotations

import logging
import os

import uvicorn
from fastapi import FastAPI, Header, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("mock_hospital_api")

MODULE_NAME = os.getenv("MODULE_NAME", "MockHospitalAPI")
API_KEY = os.getenv("HOSPITAL_API_KEY", "test-api-key-12345")

# ─────────────────────────────────────────────────────────────────────────────
# Instrument catalog (YOLO label lowercase → hospital device info)
# Replace with DB lookup in production
# ─────────────────────────────────────────────────────────────────────────────

_CATALOG: dict[str, dict] = {
    "scissors": {
        "catalog_id": "SC001",
        "name": "Mayo Straight Scissors 17cm",
        "manufacturer": "Aesculap",
        "fda_code": "HRQ",
        "device_class": "I",
        "unit_price_usd": 45.00,
        "reusable": True,
        "tray_location": "Tray A - Slot 3",
    },
    "surgical scissors": {
        "catalog_id": "SC002",
        "name": "Metzenbaum Curved Scissors 18cm",
        "manufacturer": "Aesculap",
        "fda_code": "HRQ",
        "device_class": "I",
        "unit_price_usd": 52.00,
        "reusable": True,
        "tray_location": "Tray A - Slot 4",
    },
    "forceps": {
        "catalog_id": "FC014",
        "name": "Ring Forceps 20cm",
        "manufacturer": "Lawton",
        "fda_code": "GZY",
        "device_class": "I",
        "unit_price_usd": 38.00,
        "reusable": True,
        "tray_location": "Tray A - Slot 6",
    },
    "scalpel": {
        "catalog_id": "SL003",
        "name": "Scalpel Handle No. 3",
        "manufacturer": "Swann-Morton",
        "fda_code": "KZH",
        "device_class": "I",
        "unit_price_usd": 12.00,
        "reusable": True,
        "tray_location": "Tray A - Slot 1",
    },
    "cautery pen": {
        "catalog_id": "EP007",
        "name": "Electrosurgical Pencil (Bovie)",
        "manufacturer": "Medline",
        "fda_code": "GXP",
        "device_class": "II",
        "unit_price_usd": 8.50,
        "reusable": False,
        "tray_location": "Tray B - Slot 1",
    },
    "clamp": {
        "catalog_id": "CL021",
        "name": "Kocher Clamp 18cm",
        "manufacturer": "Aesculap",
        "fda_code": "FYN",
        "device_class": "I",
        "unit_price_usd": 67.00,
        "reusable": True,
        "tray_location": "Tray A - Slot 8",
    },
    "overholt clamp": {
        "catalog_id": "CL022",
        "name": "Overholt Dissecting Forceps 20cm",
        "manufacturer": "Aesculap",
        "fda_code": "FYN",
        "device_class": "I",
        "unit_price_usd": 72.00,
        "reusable": True,
        "tray_location": "Tray A - Slot 9",
    },
    "retractor": {
        "catalog_id": "RT009",
        "name": "Richardson Retractor Large",
        "manufacturer": "V. Mueller",
        "fda_code": "FYN",
        "device_class": "I",
        "unit_price_usd": 89.00,
        "reusable": True,
        "tray_location": "Tray B - Slot 3",
    },
    "needle holder": {
        "catalog_id": "NH011",
        "name": "Mayo-Hegar Needle Holder 18cm",
        "manufacturer": "Aesculap",
        "fda_code": "GAM",
        "device_class": "I",
        "unit_price_usd": 55.00,
        "reusable": True,
        "tray_location": "Tray A - Slot 10",
    },
    "suction": {
        "catalog_id": "SU004",
        "name": "Yankauer Suction Tip",
        "manufacturer": "Medline",
        "fda_code": "KZG",
        "device_class": "II",
        "unit_price_usd": 3.20,
        "reusable": False,
        "tray_location": "Tray B - Slot 2",
    },
    "drain": {
        "catalog_id": "DR002",
        "name": "Jackson-Pratt Drain 7mm",
        "manufacturer": "Cardinal Health",
        "fda_code": "FTF",
        "device_class": "II",
        "unit_price_usd": 18.00,
        "reusable": False,
        "tray_location": "Tray B - Slot 5",
    },
    "sponge": {
        "catalog_id": "SP008",
        "name": "Surgical Lap Sponge 4x4",
        "manufacturer": "Medline",
        "fda_code": "KZF",
        "device_class": "II",
        "unit_price_usd": 1.50,
        "reusable": False,
        "tray_location": "Tray C - Slot 1",
    },
    "trocar": {
        "catalog_id": "TR006",
        "name": "Laparoscopic Trocar 12mm",
        "manufacturer": "Ethicon",
        "fda_code": "GEI",
        "device_class": "II",
        "unit_price_usd": 22.00,
        "reusable": False,
        "tray_location": "Tray B - Slot 6",
    },
    "needle": {
        "catalog_id": "ND015",
        "name": "Surgical Suture Needle (Curved)",
        "manufacturer": "Ethicon",
        "fda_code": "GAM",
        "device_class": "I",
        "unit_price_usd": 4.00,
        "reusable": False,
        "tray_location": "Tray C - Slot 3",
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────

class LookupRequest(BaseModel):
    labels: list[str]


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Mock Hospital Instrument Catalog API",
    description="Mock hospital/vendor instrument catalog API for pre-integration testing.",
    version="1.0.0",
)


def _require_api_key(x_api_key: str | None) -> None:
    if x_api_key != API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key. Pass X-API-Key header.",
        )


@app.post("/instruments/lookup", summary="Batch lookup YOLO labels in hospital catalog")
async def lookup_instruments(
    body: LookupRequest,
    x_api_key: str | None = Header(None),
) -> JSONResponse:
    _require_api_key(x_api_key)

    result: dict[str, dict | None] = {}
    for label in body.labels:
        normalized = label.lower().strip()
        entry = _CATALOG.get(normalized)
        result[label] = entry
        if entry:
            logger.info("Catalog HIT : %r → %s (%s)", label, entry["catalog_id"], entry["name"])
        else:
            logger.info("Catalog MISS: %r", label)

    hits = sum(1 for v in result.values() if v is not None)
    return JSONResponse({
        "matches": result,
        "queried": len(body.labels),
        "hits": hits,
    })


@app.get("/health", summary="Health check (no auth required)")
async def health() -> JSONResponse:
    return JSONResponse({
        "status": "healthy",
        "module": MODULE_NAME,
        "catalog_size": len(_CATALOG),
    })


@app.get("/catalog", summary="Full catalog list (auth required)")
async def list_catalog(x_api_key: str | None = Header(None)) -> JSONResponse:
    _require_api_key(x_api_key)
    return JSONResponse({
        "count": len(_CATALOG),
        "instruments": _CATALOG,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "src.mock_hospital_api.main:app",
        host="0.0.0.0",
        port=8006,
        log_level="info",
    )

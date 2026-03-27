"""
main.py — Mock 3rd Party External AI (Edge Inference)

Simulates a 3rd party AI model running on the Raspberry Pi.
It intentionally uses a different endpoint (/predict) and JSON schema
than our native inference_agent to test the API Adapter Pattern.
"""

import asyncio
import logging
import random
import time
from typing import Any, Optional

from fastapi import FastAPI, File, Header, UploadFile
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mock_external_ai")

app = FastAPI(title="Mock 3rd Party Edge AI")

# Hardcoded fake detections to simulate model output
FAKE_DETECTIONS = [
    {"label": "scalpel", "score": 0.98, "box": [10, 20, 100, 200]},
    {"label": "forceps", "score": 0.95, "box": [30, 40, 150, 250]},
    {"label": "scissors", "score": 0.89, "box": [50, 60, 180, 300]},
]


class MockPredictResponse(BaseModel):
    success: bool
    inference_ms: float
    items: list[dict[str, Any]]
    device_temp_c: float


@app.post("/predict", response_model=MockPredictResponse)
async def predict(
    image: UploadFile = File(...),
    x_app_id: Optional[str] = Header(None, alias="X-App-ID"),
    x_device_id: Optional[str] = Header(None, alias="X-Device-ID"),
    authorization: Optional[str] = Header(None),
):
    """Simulates processing an image and returning 3rd party format detections."""
    start_time = time.time()

    # Log identity headers for integration verification
    missing: list[str] = []
    if not x_app_id:
        missing.append("X-App-ID")
    if not x_device_id:
        missing.append("X-Device-ID")
    if missing:
        logger.warning("Missing recommended headers: %s", missing)
    logger.info(
        "Predict called — X-App-ID=%s X-Device-ID=%s Auth=%s",
        x_app_id, x_device_id, "present" if authorization else "absent",
    )

    # Read image bytes just to simulate transfer
    _ = await image.read()

    # Simulate NPU/GPU inference time (40ms - 80ms)
    delay_s = random.uniform(0.04, 0.08)
    await asyncio.sleep(delay_s)

    # Return 1 to 3 random items
    num_items = random.randint(1, 3)
    detected_items = random.sample(FAKE_DETECTIONS, num_items)

    # Simulate a slightly fluctuating temperature
    fake_temp = round(random.uniform(60.0, 75.0), 1)

    inference_ms = round((time.time() - start_time) * 1000, 2)

    logger.info("Simulated detection of %d items in %.1f ms", len(detected_items), inference_ms)

    return MockPredictResponse(
        success=True,
        inference_ms=inference_ms,
        items=detected_items,
        device_temp_c=fake_temp
    )


@app.get("/integration/validate")
async def validate_integration(
    x_app_id: Optional[str] = Header(None, alias="X-App-ID"),
    x_device_id: Optional[str] = Header(None, alias="X-Device-ID"),
    authorization: Optional[str] = Header(None),
):
    """Vendors call this to verify their headers are being sent correctly."""
    checks = {
        "x_app_id": {
            "present": bool(x_app_id),
            "value": x_app_id,
        },
        "x_device_id": {
            "present": bool(x_device_id),
            "value": x_device_id,
        },
        "authorization": {
            "present": bool(authorization),
            "scheme": "Bearer" if authorization and authorization.startswith("Bearer ") else None,
        },
    }
    all_ok = all(c["present"] for c in checks.values())
    return {"valid": all_ok, "checks": checks}


@app.get("/status")
async def status():
    """Simulates a health/status endpoint for the 3rd party API."""
    return {"status": "ok", "model_loaded": True, "version": "1.0.0-mock"}


@app.get("/health")
async def health():
    """Health alias — Gateway calls /health on INFERENCE_URL to check reachability."""
    return {"status": "ok", "model_loaded": True, "version": "1.0.0-mock"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8006)

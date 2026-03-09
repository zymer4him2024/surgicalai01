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
from typing import Any

from fastapi import FastAPI, File, UploadFile
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
    processing_time_ms: float
    items: list[dict[str, Any]]
    device_temp_c: float


@app.post("/predict", response_model=MockPredictResponse)
async def predict(image: UploadFile = File(...)):
    """Simulates processing an image and returning 3rd party format detections."""
    start_time = time.time()
    
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
    
    processing_time_ms = round((time.time() - start_time) * 1000, 2)
    
    logger.info("Simulated detection of %d items in %.1f ms", len(detected_items), processing_time_ms)
    
    return MockPredictResponse(
        success=True,
        processing_time_ms=processing_time_ms,
        items=detected_items,
        device_temp_c=fake_temp
    )


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

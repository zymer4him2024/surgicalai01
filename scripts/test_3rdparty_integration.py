#!/usr/bin/env python3
"""
test_3rdparty_integration.py — Pseudo 3rd Party AI Transaction Test

Simulates the full pipeline:
  Mock 3rd Party AI  →  Gateway Adapter  →  Count Comparison  →  State Result

Run from project root:
  python scripts/test_3rdparty_integration.py
"""

import json
import random
import time
from collections import Counter
from typing import Any

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — Simulate mock 3rd party AI response (same schema as mock_external_ai)
# ─────────────────────────────────────────────────────────────────────────────

FAKE_DETECTIONS = [
    {"label": "scalpel",  "score": 0.98, "box": [10,  20,  100, 200]},
    {"label": "forceps",  "score": 0.95, "box": [30,  40,  150, 250]},
    {"label": "scissors", "score": 0.89, "box": [50,  60,  180, 300]},
]

def simulate_3rdparty_predict() -> dict[str, Any]:
    """Simulates POST /predict from mock_external_ai (port 8006)."""
    delay_ms = round(random.uniform(40, 80), 2)
    num_items = random.randint(1, 3)
    detected = random.sample(FAKE_DETECTIONS, num_items)
    return {
        "success": True,
        "inference_ms": delay_ms,
        "device_temp_c": round(random.uniform(60.0, 75.0), 1),
        "items": detected,
    }


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — Gateway adapter (copied from src/gateway/main.py)
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_inference_response(data: dict) -> dict:
    """Adapter pattern to normalize 3rd party AI response schemas."""
    if "success" in data and "items" in data:
        normalized_detections = []
        for i, item in enumerate(data.get("items", [])):
            normalized_detections.append({
                "class_id": i,
                "class_name": item.get("label", "unknown"),
                "confidence": item.get("score", 0.0),
                "bbox": item.get("box", [0, 0, 0, 0]),
            })
        return {
            "detections": normalized_detections,
            "inference_time_ms": data.get("inference_ms") or data.get("processing_time_ms", 0.0),
            "npu_temp_celsius": data.get("device_temp_c", 0.0),
            "thermal_status": "normal",
        }
    return data


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — State machine: compare actual vs target
# ─────────────────────────────────────────────────────────────────────────────

MATCH_TOLERANCE = 1  # ±1 count tolerance (same as gateway default)

def evaluate_job(detections: list[dict], target: dict[str, int]) -> dict:
    actual: Counter = Counter()
    for det in detections:
        actual[det["class_name"]] += 1

    mismatches = []
    for instrument, required in target.items():
        actual_count = actual.get(instrument, 0)
        if abs(actual_count - required) > MATCH_TOLERANCE:
            mismatches.append({
                "instrument": instrument,
                "required": required,
                "actual": actual_count,
                "delta": actual_count - required,
            })

    return {
        "actual_counts": dict(actual),
        "target": target,
        "mismatches": mismatches,
        "state": "MATCH" if not mismatches else "ERROR",
    }


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — Run transaction and print report
# ─────────────────────────────────────────────────────────────────────────────

DIVIDER = "─" * 60

def run_test(job_id: str, target: dict[str, int]) -> None:
    print(f"\n{DIVIDER}")
    print(f"  JOB: {job_id}")
    print(f"  TARGET: {json.dumps(target)}")
    print(DIVIDER)

    # Simulate 3rd party call
    t0 = time.perf_counter()
    raw_response = simulate_3rdparty_predict()
    call_ms = round((time.perf_counter() - t0) * 1000, 2)

    print("\n[1] 3rd Party AI Response (POST /predict → port 8006)")
    print(json.dumps(raw_response, indent=2))

    # Normalize
    normalized = _normalize_inference_response(raw_response)
    print("\n[2] After Gateway Adapter (_normalize_inference_response)")
    print(json.dumps(normalized, indent=2))

    # Evaluate
    result = evaluate_job(normalized["detections"], target)
    print("\n[3] State Machine Evaluation")
    print(f"    Actual counts : {result['actual_counts']}")
    print(f"    Target        : {result['target']}")
    if result["mismatches"]:
        for m in result["mismatches"]:
            direction = "over" if m["delta"] > 0 else "under"
            print(f"    MISMATCH      : {m['instrument']} — need {m['required']}, got {m['actual']} ({direction} by {abs(m['delta'])})")
    print(f"\n    STATE → {result['state']}")
    print(f"    Round-trip    : {call_ms:.2f} ms (simulated)")
    print(DIVIDER)


if __name__ == "__main__":
    print("=" * 60)
    print("  3rd Party AI Integration — Pseudo Transaction Test")
    print("=" * 60)

    # Run 3 test scenarios
    run_test(
        job_id="TRAY-TEST-01",
        target={"scissors": 1, "scalpel": 1, "forceps": 1},
    )
    run_test(
        job_id="TRAY-TEST-02",
        target={"scissors": 2},
    )
    run_test(
        job_id="TRAY-TEST-03",
        target={"scalpel": 1},
    )

    print("\nTest complete.")

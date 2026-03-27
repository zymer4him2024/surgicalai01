"""Counting logic and state transitions for gas cylinder inventory."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.gas_gateway.schemas import CountSnapshot, GasState


@dataclass
class GasCountingState:
    """Domain state for gas inventory counting.

    All mutations must be done under _lock.
    Invariant: low_stock == True implies total_count < low_stock_threshold.
    """
    # _lock protects: state, total_count, class_counts
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    state: GasState = GasState.COUNTING
    total_count: int = 0
    class_counts: dict[str, int] = field(default_factory=dict)
    low_stock_threshold: int = 5
    location: str = ""
    operator_id: str = ""
    last_sync_at: float = 0.0


async def update_count(
    s: GasCountingState,
    detections: list[dict],
) -> bool:
    """Update total count from inference results. Returns True if state changed."""
    counts: dict[str, int] = {}
    total = 0
    for d in detections:
        name = d.get("class_name", "unknown")
        if name.lower() == "background":
            continue
        counts[name] = counts.get(name, 0) + 1
        total += 1

    async with s._lock:
        s.total_count = total
        s.class_counts = counts

        prev_state = s.state
        if total < s.low_stock_threshold:
            s.state = GasState.LOW_STOCK
        else:
            s.state = GasState.COUNTING
        return s.state != prev_state


def should_sync(s: GasCountingState, interval: float) -> bool:
    """Check if enough time has elapsed for periodic sync."""
    return (time.monotonic() - s.last_sync_at) >= interval


def build_snapshot(
    s: GasCountingState,
    trigger: str,
    app_id: str,
    device_id: str,
) -> CountSnapshot:
    """Create a snapshot payload for Firebase/customer DB."""
    return CountSnapshot(
        device_id=device_id,
        app_id=app_id,
        total_count=s.total_count,
        low_stock=s.state == GasState.LOW_STOCK,
        location=s.location,
        operator_id=s.operator_id,
        trigger=trigger,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )

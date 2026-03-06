"""
tests/test_firebase_sync.py — Firebase Sync Agent 통합 테스트

테스트 흐름:
  1. POST /sync  → 이벤트 큐 삽입 (202 Accepted)
  2. GET /queue/item/{id} 폴링 → status=done 대기
  3. firestore_doc_id 획득
  4. CLAUDE.md / GEMINI.md에 결과 기록

실행:
  pytest tests/test_firebase_sync.py -v -s

사전 조건:
  - firebase_sync_agent 컨테이너 실행 중 (Port 8004 노출 필요)
    docker-compose.mac.yml 기준: 8004:8004 포트 매핑 후 실행
  - 또는 로컬에서 직접 실행:
    python -m src.firebase_sync.main
"""

from __future__ import annotations

import pathlib
import time
from datetime import datetime, timezone

import httpx
import pytest

BASE_URL = "http://localhost:8004"
CLAUDE_MD = pathlib.Path(__file__).parents[1] / "CLAUDE.md"
GEMINI_MD = pathlib.Path(__file__).parents[1] / "GEMINI.md"
MAX_WAIT_SEC = 30          # 업로드 완료 최대 대기 시간
POLL_INTERVAL = 0.5


# ─────────────────────────────────────────────────────────────────────────────
# 픽스처
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=BASE_URL, timeout=15.0) as c:
        yield c


# ─────────────────────────────────────────────────────────────────────────────
# 헬스 체크
# ─────────────────────────────────────────────────────────────────────────────

def test_health(client: httpx.Client) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["module"] == "FirebaseSyncAgent"
    assert "firebase_configured" in data
    assert "queue_depth" in data
    print(f"\n  ✓ Health OK — simulation={data['simulation_mode']}")


# ─────────────────────────────────────────────────────────────────────────────
# 큐 삽입 테스트
# ─────────────────────────────────────────────────────────────────────────────

def test_sync_enqueue(client: httpx.Client) -> None:
    payload = {
        "event_type": "mismatch",
        "expected_count": 3,
        "actual_count": 2,
        "missing_items": ["forceps"],
        "detected_items": [
            {"class_name": "scalpel", "confidence": 0.92, "bbox": [10, 20, 100, 80]},
            {"class_name": "scissors", "confidence": 0.85, "bbox": [150, 30, 250, 120]},
        ],
        "metadata": {"operator": "test_runner", "case_id": "TC-001"},
    }
    resp = client.post("/sync", json=payload)
    assert resp.status_code == 202, resp.text
    data = resp.json()
    assert data["status"] == "queued"
    assert isinstance(data["event_id"], int)
    print(f"\n  ✓ Enqueued — event_id={data['event_id']}")


# ─────────────────────────────────────────────────────────────────────────────
# 핵심 테스트: 업로드 완료 → doc_id → CLAUDE.md 기록
# ─────────────────────────────────────────────────────────────────────────────

def test_upload_and_record_doc_id(client: httpx.Client) -> None:
    """
    Firebase 업로드 완료 후 Firestore 문서 ID를
    CLAUDE.md 및 GEMINI.md에 자동 기록합니다.
    """
    # ── 1) MISMATCH 이벤트 큐 삽입 ───────────────────────────────────────────
    payload = {
        "event_type": "mismatch",
        "expected_count": 5,
        "actual_count": 3,
        "missing_items": ["needle_holder", "clamp"],
        "detected_items": [],
        "metadata": {"test": True, "case_id": "TC-UPLOAD-001"},
    }
    enqueue_resp = client.post("/sync", json=payload)
    assert enqueue_resp.status_code == 202
    event_id: int = enqueue_resp.json()["event_id"]
    print(f"\n  → Queued event_id={event_id}")

    # ── 2) 업로드 완료 폴링 ──────────────────────────────────────────────────
    doc_id: str | None = None
    storage_urls: list[str] = []
    deadline = time.monotonic() + MAX_WAIT_SEC

    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
        item_resp = client.get(f"/queue/item/{event_id}")
        assert item_resp.status_code == 200, item_resp.text
        item = item_resp.json()

        if item["status"] == "done":
            doc_id = item["firestore_doc_id"]
            storage_urls = item.get("storage_urls", [])
            break
        if item["status"] == "failed":
            pytest.fail(f"Upload failed: {item.get('error_message')}")

    assert doc_id is not None, (
        f"Upload did not complete within {MAX_WAIT_SEC}s "
        f"(last status: {item.get('status')})"
    )
    print(f"  ✓ Upload done — doc_id={doc_id}")
    print(f"    Storage URLs ({len(storage_urls)} snapshots):")
    for url in storage_urls:
        print(f"      {url}")

    # ── 3) CLAUDE.md / GEMINI.md에 결과 기록 ─────────────────────────────────
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    entry = _build_record_entry(event_id, doc_id, storage_urls, timestamp)

    for md_path in (CLAUDE_MD, GEMINI_MD):
        _append_record(md_path, entry)
        print(f"  ✓ Recorded in {md_path.name}")

    # ── 4) 검증 ──────────────────────────────────────────────────────────────
    assert len(doc_id) > 0
    # 시뮬레이션 모드: sim_ 접두사, 실제 모드: Firestore 자동 ID
    assert doc_id.startswith("sim_") or len(doc_id) == 20, (
        f"Unexpected doc_id format: {doc_id}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 오프라인 내성 테스트 (큐 깊이 확인)
# ─────────────────────────────────────────────────────────────────────────────

def test_queue_status(client: httpx.Client) -> None:
    resp = client.get("/queue/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_pending" in data
    assert "total_done" in data
    assert "firebase_reachable" in data
    assert "simulation_mode" in data
    total = data["total_done"] + data["total_failed"] + data["total_pending"]
    assert total >= 0
    print(f"\n  ✓ Queue status — done={data['total_done']}, "
          f"pending={data['total_pending']}, failed={data['total_failed']}")


# ─────────────────────────────────────────────────────────────────────────────
# 스냅샷 모듈 단위 테스트 (asyncio)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_snapshot_capture_simulation() -> None:
    """스냅샷 캡처: display_agent 없어도 시뮬레이션 이미지 3장 생성 확인."""
    import asyncio

    import numpy as np

    from src.firebase_sync.snapshot import (
        EXPOSURE_MULTIPLIERS,
        capture_snapshots,
    )

    async with httpx.AsyncClient(timeout=3.0) as client:
        shots = await capture_snapshots(client)

    assert len(shots) == 3, f"Expected 3 shots, got {len(shots)}"

    for i, shot in enumerate(shots):
        assert shot["shot"] == i + 1
        assert len(shot["jpeg_bytes"]) > 1000, "JPEG too small"
        assert "timestamp" in shot
        assert shot["exposure_multiplier"] == EXPOSURE_MULTIPLIERS[i]

    # 노출 보정 확인: shot2(어둡게) 평균값 < shot1(표준) < shot3(밝게)
    def avg_brightness(jpeg: bytes) -> float:
        import cv2
        arr = np.frombuffer(jpeg, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        return float(img.mean()) if img is not None else 0.0

    b1 = avg_brightness(shots[0]["jpeg_bytes"])
    b2 = avg_brightness(shots[1]["jpeg_bytes"])
    b3 = avg_brightness(shots[2]["jpeg_bytes"])
    assert b2 < b1, f"Under-exposed shot should be darker: {b2:.1f} vs {b1:.1f}"
    assert b3 > b1, f"Over-exposed shot should be brighter: {b3:.1f} vs {b1:.1f}"
    print(f"\n  ✓ Exposure: under={b2:.1f} < standard={b1:.1f} < over={b3:.1f}")


# ─────────────────────────────────────────────────────────────────────────────
# 헬퍼
# ─────────────────────────────────────────────────────────────────────────────

def _build_record_entry(
    event_id: int,
    doc_id: str,
    storage_urls: list[str],
    timestamp: str,
) -> str:
    urls_md = "\n".join(f"    - `{url}`" for url in storage_urls)
    return (
        f"\n\n### Firebase 업로드 기록 (자동 생성)\n"
        f"| 항목 | 값 |\n"
        f"|---|---|\n"
        f"| 이벤트 ID | `{event_id}` |\n"
        f"| Firestore 문서 ID | `{doc_id}` |\n"
        f"| 컬렉션 | `sync_events` |\n"
        f"| 스냅샷 수 | {len(storage_urls)}장 |\n"
        f"| 기록 시각 | {timestamp} |\n\n"
        f"Storage URLs:\n{urls_md}\n"
    )


def _append_record(md_path: pathlib.Path, entry: str) -> None:
    """CLAUDE.md / GEMINI.md 맨 끝에 기록 추가."""
    current = md_path.read_text(encoding="utf-8")
    # 중복 기록 방지: 동일 섹션이 이미 있으면 교체
    marker = "### Firebase 업로드 기록 (자동 생성)"
    if marker in current:
        # 기존 섹션 제거 후 새로 추가
        current = current[: current.index(marker)].rstrip()
    md_path.write_text(current + entry, encoding="utf-8")

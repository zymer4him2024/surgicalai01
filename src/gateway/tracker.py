"""
tracker.py — Lightweight ByteTrack-inspired Object Tracker

Hailo-8 HEF 모델은 Ultralytics model.track()를 직접 사용할 수 없으므로,
raw detection 결과를 받아 IoU 매칭 + EMA 스무딩 + 고유 ID 카운팅을 수행합니다.

핵심 기능:
  1) IoU 기반 매칭: 이전 프레임의 트랙과 현재 프레임의 탐지를 매칭
  2) EMA 스무딩: 바운딩 박스 좌표의 프레임간 떨림(jitter) 억제
  3) Track ID 할당: 동일 물체에 안정적인 고유 ID 부여
  4) 고유 카운팅: track_id 기반으로 물리적 객체를 정확히 1회만 카운팅
  5) Track 에이징: max_age 프레임 동안 미매칭 시 트랙 삭제

사용법:
  tracker = SurgicalTracker(max_age=30, min_hits=3, iou_threshold=0.3)
  
  # 매 프레임마다:
  tracked = tracker.update(detections)
  counts = tracker.get_counts()
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


# ─────────────────────────────────────────────────────────────────────────────
# IoU 계산
# ─────────────────────────────────────────────────────────────────────────────

def _iou(a: list[float], b: list[float]) -> float:
    """두 바운딩 박스 [x1, y1, x2, y2]의 IoU 계산."""
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 단일 트랙 (하나의 추적 대상)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Track:
    """단일 물체의 추적 상태."""
    track_id: int
    class_name: str
    bbox: list[float]           # [x1, y1, x2, y2] — EMA 스무딩 적용
    confidence: float
    hits: int = 1               # 연속 매칭 횟수
    age: int = 0                # 마지막 매칭 이후 경과 프레임 수
    total_hits: int = 1         # 전체 매칭 횟수 (생존 기간)
    first_seen: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)

    @property
    def is_confirmed(self) -> bool:
        """min_hits 이상 매칭되어야 확정 트랙으로 간주."""
        return self.total_hits >= 3

    def predict(self) -> list[float]:
        """다음 프레임의 예상 위치 (정적 장면이므로 현재 위치 유지)."""
        return self.bbox.copy()

    def update(self, det: dict, alpha: float = 0.6) -> None:
        """새 탐지 결과로 트랙 업데이트 (EMA 스무딩)."""
        new_bbox = det["bbox"]
        # EMA: alpha * new + (1 - alpha) * old
        self.bbox = [
            round(alpha * n + (1 - alpha) * o, 2)
            for n, o in zip(new_bbox, self.bbox)
        ]
        self.confidence = det["confidence"]
        self.class_name = det["class_name"]
        self.hits += 1
        self.total_hits += 1
        self.age = 0
        self.last_seen = time.monotonic()


# ─────────────────────────────────────────────────────────────────────────────
# SurgicalTracker — ByteTrack 스타일 메인 트래커
# ─────────────────────────────────────────────────────────────────────────────

class SurgicalTracker:
    """
    수술 기구 전용 경량 트래커.
    
    Parameters:
        max_age: 미매칭 허용 프레임 수 (이후 트랙 삭제)
        min_hits: 확정 트랙으로 표시되기 위한 최소 연속 매칭 횟수
        iou_threshold: 트랙-탐지 매칭 IoU 임계값
        ema_alpha: EMA 스무딩 계수 (1.0 = 스무딩 없음, 0.5 = 50:50 혼합)
    """

    def __init__(
        self,
        max_age: int = 30,
        min_hits: int = 3,
        iou_threshold: float = 0.3,
        ema_alpha: float = 0.6,
    ) -> None:
        self.max_age = max_age
        self.min_hits = min_hits
        self.iou_threshold = iou_threshold
        self.ema_alpha = ema_alpha
        self._next_id = 1
        self.tracks: list[Track] = []

    def update(self, detections: list[dict]) -> list[dict]:
        """
        새 프레임의 탐지 결과를 받아 트랙을 업데이트합니다.

        Args:
            detections: [{"class_name": str, "confidence": float, "bbox": [x1,y1,x2,y2], ...}, ...]

        Returns:
            확정된 트랙의 탐지 결과 리스트 (track_id 포함)
        """
        # 1) 모든 기존 트랙의 age 증가
        for track in self.tracks:
            track.age += 1

        # 2) 헝가리안 알고리즘 대신 Greedy IoU 매칭 (경량화)
        matched_track_indices: set[int] = set()
        matched_det_indices: set[int] = set()

        # IoU 행렬을 생성하여 가장 높은 IoU부터 매칭
        iou_pairs: list[tuple[float, int, int]] = []
        for ti, track in enumerate(self.tracks):
            for di, det in enumerate(detections):
                # 같은 클래스만 매칭 시도
                if track.class_name != det.get("class_name"):
                    continue
                score = _iou(track.predict(), det["bbox"])
                if score >= self.iou_threshold:
                    iou_pairs.append((score, ti, di))

        # IoU 내림차순 정렬 후 Greedy 매칭
        iou_pairs.sort(key=lambda x: x[0], reverse=True)
        for iou_score, ti, di in iou_pairs:
            if ti in matched_track_indices or di in matched_det_indices:
                continue
            self.tracks[ti].update(detections[di], alpha=self.ema_alpha)
            matched_track_indices.add(ti)
            matched_det_indices.add(di)

        # 3) 미매칭 탐지 → 새 트랙 생성
        for di, det in enumerate(detections):
            if di not in matched_det_indices:
                new_track = Track(
                    track_id=self._next_id,
                    class_name=det.get("class_name", "unknown"),
                    bbox=det["bbox"].copy(),
                    confidence=det["confidence"],
                )
                self.tracks.append(new_track)
                self._next_id += 1

        # 4) 죽은 트랙 제거 (max_age 초과)
        self.tracks = [t for t in self.tracks if t.age <= self.max_age]

        # 5) 확정 트랙만 반환 (min_hits 이상 매칭된 것)
        results = []
        for track in self.tracks:
            if track.is_confirmed and track.age == 0:  # 현재 프레임에서 매칭된 확정 트랙만
                results.append({
                    "track_id": track.track_id,
                    "class_id": 0,  # 필요 시 det에서 가져오기
                    "class_name": track.class_name,
                    "confidence": track.confidence,
                    "bbox": track.bbox,
                })
        return results

    def get_counts(self) -> dict[str, int]:
        """
        현재 활성 트랙 기준 고유 카운팅.
        확정된(confirmed) 트랙만 카운트합니다.
        Background 클래스는 제외합니다.
        """
        counts: dict[str, int] = {}
        for track in self.tracks:
            if not track.is_confirmed:
                continue
            if track.age > self.max_age:
                continue
            if track.class_name == "Background":
                continue
            counts[track.class_name] = counts.get(track.class_name, 0) + 1
        return counts

    def get_active_track_count(self) -> int:
        """현재 활성 확정 트랙 수."""
        return sum(1 for t in self.tracks if t.is_confirmed and t.age <= self.max_age)

    def reset(self) -> None:
        """새 Job 시작 시 모든 트랙 초기화."""
        self.tracks.clear()
        self._next_id = 1

"""
tracker.py — Lightweight ByteTrack-inspired object tracker

Hailo-8 HEF models cannot use Ultralytics model.track() directly,
so this tracker implements IoU matching + EMA smoothing + unique ID counting
on raw detection results.

Key features:
  1) IoU-based matching: match current frame detections to existing tracks
  2) EMA smoothing: suppress bounding box jitter between frames
  3) Track ID assignment: stable unique IDs per physical object
  4) Unique counting: count each physical object exactly once via track_id
  5) Track aging: delete tracks unmatched for max_age frames

Usage:
  tracker = SurgicalTracker(max_age=30, min_hits=3, iou_threshold=0.3)

  # Per frame:
  tracked = tracker.update(detections)
  counts = tracker.get_counts()
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


# ─────────────────────────────────────────────────────────────────────────────
# IoU calculation
# ─────────────────────────────────────────────────────────────────────────────

def _iou(a: list[float], b: list[float]) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Single track
# ─────────────────────────────────────────────────────────────────────────────

_CLASS_HISTORY_LEN = 10   # majority-vote window
_SIZE_LOCK_AGE = 12       # frames before width/height EMA is frozen


@dataclass
class Track:
    track_id: int
    class_name: str
    bbox: list[float]       # [x1, y1, x2, y2] — EMA smoothed
    confidence: float
    hits: int = 1           # consecutive match count
    age: int = 0            # frames since last match
    total_hits: int = 1     # total match count (lifetime)
    first_seen: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)
    class_history: list = field(default_factory=list)  # rolling class votes

    @property
    def is_confirmed(self) -> bool:
        return self.total_hits >= 2

    def predict(self) -> list[float]:
        return self.bbox.copy()

    def _majority_class(self) -> str:
        if not self.class_history:
            return self.class_name
        return max(set(self.class_history), key=self.class_history.count)

    def update(self, det: dict, alpha: float = 0.6) -> None:
        new_bbox = det["bbox"]
        x1_new, y1_new, x2_new, y2_new = new_bbox
        x1_old, y1_old, x2_old, y2_old = self.bbox

        # Center coordinates: stay responsive at full alpha
        cx_new = (x1_new + x2_new) / 2
        cy_new = (y1_new + y2_new) / 2
        cx_old = (x1_old + x2_old) / 2
        cy_old = (y1_old + y2_old) / 2
        cx = alpha * cx_new + (1 - alpha) * cx_old
        cy = alpha * cy_new + (1 - alpha) * cy_old

        # Width/Height: freeze after object is stable (size lock)
        w_new = x2_new - x1_new
        h_new = y2_new - y1_new
        w_old = x2_old - x1_old
        h_old = y2_old - y1_old
        size_alpha = 0.01 if self.total_hits > _SIZE_LOCK_AGE else alpha
        w = size_alpha * w_new + (1 - size_alpha) * w_old
        h = size_alpha * h_new + (1 - size_alpha) * h_old

        self.bbox = [
            round(cx - w / 2, 2), round(cy - h / 2, 2),
            round(cx + w / 2, 2), round(cy + h / 2, 2),
        ]

        # Class voting: majority vote over last N frames
        self.class_history.append(det["class_name"])
        if len(self.class_history) > _CLASS_HISTORY_LEN:
            self.class_history.pop(0)
        self.class_name = self._majority_class()

        self.confidence = det["confidence"]
        self.hits += 1
        self.total_hits += 1
        self.age = 0
        self.last_seen = time.monotonic()


# ─────────────────────────────────────────────────────────────────────────────
# SurgicalTracker — ByteTrack-style main tracker
# ─────────────────────────────────────────────────────────────────────────────

class SurgicalTracker:
    """
    Lightweight tracker for surgical instruments.

    Parameters:
        max_age: frames to keep an unmatched track before deletion
        min_hits: consecutive matches required to confirm a track
        iou_threshold: IoU threshold for track-detection matching
        ema_alpha: EMA smoothing factor (1.0 = no smoothing, 0.5 = 50/50)
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
        Update tracks with new frame detections.

        Args:
            detections: [{"class_name": str, "confidence": float, "bbox": [x1,y1,x2,y2], ...}, ...]

        Returns:
            Confirmed tracks from the current frame (includes track_id).
        """
        for track in self.tracks:
            track.age += 1

        matched_track_indices: set[int] = set()
        matched_det_indices: set[int] = set()

        iou_pairs: list[tuple[float, int, int]] = []
        for ti, track in enumerate(self.tracks):
            for di, det in enumerate(detections):
                # Match by IoU only — class voting handles classification
                score = _iou(track.predict(), det["bbox"])
                if score >= self.iou_threshold:
                    iou_pairs.append((score, ti, di))

        iou_pairs.sort(key=lambda x: x[0], reverse=True)
        for iou_score, ti, di in iou_pairs:
            if ti in matched_track_indices or di in matched_det_indices:
                continue
            self.tracks[ti].update(detections[di], alpha=self.ema_alpha)
            matched_track_indices.add(ti)
            matched_det_indices.add(di)

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

        self.tracks = [t for t in self.tracks if t.age <= self.max_age]

        results = []
        for track in self.tracks:
            if track.is_confirmed and track.age == 0:
                results.append({
                    "track_id": track.track_id,
                    "class_id": 0,
                    "class_name": track.class_name,
                    "confidence": track.confidence,
                    "bbox": track.bbox,
                })
        return results

    def get_counts(self) -> dict[str, int]:
        """Count unique confirmed active tracks. Excludes Background class.

        Deduplicates same-class tracks that overlap > 40% IoU (safety net
        against tracker creating duplicate tracks for the same physical object).
        """
        active = [
            t for t in self.tracks
            if t.is_confirmed and t.age <= self.max_age and t.class_name != "Background"
        ]
        # Deduplicate: suppress same-class tracks with high IoU (keep higher total_hits)
        active.sort(key=lambda t: t.total_hits, reverse=True)
        kept: list[Track] = []
        for t in active:
            is_dup = False
            for k in kept:
                if t.class_name == k.class_name and _iou(t.bbox, k.bbox) > 0.4:
                    is_dup = True
                    break
            if not is_dup:
                kept.append(t)
        counts: dict[str, int] = {}
        for t in kept:
            counts[t.class_name] = counts.get(t.class_name, 0) + 1
        return counts

    def get_active_track_count(self) -> int:
        return sum(1 for t in self.tracks if t.is_confirmed and t.age <= self.max_age)

    def reset(self) -> None:
        self.tracks.clear()
        self._next_id = 1

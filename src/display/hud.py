"""
hud.py — HUD(Heads-Up Display) 렌더러

레이아웃 (1920×1080 기준):
  ┌─────────────────────────────────────────────────────────────┐
  │ [AI STATUS]  (좌상단)          [NETWORK]    (우상단)        │
  │  ● Inference  Ready             ● Gateway    Online         │
  │  ● FPS        24.3              ● Inference  Online         │
  │  ● NPU Temp   72°C              ● Camera     Online         │
  │  ● Thermal    Normal                                        │
  │                                                             │
  │                  (카메라 피드 + 바운딩 박스)                  │
  │                                                             │
  │ [TRAY INFO]  (좌하단)                                       │
  │  scalpel          × 2                                       │
  │  forceps          × 1                                       │
  │  ─────────────────────                                      │
  │  Total            3 pcs                                     │
  │                      [테두리 8px — 색상: 녹/황/적 보간]      │
  └─────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import cv2
import numpy as np

from src.display.buffer import _StateSnapshot, get_border_bgr
from src.display.schemas import BorderColor, Detection

# ─────────────────────────────────────────────────────────────────────────────
# 상수
# ─────────────────────────────────────────────────────────────────────────────

FONT = cv2.FONT_HERSHEY_DUPLEX
FONT_SMALL = cv2.FONT_HERSHEY_SIMPLEX

C_WHITE = (230, 230, 230)
C_GRAY = (150, 150, 150)
C_PANEL = (15, 15, 15)          # 패널 배경 (거의 검정)
C_PANEL_BORDER = (60, 60, 60)
C_HEADER = (200, 200, 200)

C_OK = (80, 220, 80)            # 녹색
C_WARN = (0, 200, 240)          # 황색
C_ERR = (50, 50, 220)           # 적색
C_OFF = (100, 100, 100)         # 회색 (오프라인)

# 탐지 박스 색상 (BGR)
C_BBOX = (0, 200, 255)          # 황금색
C_BBOX_TEXT_BG = (0, 140, 200)

BORDER_THICKNESS = 10
PANEL_ALPHA = 0.70              # 패널 배경 불투명도


# ─────────────────────────────────────────────────────────────────────────────
# HUD 렌더러
# ─────────────────────────────────────────────────────────────────────────────

class HUDRenderer:

    def render(self, canvas: np.ndarray, snap: _StateSnapshot) -> None:
        """canvas(백 버퍼)에 모든 HUD 요소를 합성."""
        h, w = canvas.shape[:2]

        # 1) 베이스 프레임 (카메라 영상)
        if snap.base_frame is not None:
            resized = cv2.resize(snap.base_frame, (w, h), interpolation=cv2.INTER_LINEAR)
            canvas[:] = resized
        else:
            canvas[:] = (18, 18, 18)  # 프레임 없을 때 어두운 배경
            self._draw_waiting(canvas)

        # 2) 바운딩 박스 오버레이
        self._draw_detections(canvas, snap.detections)

        # 3) HUD 패널들
        self._draw_ai_panel(canvas, snap, x=16, y=16)
        self._draw_network_panel(canvas, snap, x=w - 260, y=16)
        ROW_H = 26
        if snap.scan_info is not None:
            n_targets = sum(1 for v in snap.scan_info.target.values() if v > 0)
            data_ph = 62 + max(1, n_targets) * ROW_H
        else:
            data_ph = 0
        self._draw_data_panel(canvas, snap, x=16, y=h - 200 - data_ph - 8)
        self._draw_tray_panel(canvas, snap, x=16, y=h - 200)

        # 4) 매칭 상태 텍스트 (상단 중앙)
        self._draw_status_text(canvas, snap)

        # 5) 테두리 (항상 마지막 — 다른 요소 위에 그려짐)
        self._draw_border(canvas, snap)

        # 6) QR 플래시 배너 (테두리 위에 — 일시적 표시)
        if snap.flash_text:
            self._draw_flash_banner(canvas, snap.flash_text)

    # ── 대기 화면 ─────────────────────────────────────────────────────────────

    @staticmethod
    def _draw_waiting(canvas: np.ndarray) -> None:
        h, w = canvas.shape[:2]
        text = "Waiting for camera feed..."
        (tw, _), _ = cv2.getTextSize(text, FONT_SMALL, 0.8, 1)
        cv2.putText(canvas, text, ((w - tw) // 2, h // 2),
                    FONT_SMALL, 0.8, C_GRAY, 1, cv2.LINE_AA)

    # ── 탐지 박스 ─────────────────────────────────────────────────────────────

    @staticmethod
    def _draw_detections(canvas: np.ndarray, detections: list[Detection]) -> None:
        h, w = canvas.shape[:2]
        for det in detections:
            # 1) 바운딩 박스 변환
            x1, y1, x2, y2 = (int(v * s / 640) for v, s in
                               zip(det.bbox, [w, h, w, h]))
            cv2.rectangle(canvas, (x1, y1), (x2, y2), C_BBOX, 2, cv2.LINE_AA)
            
            # 2) 레이블
            label = f"{det.class_name} {det.confidence:.0%}"
            (lw, lh), _ = cv2.getTextSize(label, FONT_SMALL, 0.55, 1)
            cv2.rectangle(canvas, (x1, y1 - lh - 8), (x1 + lw + 6, y1),
                          C_BBOX_TEXT_BG, -1)
            cv2.putText(canvas, label, (x1 + 3, y1 - 4),
                        FONT_SMALL, 0.55, C_WHITE, 1, cv2.LINE_AA)
            
            # 3) 키포인트 (SurgeoNet 12 pts)
            if det.keypoints:
                for kp in det.keypoints:
                    kx, ky = int(kp[0] * w / 640), int(kp[1] * h / 640)
                    cv2.circle(canvas, (kx, ky), 3, (0, 255, 255), -1, cv2.LINE_AA)

    # ── AI STATUS 패널 (좌상단) ───────────────────────────────────────────────

    def _draw_ai_panel(
        self, canvas: np.ndarray, snap: _StateSnapshot, x: int, y: int
    ) -> None:
        ai = snap.ai_status
        pw, ph = 250, 152

        self._panel_bg(canvas, x, y, pw, ph)
        self._header(canvas, "[+] AI STATUS", x + 10, y + 22)

        rows = [
            ("Inference",
             "Ready" if ai.inference_ready else "Offline",
             C_OK if ai.inference_ready else C_ERR),
            ("FPS",
             f"{ai.fps:.1f}",
             C_OK if ai.fps > 15 else C_WARN),
            ("NPU Temp",
             f"{ai.npu_temp_celsius:.1f}°C" if ai.npu_temp_celsius else "N/A",
             _temp_color(ai.npu_temp_celsius)),
            ("Thermal",
             ai.thermal_status.capitalize(),
             C_OK if ai.thermal_status == "normal" else
             C_WARN if ai.thermal_status == "warning" else C_ERR),
        ]
        for i, (label, value, color) in enumerate(rows):
            ry = y + 48 + i * 26
            cv2.circle(canvas, (x + 16, ry - 5), 5, color, -1, cv2.LINE_AA)
            cv2.putText(canvas, label, (x + 28, ry),
                        FONT_SMALL, 0.48, C_GRAY, 1, cv2.LINE_AA)
            cv2.putText(canvas, value, (x + 140, ry),
                        FONT_SMALL, 0.50, C_WHITE, 1, cv2.LINE_AA)

    # ── NETWORK 패널 (우상단) ─────────────────────────────────────────────────

    def _draw_network_panel(
        self, canvas: np.ndarray, snap: _StateSnapshot, x: int, y: int
    ) -> None:
        net = snap.network_status
        pw, ph = 240, 126

        self._panel_bg(canvas, x, y, pw, ph)
        self._header(canvas, "[+] NETWORK", x + 10, y + 22)

        rows = [
            ("Gateway",   net.gateway),
            ("Inference", net.inference),
            ("Camera",    net.camera),
        ]
        for i, (label, online) in enumerate(rows):
            ry = y + 50 + i * 26
            color = C_OK if online else C_ERR
            status = "Online" if online else "Offline"
            cv2.circle(canvas, (x + 16, ry - 5), 5, color, -1, cv2.LINE_AA)
            cv2.putText(canvas, label, (x + 28, ry),
                        FONT_SMALL, 0.48, C_GRAY, 1, cv2.LINE_AA)
            cv2.putText(canvas, status, (x + 130, ry),
                        FONT_SMALL, 0.50, C_WHITE, 1, cv2.LINE_AA)

    # ── DATA INFO 패널 (TRAY INFO 바로 위) ───────────────────────────────────

    def _draw_data_panel(
        self, canvas: np.ndarray, snap: _StateSnapshot, x: int, y: int
    ) -> None:
        if snap.scan_info is None:
            return
        si = snap.scan_info
        targets = [(k, v) for k, v in si.target.items() if v > 0]
        ROW_H = 26
        pw = 310
        # Header (22) + Job (24) + Scan (24) + Targets
        ph = 76 + max(1, len(targets)) * ROW_H

        ch = canvas.shape[0]
        y = min(y, ch - ph - 8)

        self._panel_bg(canvas, x, y, pw, ph)
        self._header(canvas, "[+] DATA INFO", x + 10, y + 22)

        # Job ID (truncate to avoid running over the box)
        job_display = si.job_id if len(si.job_id) <= 26 else si.job_id[:23] + "..."
        cv2.putText(canvas, f"Job:  {job_display}", (x + 12, y + 46),
                    FONT_SMALL, 0.44, C_WHITE, 1, cv2.LINE_AA)

        # Scan time
        scan_str = si.scanned_at if si.scanned_at else "waiting..."
        cv2.putText(canvas, f"Scan: {scan_str}", (x + 12, y + 70),
                    FONT_SMALL, 0.40, C_GRAY, 1, cv2.LINE_AA)

        # Target class rows
        start_y = y + 70
        if not targets:
            cv2.putText(canvas, "No targets", (x + 12, start_y + ROW_H),
                        FONT_SMALL, 0.44, C_GRAY, 1, cv2.LINE_AA)
        else:
            for i, (cls, cnt) in enumerate(targets):
                ry = start_y + (i + 1) * ROW_H
                # Truncate class name to prevent overflow
                cls_disp = cls if len(cls) <= 20 else cls[:17] + "..."
                cv2.putText(canvas, cls_disp, (x + 12, ry),
                            FONT_SMALL, 0.44, C_WHITE, 1, cv2.LINE_AA)
                cv2.putText(canvas, f"x {cnt}", (x + 262, ry),
                            FONT_SMALL, 0.48, C_WARN, 1, cv2.LINE_AA)

    # ── TRAY INFO 패널 (좌하단) ───────────────────────────────────────────────

    def _draw_tray_panel(
        self, canvas: np.ndarray, snap: _StateSnapshot, x: int, y: int
    ) -> None:
        items = snap.tray_items
        row_h = 28
        ph = 52 + len(items) * row_h + 34   # 동적 높이
        pw = 310

        # y 위치가 캔버스 밖으로 나가지 않도록 보정
        h = canvas.shape[0]
        y = min(y, h - ph - 8)

        self._panel_bg(canvas, x, y, pw, ph)
        self._header(canvas, "[+] TRAY INFO", x + 10, y + 22)

        if not items:
            cv2.putText(canvas, "No objects detected", (x + 12, y + 50),
                        FONT_SMALL, 0.45, C_GRAY, 1, cv2.LINE_AA)
            return

        for i, item in enumerate(items):
            ry = y + 46 + i * row_h
            # device_name 있으면 우선 표시, 없으면 class_name 표시
            display_name = item.device_name if item.device_name else item.class_name
            # FDA 등급 배지 (있을 때만)
            if item.fda_class:
                badge = f"[{item.fda_class}]"
                cv2.putText(canvas, badge, (x + 12, ry),
                            FONT_SMALL, 0.40, C_GRAY, 1, cv2.LINE_AA)
                cv2.putText(canvas, display_name, (x + 40, ry),
                            FONT_SMALL, 0.46, C_WHITE, 1, cv2.LINE_AA)
            else:
                cv2.putText(canvas, display_name, (x + 12, ry),
                            FONT_SMALL, 0.46, C_WHITE, 1, cv2.LINE_AA)
            cv2.putText(canvas, f"x {item.count}", (x + 262, ry),
                        FONT_SMALL, 0.50, C_OK, 1, cv2.LINE_AA)

        # 구분선 + 합계
        sep_y = y + 46 + len(items) * row_h + 4
        cv2.line(canvas, (x + 10, sep_y), (x + pw - 10, sep_y), C_PANEL_BORDER, 1)
        total = sum(i.count for i in items)
        cv2.putText(canvas, "Total", (x + 12, sep_y + 20),
                    FONT_SMALL, 0.50, C_GRAY, 1, cv2.LINE_AA)
        cv2.putText(canvas, f"{total} pcs", (x + 240, sep_y + 20),
                    FONT_SMALL, 0.52, C_WARN, 1, cv2.LINE_AA)

    # ── 매칭 상태 텍스트 (상단 중앙) ─────────────────────────────────────────

    @staticmethod
    def _draw_status_text(canvas: np.ndarray, snap: _StateSnapshot) -> None:
        target = snap.target_border_color
        if target == BorderColor.GREEN:
            text, color = "YES MATCH", C_OK
        elif target == BorderColor.RED:
            text, color = "NO MATCH", C_ERR
        else:
            return  # yellow = standby, no text

        h, w = canvas.shape[:2]
        scale, thickness = 2.2, 3
        (tw, th), _ = cv2.getTextSize(text, FONT, scale, thickness)
        x = (w - tw) // 2
        y = 90
        # semi-transparent background
        pad = 20
        roi = canvas[y - th - pad: y + pad, x - pad: x + tw + pad]
        dark = np.zeros_like(roi)
        cv2.addWeighted(dark, 0.55, roi, 0.45, 0, roi)
        canvas[y - th - pad: y + pad, x - pad: x + tw + pad] = roi
        cv2.putText(canvas, text, (x, y), FONT, scale, color, thickness, cv2.LINE_AA)

    # ── QR 플래시 배너 (하단 중앙, 3초 표시) ────────────────────────────────

    @staticmethod
    def _draw_flash_banner(canvas: np.ndarray, text: str) -> None:
        h, w = canvas.shape[:2]
        scale, thickness = 1.4, 2
        (tw, th), _ = cv2.getTextSize(text, FONT, scale, thickness)
        x = (w - tw) // 2
        y = h - 55
        pad = 16
        y1, y2 = max(0, y - th - pad), min(h, y + pad)
        x1, x2 = max(0, x - pad), min(w, x + tw + pad)
        roi = canvas[y1:y2, x1:x2]
        dark = np.zeros_like(roi)
        cv2.addWeighted(dark, 0.65, roi, 0.35, 0, roi)
        canvas[y1:y2, x1:x2] = roi
        cv2.putText(canvas, text, (x, y), FONT, scale, C_WARN, thickness, cv2.LINE_AA)

    # ── 테두리 (색상 보간) ────────────────────────────────────────────────────

    @staticmethod
    def _draw_border(canvas: np.ndarray, snap: _StateSnapshot) -> None:
        h, w = canvas.shape[:2]
        color = get_border_bgr(snap)
        t = BORDER_THICKNESS
        # 사각형 4변을 두꺼운 선으로 그림 (rectangle의 음수 두께는 채우기라 사용 안 함)
        cv2.rectangle(canvas, (t // 2, t // 2), (w - t // 2, h - t // 2),
                      color, t, cv2.LINE_AA)

    # ── 공통 헬퍼 ─────────────────────────────────────────────────────────────

    @staticmethod
    def _panel_bg(
        canvas: np.ndarray, x: int, y: int, w: int, h: int
    ) -> None:
        """반투명 어두운 패널 배경."""
        roi = canvas[y: y + h, x: x + w]
        dark = np.full_like(roi, C_PANEL)
        cv2.addWeighted(dark, PANEL_ALPHA, roi, 1 - PANEL_ALPHA, 0, roi)
        canvas[y: y + h, x: x + w] = roi
        cv2.rectangle(canvas, (x, y), (x + w, y + h), C_PANEL_BORDER, 1)

    @staticmethod
    def _header(canvas: np.ndarray, text: str, x: int, y: int) -> None:
        cv2.putText(canvas, text, (x, y), FONT_SMALL, 0.55,
                    C_HEADER, 1, cv2.LINE_AA)


# ─────────────────────────────────────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────────────────────────────────────

def _temp_color(temp: float | None) -> tuple[int, int, int]:
    if temp is None:
        return C_GRAY
    if temp >= 90:
        return C_ERR
    if temp >= 80:
        return C_WARN
    return C_OK

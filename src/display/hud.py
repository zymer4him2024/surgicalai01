"""
hud.py — HUD (Heads-Up Display) renderer

Layout (1920x1080):
  ┌─────────────────────────────────────────────────────────────┐
  │ [AI STATUS]  (top-left)         [NETWORK]    (top-right)    │
  │  ● Inference  Ready              ● Gateway    Online         │
  │  ● FPS        24.3               ● Inference  Online         │
  │  ● NPU Temp   72°C               ● Camera     Online         │
  │  ● Thermal    Normal                                        │
  │                                                             │
  │              (camera feed + bounding boxes)                 │
  │                                                             │
  │ [TRAY INFO]  (bottom-left)                                  │
  │  scalpel          x 2                                       │
  │  forceps          x 1                                       │
  │  ─────────────────────                                      │
  │  Total            3 pcs                                     │
  │                       [border 8px — color: green/yellow/red]│
  └─────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import os

import cv2
import numpy as np

# Coordinate space of incoming bbox values — must match runner.py INPUT_SIZE
_BBOX_COORD_SIZE = int(os.getenv("INPUT_SIZE", "416"))

from src.display.buffer import _StateSnapshot, get_border_bgr
from src.display.schemas import BorderColor, Detection

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

FONT = cv2.FONT_HERSHEY_DUPLEX
FONT_SMALL = cv2.FONT_HERSHEY_SIMPLEX

C_WHITE = (230, 230, 230)
C_GRAY = (150, 150, 150)
C_PANEL = (15, 15, 15)
C_PANEL_BORDER = (60, 60, 60)
C_HEADER = (200, 200, 200)

C_OK = (80, 220, 80)
C_WARN = (0, 200, 240)
C_ERR = (50, 50, 220)
C_OFF = (100, 100, 100)

C_BBOX = (0, 200, 255)
C_BBOX_TEXT_BG = (0, 140, 200)

BORDER_THICKNESS = 10
PANEL_ALPHA = 0.70


# ─────────────────────────────────────────────────────────────────────────────
# HUD Renderer
# ─────────────────────────────────────────────────────────────────────────────

class HUDRenderer:

    def __init__(self) -> None:
        self._last_frame_id: int = -1
        self._cached_resized: np.ndarray | None = None
        self._last_tray_items: list = []  # persist tray data until new inference replaces it

    def render(self, canvas: np.ndarray, snap: _StateSnapshot) -> None:
        h, w = canvas.shape[:2]

        # 1) Base frame (camera feed)
        if snap.base_frame is not None:
            frame_id = id(snap.base_frame)
            if frame_id != self._last_frame_id:
                fh, fw = snap.base_frame.shape[:2]
                if fw == w and fh == h:
                    self._cached_resized = snap.base_frame
                else:
                    self._cached_resized = cv2.resize(
                        snap.base_frame, (w, h), interpolation=cv2.INTER_LINEAR
                    )
                self._last_frame_id = frame_id
            if self._cached_resized is not None:
                canvas[:] = self._cached_resized
        else:
            canvas[:] = (18, 18, 18)
            self._draw_waiting(canvas)

        # 2) Bounding box overlay
        self._draw_detections(canvas, snap.detections)

        # 3) HUD panels
        self._draw_ai_panel(canvas, snap, x=16, y=16)
        self._draw_network_panel(canvas, snap, x=w - 260, y=16)

        # Persist tray data until new inference arrives — only replace on non-empty update
        if snap.tray_items:
            self._last_tray_items = snap.tray_items
        display_tray_items = self._last_tray_items

        # TRAY INFO: responsive height, anchored from bottom
        tray_row_h = 28
        n_tray = len(display_tray_items) if display_tray_items else 1
        tray_ph = 52 + n_tray * tray_row_h + 34
        tray_y = h - tray_ph - 16

        # DATA INFO: same width as TRAY, 50% taller vertically, above TRAY
        if snap.scan_info is not None:
            if "total" in snap.scan_info.target:
                n_targets = 1
            else:
                n_targets = sum(1 for v in snap.scan_info.target.values() if v > 0)
            data_ph = 114 + max(1, n_targets) * 39
            data_y = tray_y - data_ph - 10
            self._draw_data_panel(canvas, snap, x=16, y=data_y)

        self._draw_tray_panel_impl(canvas, display_tray_items, x=16, y=tray_y)

        # 4) Match status text (top center)
        self._draw_status_text(canvas, snap)

        # 5) Border (always last — drawn on top of everything)
        self._draw_border(canvas, snap)

        # 6) QR flash banner (above border — temporary)
        if snap.flash_text:
            self._draw_flash_banner(canvas, snap.flash_text)

        # 7) Persistent center prompt (e.g. "Por favor, escaneie o código QR")
        if snap.center_text:
            self._draw_center_prompt(canvas, snap.center_text)

    # ── Waiting screen ────────────────────────────────────────────────────────

    @staticmethod
    def _draw_waiting(canvas: np.ndarray) -> None:
        h, w = canvas.shape[:2]
        text = "Waiting for camera feed..."
        (tw, _), _ = cv2.getTextSize(text, FONT_SMALL, 0.8, 1)
        cv2.putText(canvas, text, ((w - tw) // 2, h // 2),
                    FONT_SMALL, 0.8, C_GRAY, 1, cv2.LINE_AA)

    # ── Detection boxes ───────────────────────────────────────────────────────

    @staticmethod
    def _draw_detections(canvas: np.ndarray, detections: list[Detection]) -> None:
        h, w = canvas.shape[:2]
        for det in detections:
            x1, y1, x2, y2 = (int(v * s / _BBOX_COORD_SIZE) for v, s in
                               zip(det.bbox, [w, h, w, h]))
            cv2.rectangle(canvas, (x1, y1), (x2, y2), C_BBOX, 2, cv2.LINE_AA)

            label = f"{det.class_name} {det.confidence:.0%}"
            (lw, lh), _ = cv2.getTextSize(label, FONT_SMALL, 0.55, 1)
            cv2.rectangle(canvas, (x1, y1 - lh - 8), (x1 + lw + 6, y1),
                          C_BBOX_TEXT_BG, -1)
            cv2.putText(canvas, label, (x1 + 3, y1 - 4),
                        FONT_SMALL, 0.55, C_WHITE, 1, cv2.LINE_AA)

            dim_text = f"{x2 - x1}x{y2 - y1}px"
            (dw, dh), _ = cv2.getTextSize(dim_text, FONT_SMALL, 0.42, 1)
            cv2.rectangle(canvas, (x2 - dw - 6, y2 - dh - 6), (x2, y2),
                          C_BBOX_TEXT_BG, -1)
            cv2.putText(canvas, dim_text, (x2 - dw - 3, y2 - 4),
                        FONT_SMALL, 0.42, C_WHITE, 1, cv2.LINE_AA)

            if det.keypoints:
                for kp in det.keypoints:
                    kx, ky = int(kp[0] * w / 640), int(kp[1] * h / 640)
                    cv2.circle(canvas, (kx, ky), 3, (0, 255, 255), -1, cv2.LINE_AA)

    # ── AI STATUS panel (top-left) ────────────────────────────────────────────

    def _draw_ai_panel(
        self, canvas: np.ndarray, snap: _StateSnapshot, x: int, y: int
    ) -> None:
        ai = snap.ai_status
        pw, ph = 250, 178

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
             f"{ai.npu_temp_celsius:.1f}°C" if ai.npu_temp_celsius is not None else "N/A",
             _temp_color(ai.npu_temp_celsius)),
            ("CPU Temp",
             f"{ai.cpu_temp_celsius:.1f}°C" if ai.cpu_temp_celsius is not None else "N/A",
             _temp_color(ai.cpu_temp_celsius)),
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

    # ── NETWORK panel (top-right) ─────────────────────────────────────────────

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

    # ── DATA INFO panel (above TRAY INFO) ────────────────────────────────────

    def _draw_data_panel(
        self, canvas: np.ndarray, snap: _StateSnapshot, x: int, y: int
    ) -> None:
        if snap.scan_info is None:
            return
        si = snap.scan_info
        is_total_mode = "total" in si.target
        ROW_H = 39
        pw = 310

        if is_total_mode:
            # Total-count mode: compact panel showing just target count
            ph = 114 + ROW_H
        else:
            targets = [(k, v) for k, v in si.target.items() if v > 0]
            ph = 114 + max(1, len(targets)) * ROW_H

        ch = canvas.shape[0]
        y = max(16, min(y, ch - ph - 8))

        self._panel_bg(canvas, x, y, pw, ph)
        self._header(canvas, "[+] DATA INFO", x + 10, y + 28)

        job_display = si.job_id if len(si.job_id) <= 26 else si.job_id[:23] + "..."
        cv2.putText(canvas, f"Job:  {job_display}", (x + 12, y + 58),
                    FONT_SMALL, 0.52, C_WHITE, 1, cv2.LINE_AA)

        scan_str = si.scanned_at if si.scanned_at else "waiting..."
        cv2.putText(canvas, f"Scan: {scan_str}", (x + 12, y + 88),
                    FONT_SMALL, 0.46, C_GRAY, 1, cv2.LINE_AA)

        start_y = y + 88
        if is_total_mode:
            total_cnt = si.target["total"]
            ry = start_y + ROW_H
            cv2.putText(canvas, "Target Objects", (x + 12, ry),
                        FONT_SMALL, 0.52, C_WHITE, 1, cv2.LINE_AA)
            cv2.putText(canvas, f"x {total_cnt}", (x + 262, ry),
                        FONT_SMALL, 0.56, C_WARN, 1, cv2.LINE_AA)
        else:
            if not targets:
                cv2.putText(canvas, "No targets", (x + 12, start_y + ROW_H),
                            FONT_SMALL, 0.52, C_GRAY, 1, cv2.LINE_AA)
            else:
                for i, (cls, cnt) in enumerate(targets):
                    ry = start_y + (i + 1) * ROW_H
                    cls_disp = cls if len(cls) <= 20 else cls[:17] + "..."
                    cv2.putText(canvas, cls_disp, (x + 12, ry),
                                FONT_SMALL, 0.52, C_WHITE, 1, cv2.LINE_AA)
                    cv2.putText(canvas, f"x {cnt}", (x + 262, ry),
                                FONT_SMALL, 0.56, C_WARN, 1, cv2.LINE_AA)

    # ── TRAY INFO panel (bottom-left, responsive) ────────────────────────────

    def _draw_tray_panel_impl(
        self, canvas: np.ndarray, items: list, x: int, y: int
    ) -> None:
        row_h = 28
        n_items = len(items) if items else 1
        ph = 52 + n_items * row_h + 34
        pw = 310

        ch = canvas.shape[0]
        y = max(16, min(y, ch - ph - 8))

        self._panel_bg(canvas, x, y, pw, ph)
        self._header(canvas, "[+] TRAY INFO", x + 10, y + 22)

        if not items:
            cv2.putText(canvas, "No objects detected", (x + 12, y + 50),
                        FONT_SMALL, 0.45, C_GRAY, 1, cv2.LINE_AA)
            return

        for i, item in enumerate(items):
            ry = y + 46 + i * row_h
            display_name = item.device_name if item.device_name else item.class_name
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

        sep_y = y + 46 + len(items) * row_h + 4
        cv2.line(canvas, (x + 10, sep_y), (x + pw - 10, sep_y), C_PANEL_BORDER, 1)
        total = sum(i.count for i in items)
        cv2.putText(canvas, "Total", (x + 12, sep_y + 20),
                    FONT_SMALL, 0.50, C_GRAY, 1, cv2.LINE_AA)
        cv2.putText(canvas, f"{total} pcs", (x + 240, sep_y + 20),
                    FONT_SMALL, 0.52, C_WARN, 1, cv2.LINE_AA)

    # ── Match status text (top center) ───────────────────────────────────────

    @staticmethod
    def _draw_status_text(canvas: np.ndarray, snap: _StateSnapshot) -> None:
        target = snap.target_border_color
        if target == BorderColor.GREEN:
            text, color = "GOOD", C_OK
        elif target == BorderColor.RED:
            text, color = "NO MATCH", C_ERR
        else:
            return  # yellow = standby, no text

        h, w = canvas.shape[:2]
        scale, thickness = 2.2, 3
        (tw, th), _ = cv2.getTextSize(text, FONT, scale, thickness)
        x = (w - tw) // 2
        y = 90
        pad = 20
        y1 = max(0, y - th - pad)
        y2 = min(h, y + pad)
        x1 = max(0, x - pad)
        x2 = min(w, x + tw + pad)
        if y2 > y1 and x2 > x1:
            roi = canvas[y1:y2, x1:x2]
            dark = np.zeros_like(roi)
            cv2.addWeighted(dark, 0.55, roi, 0.45, 0, roi)
        cv2.putText(canvas, text, (x, y), FONT, scale, color, thickness, cv2.LINE_AA)

    # ── Persistent center prompt ──────────────────────────────────────────────

    @staticmethod
    def _draw_center_prompt(canvas: np.ndarray, text: str) -> None:
        h, w = canvas.shape[:2]
        scale, thickness = 1.6, 2
        (tw, th), _ = cv2.getTextSize(text, FONT, scale, thickness)
        x = (w - tw) // 2
        y = (h + th) // 2 + 40
        pad = 24
        y1 = max(0, y - th - pad)
        y2 = min(h, y + pad)
        x1 = max(0, x - pad)
        x2 = min(w, x + tw + pad)
        roi = canvas[y1:y2, x1:x2]
        dark = np.zeros_like(roi)
        cv2.addWeighted(dark, 0.60, roi, 0.40, 0, roi)
        cv2.putText(canvas, text, (x, y), FONT, scale, C_WARN, thickness, cv2.LINE_AA)

    # ── QR flash banner (bottom center, 3s) ──────────────────────────────────

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
        cv2.putText(canvas, text, (x, y), FONT, scale, C_WARN, thickness, cv2.LINE_AA)

    # ── Border (color interpolated) ───────────────────────────────────────────

    @staticmethod
    def _draw_border(canvas: np.ndarray, snap: _StateSnapshot) -> None:
        h, w = canvas.shape[:2]
        color = get_border_bgr(snap)
        t = BORDER_THICKNESS
        cv2.rectangle(canvas, (t // 2, t // 2), (w - t // 2, h - t // 2),
                      color, t, cv2.LINE_AA)

    # ── Common helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _panel_bg(
        canvas: np.ndarray, x: int, y: int, w: int, h: int
    ) -> None:
        ch, cw = canvas.shape[:2]
        x, y = max(0, x), max(0, y)
        x2, y2 = min(cw, x + w), min(ch, y + h)
        if x2 <= x or y2 <= y:
            return
        roi = canvas[y:y2, x:x2]
        dark = np.full_like(roi, C_PANEL)
        cv2.addWeighted(dark, PANEL_ALPHA, roi, 1 - PANEL_ALPHA, 0, roi)
        cv2.rectangle(canvas, (x, y), (x2, y2), C_PANEL_BORDER, 1)

    @staticmethod
    def _header(canvas: np.ndarray, text: str, x: int, y: int) -> None:
        cv2.putText(canvas, text, (x, y), FONT_SMALL, 0.55,
                    C_HEADER, 1, cv2.LINE_AA)


# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────

def _temp_color(temp: float | None) -> tuple[int, int, int]:
    if temp is None:
        return C_GRAY
    if temp >= 90:
        return C_ERR
    if temp >= 80:
        return C_WARN
    return C_OK

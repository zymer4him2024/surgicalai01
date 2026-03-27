"""Gas HUD renderer — info-only panel (no camera feed).

Layout (1920x1080):
  ┌──────────────────────────────────────────────────────────────┐
  │ [AI STATUS]  (top-left)                                      │
  │  . Inference  Ready                                          │
  │  . FPS        12.0                                           │
  │  . NPU Temp   72 C                                           │
  │  . Thermal    Normal                                         │
  │                                                              │
  │                    GAS INVENTORY                              │
  │                                                              │
  │                   Total Count                                 │
  │                      12                                       │
  │                                                              │
  │                  Status: NORMAL                               │
  │                                                              │
  │  Location: Warehouse A                                        │
  │  Operator: John D.                                            │
  │  2026-03-20 14:32:05                                          │
  │                             [border 10px -- green/red]        │
  └──────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import cv2
import numpy as np

from src.gas_display.buffer import GasStateSnapshot
from src.gas_display.schemas import GasBorderColor

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
C_BG = (18, 18, 18)

C_OK = (80, 220, 80)
C_WARN = (0, 200, 240)
C_ERR = (50, 50, 220)

_BGR = {
    GasBorderColor.GREEN: (80, 220, 80),
    GasBorderColor.RED: (30, 30, 230),
}

BORDER_THICKNESS = 10
PANEL_ALPHA = 0.70


# ─────────────────────────────────────────────────────────────────────────────
# HUD Renderer
# ─────────────────────────────────────────────────────────────────────────────

class GasHUDRenderer:

    def render(self, canvas: np.ndarray, snap: GasStateSnapshot) -> None:
        h, w = canvas.shape[:2]
        canvas[:] = C_BG

        self._draw_ai_panel(canvas, snap, x=16, y=16)
        self._draw_title(canvas, w)
        self._draw_count(canvas, snap, w, h)
        self._draw_status(canvas, snap, w, h)
        self._draw_footer(canvas, snap, w, h)
        self._draw_border(canvas, snap)

    # ── AI STATUS panel (top-left) ──────────────────────────────────────────

    def _draw_ai_panel(
        self, canvas: np.ndarray, snap: GasStateSnapshot, x: int, y: int
    ) -> None:
        pw, ph = 250, 152
        _panel_bg(canvas, x, y, pw, ph)
        _header(canvas, "[+] AI STATUS", x + 10, y + 22)

        rows = [
            ("Inference",
             "Ready" if snap.inference_ready else "Offline",
             C_OK if snap.inference_ready else C_ERR),
            ("FPS",
             f"{snap.ai_fps:.1f}",
             C_OK if snap.ai_fps > 5 else C_WARN),
            ("NPU Temp",
             f"{snap.npu_temp_celsius:.1f} C" if snap.npu_temp_celsius else "N/A",
             _temp_color(snap.npu_temp_celsius)),
            ("Thermal",
             snap.thermal_status.capitalize(),
             C_OK if snap.thermal_status == "normal" else
             C_WARN if snap.thermal_status == "warning" else C_ERR),
        ]
        for i, (label, value, color) in enumerate(rows):
            ry = y + 48 + i * 26
            cv2.circle(canvas, (x + 16, ry - 5), 5, color, -1, cv2.LINE_AA)
            cv2.putText(canvas, label, (x + 28, ry),
                        FONT_SMALL, 0.48, C_GRAY, 1, cv2.LINE_AA)
            cv2.putText(canvas, value, (x + 140, ry),
                        FONT_SMALL, 0.50, C_WHITE, 1, cv2.LINE_AA)

    # ── Title ───────────────────────────────────────────────────────────────

    @staticmethod
    def _draw_title(canvas: np.ndarray, w: int) -> None:
        text = "GAS INVENTORY"
        scale, thickness = 1.8, 2
        (tw, _), _ = cv2.getTextSize(text, FONT, scale, thickness)
        cv2.putText(canvas, text, ((w - tw) // 2, 120),
                    FONT, scale, C_HEADER, thickness, cv2.LINE_AA)

    # ── Large count (center) ────────────────────────────────────────────────

    @staticmethod
    def _draw_count(
        canvas: np.ndarray, snap: GasStateSnapshot, w: int, h: int
    ) -> None:
        label = "Total Count"
        scale_l, thick_l = 1.2, 2
        (lw, _), _ = cv2.getTextSize(label, FONT, scale_l, thick_l)
        cv2.putText(canvas, label, ((w - lw) // 2, h // 2 - 80),
                    FONT, scale_l, C_GRAY, thick_l, cv2.LINE_AA)

        count_text = str(snap.total_count)
        scale_n, thick_n = 5.0, 6
        (nw, _), _ = cv2.getTextSize(count_text, FONT, scale_n, thick_n)
        color = C_ERR if snap.state == "LOW_STOCK" else C_OK
        cv2.putText(canvas, count_text, ((w - nw) // 2, h // 2 + 40),
                    FONT, scale_n, color, thick_n, cv2.LINE_AA)

    # ── Status indicator ────────────────────────────────────────────────────

    @staticmethod
    def _draw_status(
        canvas: np.ndarray, snap: GasStateSnapshot, w: int, h: int
    ) -> None:
        if snap.state == "LOW_STOCK":
            text, color = "LOW STOCK", C_ERR
        else:
            text, color = "NORMAL", C_OK

        status_str = f"Status: {text}"
        scale, thickness = 1.4, 2
        (tw, _), _ = cv2.getTextSize(status_str, FONT, scale, thickness)
        cv2.putText(canvas, status_str, ((w - tw) // 2, h // 2 + 110),
                    FONT, scale, color, thickness, cv2.LINE_AA)

    # ── Footer (location, operator, timestamp) ──────────────────────────────

    @staticmethod
    def _draw_footer(
        canvas: np.ndarray, snap: GasStateSnapshot, w: int, h: int
    ) -> None:
        cy = h - 120
        cx = w // 2

        if snap.location:
            loc = f"Location: {snap.location}"
            (lw, _), _ = cv2.getTextSize(loc, FONT_SMALL, 0.7, 1)
            cv2.putText(canvas, loc, (cx - lw // 2, cy),
                        FONT_SMALL, 0.7, C_WHITE, 1, cv2.LINE_AA)

        if snap.operator_id:
            op = f"Operator: {snap.operator_id}"
            (ow, _), _ = cv2.getTextSize(op, FONT_SMALL, 0.7, 1)
            cv2.putText(canvas, op, (cx - ow // 2, cy + 36),
                        FONT_SMALL, 0.7, C_WHITE, 1, cv2.LINE_AA)

        if snap.timestamp:
            (tw, _), _ = cv2.getTextSize(snap.timestamp, FONT_SMALL, 0.6, 1)
            cv2.putText(canvas, snap.timestamp, (cx - tw // 2, cy + 70),
                        FONT_SMALL, 0.6, C_GRAY, 1, cv2.LINE_AA)

    # ── Border ──────────────────────────────────────────────────────────────

    @staticmethod
    def _draw_border(canvas: np.ndarray, snap: GasStateSnapshot) -> None:
        h, w = canvas.shape[:2]
        color = _BGR.get(snap.border_color, (80, 220, 80))
        t = BORDER_THICKNESS
        cv2.rectangle(canvas, (t // 2, t // 2), (w - t // 2, h - t // 2),
                      color, t, cv2.LINE_AA)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _panel_bg(canvas: np.ndarray, x: int, y: int, w: int, h: int) -> None:
    ch, cw = canvas.shape[:2]
    x, y = max(0, x), max(0, y)
    x2, y2 = min(cw, x + w), min(ch, y + h)
    if x2 <= x or y2 <= y:
        return
    roi = canvas[y:y2, x:x2]
    dark = np.full_like(roi, C_PANEL)
    cv2.addWeighted(dark, PANEL_ALPHA, roi, 1 - PANEL_ALPHA, 0, roi)
    cv2.rectangle(canvas, (x, y), (x2, y2), C_PANEL_BORDER, 1)


def _header(canvas: np.ndarray, text: str, x: int, y: int) -> None:
    cv2.putText(canvas, text, (x, y), FONT_SMALL, 0.55,
                C_HEADER, 1, cv2.LINE_AA)


def _temp_color(temp: float | None) -> tuple[int, int, int]:
    if temp is None:
        return C_GRAY
    if temp >= 90:
        return C_ERR
    if temp >= 80:
        return C_WARN
    return C_OK

"""agent.py — Fixed sequential conversion pipeline (no external AI dependency).

Steps:
  1. inspect_model  — detect format, extract class names + input size
  2. export_onnx    — .pt only: convert to ONNX via ultralytics
  3. hailo_parse    — ONNX → .har
  4. hailo_optimize — .har → optimized .har (INT8 quantization)
  5. hailo_compile  — optimized .har → .hef
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from . import tools

logger = logging.getLogger(__name__)


def run_conversion(
    file_path: str,
    hw_arch: str,
    work_dir: str,
    calib_path: str = "",
    on_log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run the full conversion pipeline sequentially.

    Returns:
        {"ok": True, "hef_path": str, "class_names": list, "input_resolution": int}
        or {"ok": False, "error": str}
    """

    def log(msg: str) -> None:
        logger.info(msg)
        if on_log:
            on_log(msg)

    # ── Step 1: Inspect ───────────────────────────────────────────────────────
    log("inspect_model: reading model metadata")
    info = tools.dispatch("inspect_model", {"file_path": file_path})
    if not info.get("ok"):
        return {"ok": False, "error": f"inspect_model failed: {info.get('error', '')}"}

    fmt = info.get("format", "")
    class_names = info.get("class_names", [])
    input_resolution = info.get("input_size") or 640
    log(f"inspect_model: format={fmt}, classes={len(class_names)}, imgsz={input_resolution}")

    # ── Step 2: Export ONNX (only for .pt) ───────────────────────────────────
    if fmt == "pt":
        log("export_onnx: converting PyTorch model to ONNX")
        result = tools.dispatch("export_onnx", {
            "pt_path": file_path,
            "work_dir": work_dir,
            "imgsz": input_resolution,
        })
        if not result.get("ok"):
            return {"ok": False, "error": f"export_onnx failed: {result.get('error', '')}"}
        onnx_path = result["onnx_path"]
        log(f"export_onnx: {onnx_path}")
    elif fmt == "onnx":
        onnx_path = file_path
    else:
        onnx_path = None  # .har input — skip straight to optimize

    # ── Step 3: Hailo Parse ───────────────────────────────────────────────────
    if onnx_path:
        import re, os
        net_name = re.sub(r"[^a-zA-Z0-9_]", "_", os.path.splitext(os.path.basename(file_path))[0])[:32]
        log(f"hailo_parse: parsing ONNX → HAR (net_name={net_name}, hw_arch={hw_arch})")
        result = tools.dispatch("hailo_parse", {
            "onnx_path": onnx_path,
            "net_name": net_name,
            "hw_arch": hw_arch,
            "work_dir": work_dir,
        })
        if not result.get("ok"):
            return {"ok": False, "error": f"hailo_parse failed: {result.get('error', '')}"}
        har_path = result["har_path"]
        log(f"hailo_parse: {har_path}")
    else:
        har_path = file_path  # input was already a .har

    # ── Step 4: Hailo Optimize ────────────────────────────────────────────────
    log("hailo_optimize: quantizing to INT8")
    opt_args: dict[str, Any] = {
        "har_path": har_path,
        "hw_arch": hw_arch,
        "work_dir": work_dir,
    }
    if calib_path:
        opt_args["calib_path"] = calib_path
    result = tools.dispatch("hailo_optimize", opt_args)
    if not result.get("ok"):
        return {"ok": False, "error": f"hailo_optimize failed: {result.get('error', '')}"}
    opt_har_path = result["optimized_har_path"]
    log(f"hailo_optimize: {opt_har_path}")

    # ── Step 5: Hailo Compile ─────────────────────────────────────────────────
    log("hailo_compile: compiling to HEF")
    result = tools.dispatch("hailo_compile", {
        "har_path": opt_har_path,
        "hw_arch": hw_arch,
        "work_dir": work_dir,
    })
    if not result.get("ok"):
        return {"ok": False, "error": f"hailo_compile failed: {result.get('error', '')}"}
    hef_path = result["hef_path"]
    log(f"hailo_compile: {hef_path}")

    return {
        "ok": True,
        "hef_path": hef_path,
        "class_names": class_names,
        "input_resolution": input_resolution,
    }

"""tools.py — Tool implementations for the conversion pipeline.

When HAILO_CONTAINER_NAME is set, hailo CLI commands are executed via
`docker exec` inside the running Hailo AI SW Suite container.
Files must be placed under HAILO_SHARED_DIR (host path), which maps to
HAILO_CONTAINER_SHARED_DIR inside the SW Suite container.
"""

from __future__ import annotations

import glob
import os
import subprocess
from pathlib import Path
from typing import Any

# ── Docker exec configuration ─────────────────────────────────────────────────
# Set these env vars to run hailo commands inside the SW Suite container.
_HAILO_CTR       = os.environ.get("HAILO_CONTAINER_NAME", "")
_HOST_SHARED     = os.environ.get("HAILO_SHARED_DIR", "").rstrip("/")
_CTR_SHARED      = os.environ.get("HAILO_CONTAINER_SHARED_DIR", "/local/shared_with_docker").rstrip("/")


def _to_ctr_path(host_path: str) -> str:
    """Translate a host path inside HAILO_SHARED_DIR to its container equivalent."""
    if _HOST_SHARED and host_path.startswith(_HOST_SHARED):
        return _CTR_SHARED + host_path[len(_HOST_SHARED):]
    return host_path


def _hailo_run(cmd: list[str], work_dir: str, timeout: int) -> subprocess.CompletedProcess:
    """Run a hailo CLI command — directly or via docker exec."""
    # Ensure any container user (e.g., hailo uid 1000) can write outputs to work_dir
    try:
        if os.path.exists(work_dir):
            os.chmod(work_dir, 0o777)
    except Exception:
        pass

    if _HAILO_CTR:
        translated = [_to_ctr_path(a) for a in cmd]
        ctr_work_dir = _to_ctr_path(work_dir)
        # Use -u $(id -u):$(id -g) to run as current user, or just rely on dir permissions
        full_cmd = ["docker", "exec", "-w", ctr_work_dir, _HAILO_CTR] + translated
        return subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout)
    return subprocess.run(cmd, capture_output=True, text=True, cwd=work_dir, timeout=timeout)


# ── Tool schemas (kept for compatibility) ─────────────────────────────────────

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "inspect_model",
        "description": "Inspect a .pt, .onnx, or .har model file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "export_onnx",
        "description": "Export a PyTorch .pt model to ONNX format.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pt_path": {"type": "string"},
                "work_dir": {"type": "string"},
                "imgsz": {"type": "integer"},
            },
            "required": ["pt_path", "work_dir"],
        },
    },
    {
        "name": "hailo_parse",
        "description": "Parse ONNX → HAR.",
        "input_schema": {
            "type": "object",
            "properties": {
                "onnx_path": {"type": "string"},
                "net_name": {"type": "string"},
                "hw_arch": {"type": "string"},
                "work_dir": {"type": "string"},
            },
            "required": ["onnx_path", "net_name", "hw_arch", "work_dir"],
        },
    },
    {
        "name": "hailo_optimize",
        "description": "Optimize HAR for INT8 quantization.",
        "input_schema": {
            "type": "object",
            "properties": {
                "har_path": {"type": "string"},
                "hw_arch": {"type": "string"},
                "work_dir": {"type": "string"},
                "calib_path": {"type": "string"},
            },
            "required": ["har_path", "hw_arch", "work_dir"],
        },
    },
    {
        "name": "hailo_compile",
        "description": "Compile optimized HAR → HEF.",
        "input_schema": {
            "type": "object",
            "properties": {
                "har_path": {"type": "string"},
                "hw_arch": {"type": "string"},
                "work_dir": {"type": "string"},
            },
            "required": ["har_path", "hw_arch", "work_dir"],
        },
    },
]


# ── Dispatch ──────────────────────────────────────────────────────────────────

def dispatch(name: str, inputs: dict[str, Any]) -> dict[str, Any]:
    handlers = {
        "inspect_model":  _inspect_model,
        "export_onnx":    _export_onnx,
        "hailo_parse":    _hailo_parse,
        "hailo_optimize": _hailo_optimize,
        "hailo_compile":  _hailo_compile,
    }
    fn = handlers.get(name)
    if fn is None:
        return {"ok": False, "error": f"Unknown tool: {name}"}
    try:
        return fn(**inputs)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ── Implementations ───────────────────────────────────────────────────────────

def _inspect_model(file_path: str) -> dict:
    path = Path(file_path)
    info: dict[str, Any] = {
        "ok": path.exists(),
        "file_path": file_path,
        "format": path.suffix.lstrip(".").lower(),
        "size_mb": round(path.stat().st_size / 1_048_576, 1) if path.exists() else 0,
        "class_names": [],
        "input_size": None,
    }
    if not path.exists():
        info["error"] = f"File not found: {file_path}"
        return info

    if path.suffix.lower() == ".pt":
        try:
            from ultralytics import YOLO
            model = YOLO(file_path)
            if hasattr(model, "names"):
                info["class_names"] = list(model.names.values())
                info["class_count"] = len(info["class_names"])
            if hasattr(model, "overrides"):
                info["input_size"] = model.overrides.get("imgsz", 640)
            info["task"] = getattr(model, "task", "detect")
        except Exception as exc:
            info["inspect_error"] = str(exc)

    elif path.suffix.lower() == ".onnx":
        try:
            import onnx
            m = onnx.load(file_path)
            inp = m.graph.input[0].type.tensor_type.shape
            dims = [d.dim_value for d in inp.dim] if inp else []
            if len(dims) >= 3:
                info["input_size"] = dims[2]
            info["opset"] = m.opset_import[0].version if m.opset_import else None
        except Exception as exc:
            info["inspect_error"] = str(exc)

    return info


def _export_onnx(pt_path: str, work_dir: str, imgsz: int = 640) -> dict:
    try:
        from ultralytics import YOLO
        import shutil
        model = YOLO(pt_path)
        result = model.export(format="onnx", imgsz=imgsz, opset=11, dynamic=False, simplify=True)
        onnx_path = str(result)
        if not onnx_path.startswith(work_dir):
            dest = os.path.join(work_dir, Path(onnx_path).name)
            shutil.move(onnx_path, dest)
            onnx_path = dest
        return {"ok": True, "onnx_path": onnx_path}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _hailo_parse(onnx_path: str, net_name: str, hw_arch: str, work_dir: str) -> dict:
    cmd = ["hailo", "parser", "onnx", onnx_path, "--net-name", net_name, "--hw-arch", hw_arch, "-y"]
    # Auto-detect YOLOv8 and use correct end nodes (avoids unsupported sigmoid/concat output layers)
    try:
        import onnx as _onnx
        _node_names = {n.name for n in _onnx.load(onnx_path).graph.node if n.op_type == "Conv"}
        _yolov8_ends = [
            "/model.22/cv2.0/cv2.0.2/Conv", "/model.22/cv2.1/cv2.1.2/Conv",
            "/model.22/cv2.2/cv2.2.2/Conv", "/model.22/cv3.0/cv3.0.2/Conv",
            "/model.22/cv3.1/cv3.1.2/Conv", "/model.22/cv3.2/cv3.2.2/Conv",
        ]
        if all(n in _node_names for n in _yolov8_ends):
            cmd += ["--end-node-names"] + _yolov8_ends
    except Exception:
        pass
    result = _hailo_run(cmd, work_dir, timeout=600)
    log = (result.stdout + result.stderr)[-8000:]
    if result.returncode != 0:
        return {"ok": False, "error": log}
    har_files = [f for f in glob.glob(os.path.join(work_dir, "*.har")) if "_optimized" not in f]
    if not har_files:
        return {"ok": False, "error": "No .har output found after parse", "log": log}
    return {"ok": True, "har_path": har_files[0], "log": log}


def _hailo_optimize(har_path: str, hw_arch: str, work_dir: str, calib_path: str = "") -> dict:
    cmd = ["hailo", "optimize", har_path, "--hw-arch", hw_arch]
    if calib_path and os.path.isdir(calib_path):
        cmd += ["--calib-set-path", calib_path]
    else:
        cmd += ["--use-random-calib-set"]
        
    alls_path = os.path.join(work_dir, "model.alls")
    try:
        with open(alls_path, "w") as f:
            f.write("model_optimization_flavor(optimization_level=0)\n")
            f.write("resources_param(max_control_core_count=65)\n")
            f.write("performance_param(compiler_optimization_level=max)\n")
        cmd += ["--model-script", alls_path]
    except Exception:
        pass

    result = _hailo_run(cmd, work_dir, timeout=1800)
    log = (result.stdout + result.stderr)[-8000:]
    if result.returncode != 0:
        return {"ok": False, "error": log}
    opt_files = glob.glob(os.path.join(work_dir, "*_optimized.har"))
    if not opt_files:
        return {"ok": False, "error": "No *_optimized.har found after optimize", "log": log}
    return {"ok": True, "optimized_har_path": opt_files[0], "log": log}


def _hailo_compile(har_path: str, hw_arch: str, work_dir: str) -> dict:
    cmd = ["hailo", "compiler", har_path, "--hw-arch", hw_arch]
    result = _hailo_run(cmd, work_dir, timeout=1800)
    log = (result.stdout + result.stderr)[-8000:]
    if result.returncode != 0:
        return {"ok": False, "error": log}
    hef_files = glob.glob(os.path.join(work_dir, "*.hef"))
    if not hef_files:
        return {"ok": False, "error": "No .hef output found after compile", "log": log}
    return {"ok": True, "hef_path": hef_files[0], "log": log}

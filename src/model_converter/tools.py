"""tools.py — Tool implementations for the conversion pipeline.

When HAILO_CONTAINER_NAME is set, hailo CLI commands are executed via
`docker exec` inside the running Hailo AI SW Suite container.
Files must be placed under HAILO_SHARED_DIR (host path), which maps to
HAILO_CONTAINER_SHARED_DIR inside the SW Suite container.
"""

from __future__ import annotations

import glob
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

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
        "description": "Parse ONNX → HAR with architecture-aware end-node detection.",
        "input_schema": {
            "type": "object",
            "properties": {
                "onnx_path": {"type": "string"},
                "net_name": {"type": "string"},
                "hw_arch": {"type": "string"},
                "work_dir": {"type": "string"},
                "arch": {"type": "string", "description": "YOLO architecture: yolov5, yolov8, yolov9, yolov10, yolov11"},
            },
            "required": ["onnx_path", "net_name", "hw_arch", "work_dir"],
        },
    },
    {
        "name": "hailo_optimize",
        "description": "Optimize HAR for INT8 quantization with on-device NMS configuration.",
        "input_schema": {
            "type": "object",
            "properties": {
                "har_path": {"type": "string"},
                "hw_arch": {"type": "string"},
                "work_dir": {"type": "string"},
                "calib_path": {"type": "string"},
                "arch": {"type": "string"},
                "num_classes": {"type": "integer"},
                "input_resolution": {"type": "integer"},
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

def _find_detect_head_index(node_names: set[str]) -> int | None:
    """Find the model.N index of the Detect head by looking for cv2.0/cv3.0 Conv nodes.

    YOLOv8 typically uses model.22, YOLOv11 uses model.23, but this varies by
    model size and config. Scanning model.20..model.30 handles all cases.
    """
    for idx in range(20, 31):
        probe = [
            f"/model.{idx}/cv2.0/cv2.0.2/Conv",
            f"/model.{idx}/cv3.0/cv3.0.2/Conv",
        ]
        if all(n in node_names for n in probe):
            return idx
    return None


def _detect_yolo_arch_from_onnx(onnx_path: str) -> str | None:
    """Detect YOLO architecture version from ONNX graph node naming patterns.

    Returns 'yolov5', 'yolov8', 'yolov9', 'yolov10', 'yolov11', or None.

    Key structural differences:
      - YOLOv8:  Detect head at model.22 (cv2.*/cv3.* end nodes)
      - YOLOv11: Detect head at model.23 (cv2.*/cv3.* end nodes), deeper backbone
      - YOLOv10: Detect head at model.23 with one2one_cv2.*/one2one_cv3.* naming
      - YOLOv9:  Uses cv4/cv5 branches in the head
      - YOLOv5:  Detect head at model.24 (m.0/m.1/m.2 Conv outputs)
    """
    try:
        import onnx as _onnx
        model = _onnx.load(onnx_path)
        node_names = {n.name for n in model.graph.node}

        # YOLOv10: model.23 with one2one_ prefix (check first — also uses model.23)
        yolov10_ends = [
            "/model.23/one2one_cv2.0/one2one_cv2.0.2/Conv",
            "/model.23/one2one_cv3.0/one2one_cv3.0.2/Conv",
        ]
        if all(n in node_names for n in yolov10_ends):
            return "yolov10"

        # YOLOv8 / YOLOv11: scan for the Detect head index
        head_idx = _find_detect_head_index(node_names)
        if head_idx is not None:
            # model.22 → YOLOv8, model.23 → YOLOv11
            # (YOLOv11 has a deeper backbone pushing the Detect head to a higher index)
            if head_idx >= 23:
                return "yolov11"
            return "yolov8"

        # YOLOv9: uses cv4/cv5 branches in the detection head
        yolov9_patterns = ["/cv4.", "/cv5."]
        if any(any(p in n for p in yolov9_patterns) for n in node_names):
            return "yolov9"

        # YOLOv5: uses model.24/m.* (Detect layer at model.24)
        yolov5_ends = ["/model.24/m.0/Conv", "/model.24/m.1/Conv", "/model.24/m.2/Conv"]
        if all(n in node_names for n in yolov5_ends):
            return "yolov5"

        return None
    except Exception as exc:
        logger.warning("Architecture detection failed: %s", exc)
        return None


def _detect_end_nodes(onnx_path: str, arch: str | None) -> list[str]:
    """Return the correct end-node-names for hailo parser based on architecture.

    These end nodes cut off the sigmoid/concat decode head that Hailo cannot compile.
    HailoRT handles the decode natively at inference time.
    """
    try:
        import onnx as _onnx
        model = _onnx.load(onnx_path)
        node_names = {n.name for n in model.graph.node if n.op_type == "Conv"}
    except Exception:
        return []

    # YOLOv8 / YOLOv11: dynamically find the Detect head index
    if arch in ("yolov8", "yolov11"):
        head_idx = _find_detect_head_index(node_names)
        if head_idx is not None:
            ends = [
                f"/model.{head_idx}/cv2.0/cv2.0.2/Conv",
                f"/model.{head_idx}/cv2.1/cv2.1.2/Conv",
                f"/model.{head_idx}/cv2.2/cv2.2.2/Conv",
                f"/model.{head_idx}/cv3.0/cv3.0.2/Conv",
                f"/model.{head_idx}/cv3.1/cv3.1.2/Conv",
                f"/model.{head_idx}/cv3.2/cv3.2.2/Conv",
            ]
            if all(n in node_names for n in ends):
                return ends

    # YOLOv10: one2one head at model.23
    if arch == "yolov10":
        ends = [
            "/model.23/one2one_cv2.0/one2one_cv2.0.2/Conv",
            "/model.23/one2one_cv2.1/one2one_cv2.1.2/Conv",
            "/model.23/one2one_cv2.2/one2one_cv2.2.2/Conv",
            "/model.23/one2one_cv3.0/one2one_cv3.0.2/Conv",
            "/model.23/one2one_cv3.1/one2one_cv3.1.2/Conv",
            "/model.23/one2one_cv3.2/one2one_cv3.2.2/Conv",
        ]
        if all(n in node_names for n in ends):
            return ends

    # YOLOv5: Detect layer at model.24
    if arch == "yolov5":
        ends = ["/model.24/m.0/Conv", "/model.24/m.1/Conv", "/model.24/m.2/Conv"]
        if all(n in node_names for n in ends):
            return ends

    # Fallback: auto-discover detection head Conv nodes
    return _auto_discover_end_nodes(onnx_path)


def _auto_discover_end_nodes(onnx_path: str) -> list[str]:
    """Auto-discover detection head end nodes by tracing Conv -> Sigmoid/Reshape chains.

    This handles unknown architectures by finding Conv nodes whose outputs feed
    directly into Sigmoid or Reshape (the start of the decode head).
    """
    try:
        import onnx as _onnx
        model = _onnx.load(onnx_path)

        # Build output->node map
        output_to_node: dict[str, Any] = {}
        for node in model.graph.node:
            for out in node.output:
                output_to_node[out] = node

        # Find Conv nodes whose outputs are consumed by Sigmoid or Reshape
        decode_ops = {"Sigmoid", "Reshape", "Transpose"}
        input_to_consumers: dict[str, list[Any]] = {}
        for node in model.graph.node:
            for inp in node.input:
                input_to_consumers.setdefault(inp, []).append(node)

        end_convs: list[str] = []
        for node in model.graph.node:
            if node.op_type != "Conv":
                continue
            for out in node.output:
                consumers = input_to_consumers.get(out, [])
                if any(c.op_type in decode_ops for c in consumers):
                    # Check if this is in the detection head (cv2/cv3 pattern or m.* pattern)
                    if any(p in node.name for p in ["cv2", "cv3", "/m.", "one2one"]):
                        end_convs.append(node.name)

        return end_convs
    except Exception as exc:
        logger.warning("Auto-discover end nodes failed: %s", exc)
        return []


def _inspect_model(file_path: str) -> dict:
    path = Path(file_path)
    info: dict[str, Any] = {
        "ok": path.exists(),
        "file_path": file_path,
        "format": path.suffix.lstrip(".").lower(),
        "size_mb": round(path.stat().st_size / 1_048_576, 1) if path.exists() else 0,
        "class_names": [],
        "input_size": None,
        "arch": None,
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
            # Detect architecture from model metadata
            model_yaml = getattr(model, "yaml", None) or {}
            yaml_path = model_yaml if isinstance(model_yaml, str) else ""
            if "yolov5" in yaml_path.lower() or "yolov5" in str(getattr(model, "yaml_file", "")).lower():
                info["arch"] = "yolov5"
            elif "yolov9" in yaml_path.lower():
                info["arch"] = "yolov9"
            elif "yolov10" in yaml_path.lower():
                info["arch"] = "yolov10"
            elif "yolo11" in yaml_path.lower() or "yolov11" in yaml_path.lower():
                info["arch"] = "yolov11"
            else:
                info["arch"] = "yolov8"  # default for ultralytics models
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
            info["arch"] = _detect_yolo_arch_from_onnx(file_path)
        except Exception as exc:
            info["inspect_error"] = str(exc)

    return info


def _validate_onnx(onnx_path: str) -> dict[str, Any]:
    """Validate an ONNX file: check model integrity, input shape, and flag unsupported ops."""
    try:
        import onnx
        model = onnx.load(onnx_path)
        onnx.checker.check_model(model)
    except Exception as exc:
        return {"ok": False, "error": f"ONNX validation failed: {exc}"}

    # Check input shape is static (no dynamic dims)
    try:
        inp = model.graph.input[0].type.tensor_type.shape
        dims = [d.dim_value for d in inp.dim] if inp else []
        if any(d == 0 for d in dims):
            return {
                "ok": False,
                "error": f"ONNX has dynamic input dims {dims}. Re-export with dynamic=False.",
            }
        if len(dims) != 4:
            return {
                "ok": False,
                "error": f"Expected 4D input [N,C,H,W], got {len(dims)}D: {dims}",
            }
        h, w = dims[2], dims[3]
        if h != w:
            logger.warning("Non-square input %dx%d — Hailo may require square input", h, w)
    except Exception as exc:
        return {"ok": False, "error": f"Cannot read ONNX input shape: {exc}"}

    return {"ok": True, "input_shape": dims}


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

        # Validate the exported ONNX
        val = _validate_onnx(onnx_path)
        if not val["ok"]:
            return val

        return {"ok": True, "onnx_path": onnx_path, "input_shape": val.get("input_shape")}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _hailo_parse(
    onnx_path: str,
    net_name: str,
    hw_arch: str,
    work_dir: str,
    arch: str | None = None,
) -> dict:
    # Validate ONNX if not already validated (e.g., user-provided .onnx input)
    val = _validate_onnx(onnx_path)
    if not val["ok"]:
        return val

    cmd = ["hailo", "parser", "onnx", onnx_path, "--net-name", net_name, "--hw-arch", hw_arch, "-y"]

    # If arch not provided, try to detect from the ONNX graph
    if not arch:
        arch = _detect_yolo_arch_from_onnx(onnx_path)

    # Architecture-aware end node detection
    end_nodes = _detect_end_nodes(onnx_path, arch)
    if end_nodes:
        cmd += ["--end-node-names"] + end_nodes
        logger.info("Using end nodes for %s: %s", arch or "auto-detected", end_nodes)
    else:
        logger.warning(
            "No end nodes detected for arch=%s — parser will use ONNX output nodes. "
            "This may include unsupported sigmoid/concat layers.",
            arch,
        )

    result = _hailo_run(cmd, work_dir, timeout=600)
    log = (result.stdout + result.stderr)[-8000:]

    if result.returncode != 0:
        # Provide actionable error messages for common failures
        if "Unable to find end node name" in log:
            return {
                "ok": False,
                "error": (
                    f"End node names not found in ONNX for arch={arch}. "
                    "The model's detection head structure does not match expected patterns. "
                    "Inspect the ONNX with: python -c \"import onnx; m=onnx.load('model.onnx'); "
                    "[print(n.name) for n in m.graph.node if n.op_type=='Conv']\""
                ),
                "log": log,
            }
        if "16x4 is not supported" in log or "not supported in activation" in log:
            return {
                "ok": False,
                "error": (
                    "Hailo parser failed due to unsupported operation in the decode head. "
                    "This typically means the ONNX includes sigmoid/concat layers that must be "
                    "cut off with --end-node-names. Architecture detection may have failed."
                ),
                "log": log,
            }
        return {"ok": False, "error": log}

    har_files = [f for f in glob.glob(os.path.join(work_dir, "*.har")) if "_optimized" not in f]
    if not har_files:
        return {"ok": False, "error": "No .har output found after parse", "log": log}
    return {"ok": True, "har_path": har_files[0], "arch": arch, "log": log}


def _build_model_script(
    work_dir: str,
    arch: str | None,
    num_classes: int,
    input_resolution: int,
) -> str:
    """Generate model.alls with NMS config appropriate for the architecture.

    NMS on-device means the HEF output is already filtered detections,
    matching what the inference runner expects (ragged NMS or regular NMS tensor).
    Without this, the HEF outputs raw feature maps requiring complex post-processing.
    """
    alls_path = os.path.join(work_dir, "model.alls")
    lines = [
        "model_optimization_flavor(optimization_level=0)",
        "resources_param(max_control_core_count=65)",
        "performance_param(compiler_optimization_level=max)",
    ]

    # Add NMS post-processing config for YOLO architectures
    # This tells the Hailo compiler to add NMS as a post-processing layer on-device
    if arch in ("yolov8", "yolov11", "yolov10"):
        # YOLOv8/v11/v10: anchor-free, uses nms_postprocess with regression_length=16
        # (DFL distribution over 16 bins for bbox regression)
        lines.append("")
        lines.append(f"# NMS configuration for {arch}")
        lines.append("nms_postprocess(")
        lines.append(f"    meta_arch=yolov8,")
        lines.append(f"    engine=cpu,")
        lines.append(f"    nms_iou_thresh=0.45,")
        lines.append(f"    nms_score_thresh=0.01,")
        lines.append(f"    image_dims=({input_resolution}, {input_resolution}),")
        lines.append(f"    max_proposals_per_class=100,")
        lines.append(f"    max_proposals_total=300,")
        lines.append(f"    classes={num_classes},")
        lines.append(f"    regression_length=16,")
        lines.append(f"    background_removal=false,")
        lines.append(")")
    elif arch == "yolov5":
        # YOLOv5: anchor-based, different meta_arch
        lines.append("")
        lines.append(f"# NMS configuration for {arch}")
        lines.append("nms_postprocess(")
        lines.append(f"    meta_arch=yolov5,")
        lines.append(f"    engine=cpu,")
        lines.append(f"    nms_iou_thresh=0.45,")
        lines.append(f"    nms_score_thresh=0.01,")
        lines.append(f"    image_dims=({input_resolution}, {input_resolution}),")
        lines.append(f"    max_proposals_per_class=100,")
        lines.append(f"    max_proposals_total=300,")
        lines.append(f"    classes={num_classes},")
        lines.append(f"    anchors_num=3,")
        lines.append(")")
    elif arch == "yolov9":
        # YOLOv9: anchor-free like v8 but different head structure
        # Use yolov8 meta_arch (compatible NMS structure)
        lines.append("")
        lines.append(f"# NMS configuration for {arch} (yolov8-compatible)")
        lines.append("nms_postprocess(")
        lines.append(f"    meta_arch=yolov8,")
        lines.append(f"    engine=cpu,")
        lines.append(f"    nms_iou_thresh=0.45,")
        lines.append(f"    nms_score_thresh=0.01,")
        lines.append(f"    image_dims=({input_resolution}, {input_resolution}),")
        lines.append(f"    max_proposals_per_class=100,")
        lines.append(f"    max_proposals_total=300,")
        lines.append(f"    classes={num_classes},")
        lines.append(f"    regression_length=16,")
        lines.append(f"    background_removal=false,")
        lines.append(")")
    else:
        logger.warning(
            "Unknown arch=%s — skipping NMS config in model.alls. "
            "HEF will output raw feature maps requiring manual post-processing.",
            arch,
        )

    with open(alls_path, "w") as f:
        f.write("\n".join(lines) + "\n")

    return alls_path


def _hailo_optimize(
    har_path: str,
    hw_arch: str,
    work_dir: str,
    calib_path: str = "",
    arch: str | None = None,
    num_classes: int = 80,
    input_resolution: int = 640,
) -> dict:
    cmd = ["hailo", "optimize", har_path, "--hw-arch", hw_arch]
    if calib_path and os.path.isdir(calib_path):
        cmd += ["--calib-set-path", calib_path]
    else:
        cmd += ["--use-random-calib-set"]

    # Build model.alls with NMS config
    has_nms = arch in ("yolov5", "yolov8", "yolov9", "yolov10", "yolov11")
    try:
        alls_path = _build_model_script(work_dir, arch, num_classes, input_resolution)
        cmd += ["--model-script", alls_path]
    except Exception as exc:
        logger.warning("Failed to write model.alls: %s", exc)
        has_nms = False

    result = _hailo_run(cmd, work_dir, timeout=1800)
    log = (result.stdout + result.stderr)[-8000:]
    if result.returncode != 0:
        # If model.alls had NMS config, retry without it (graceful fallback).
        # The NMS error may appear as model_script parse failure, postprocess
        # error, or any other crash during script loading — so we retry
        # unconditionally whenever NMS was included.
        if has_nms:
            logger.warning(
                "Optimize failed with NMS config (arch=%s) — retrying without model script. "
                "HEF will output raw feature maps requiring host-side post-processing.",
                arch,
            )
            # Remove --model-script from command entirely so Hailo uses defaults.
            # DFC 3.33.0 may not support model_optimization_flavor/resources_param.
            retry_cmd = [arg for i, arg in enumerate(cmd)
                         if arg != "--model-script" and
                         (i == 0 or cmd[i - 1] != "--model-script")]
            result = _hailo_run(retry_cmd, work_dir, timeout=1800)
            log = (result.stdout + result.stderr)[-8000:]
            if result.returncode != 0:
                return {"ok": False, "error": log}
        else:
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

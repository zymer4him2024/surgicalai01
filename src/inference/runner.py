"""
runner.py — Hailo-8 inference worker (runs in a separate process)

Design intent:
  - Isolate NPU driver memory from the FastAPI main process
  - A crash in the inference process does not affect the FastAPI server
  - Full memory/resource cleanup is possible via process restart

Communication:
  - request_queue  : send (request_id, image_bytes)
  - response_queue : receive (request_id, result_dict | error_str)
"""

from __future__ import annotations

import io
import json
import logging
import multiprocessing as mp
import os
import time
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# SurgeoNet 14-class labels (surgeonet_416.hef — trained on surgical instruments)
# class 0 = Background (filtered out)
# ─────────────────────────────────────────────────────────────────────────────
_SURGEONET_CLASS_NAMES: list[str] = [
    "Background",
    "Overholt Clamp",
    "Metz. Scissor",
    "Sur. Scissor",
    "Needle Holder",
    "Sur. Forceps",
    "Atr. Forceps",
    "Scalpel",
    "Retractor",
    "Hook",
    "Lig. Clamp",
    "Peri. Clamp",
    "Bowl",
    "Tong",
]


def _load_class_names() -> list[str]:
    """Load class names from CLASS_NAMES_JSON env var or labels.json file, falling back to SurgeoNet defaults."""
    env_json = os.getenv("CLASS_NAMES_JSON", "")
    if env_json:
        try:
            names = json.loads(env_json)
            if isinstance(names, list) and len(names) > 0:
                return names
        except json.JSONDecodeError:
            pass
    labels_path = os.getenv("LABELS_PATH", "/app/models/labels.json")
    if os.path.exists(labels_path):
        try:
            with open(labels_path) as f:
                data = json.load(f)
            if isinstance(data, list) and len(data) > 0:
                return data
            if isinstance(data, dict) and "names" in data:
                names = data["names"]
                if isinstance(names, dict):
                    return [names[str(i)] for i in range(len(names))]
                return list(names)
        except (json.JSONDecodeError, OSError, KeyError):
            pass
    return _SURGEONET_CLASS_NAMES


DEFAULT_CLASS_NAMES: list[str] = _load_class_names()

# Model input resolution — configurable via INPUT_SIZE env var (default: SurgeoNet 416x416)
INPUT_SIZE = int(os.getenv("INPUT_SIZE", "416"))
SKIP_BACKGROUND = os.getenv("SKIP_BACKGROUND", "true").lower() == "true"

# NMS parameters
CONF_THRESHOLD = float(os.getenv("CONF_THRESHOLD", "0.30"))
IOU_THRESHOLD = float(os.getenv("IOU_THRESHOLD", "0.50"))

# Whether to normalize input to [0, 1] before passing to SDK.
# With UINT8 input mode, normalization is NOT needed — SDK uses HEF's compiled scale/zero_point.
NORMALIZE_INPUT = os.getenv("NORMALIZE_INPUT", "false").lower() == "true"

# Color channel order: "bgr" or "rgb". YOLOv8 trained with OpenCV uses BGR.
COLOR_ORDER = os.getenv("COLOR_ORDER", "bgr").lower()

# Flag to log output structure once on first inference
_hailo_output_logged = False
# Counter for periodic diagnostic logging (every 100th inference)
_inference_count = 0


# ─────────────────────────────────────────────────────────────────────────────
# POC: COCO → surgical instrument mapping filter
# ─────────────────────────────────────────────────────────────────────────────

def _apply_surgical_mapping(detections: list[dict]) -> list[dict]:
    """Pass-through — all COCO detections forwarded until SurgeoNet HEF is available."""
    return detections


# ─────────────────────────────────────────────────────────────────────────────
# Inference worker function (runs in a separate process)
# ─────────────────────────────────────────────────────────────────────────────

def inference_worker(
    hef_path: str,
    request_queue: mp.Queue,  # type: ignore[type-arg]
    response_queue: mp.Queue,  # type: ignore[type-arg]
    stop_event: mp.Event,  # type: ignore[type-arg]
) -> None:
    """
    NPU inference loop.
    On start, loads the HEF, polls request_queue, and sends results to response_queue.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [InferenceWorker] %(levelname)s %(message)s",
    )
    log = logging.getLogger("inference_worker")

    # Select Hailo SDK or simulation mode
    sdk_available = _try_load_hailo(hef_path, log)

    log.info(
        "Inference worker ready (mode=%s, hef=%s)",
        "hailo" if sdk_available else "simulation",
        hef_path,
    )

    while not stop_event.is_set():
        try:
            # Receive request from queue (timeout ensures stop_event is polled)
            item = request_queue.get(timeout=1.0)
        except Exception:
            continue

        request_id: str = item["request_id"]
        image_bytes: bytes = item["image_bytes"]

        try:
            start = time.perf_counter()
            detections = (
                _run_hailo_inference(image_bytes, log)
                if sdk_available
                else _run_simulation_inference(image_bytes)
            )
            # POC: apply COCO → surgical instrument mapping (filter unmapped classes)
            detections = _apply_surgical_mapping(detections)
            elapsed_ms = (time.perf_counter() - start) * 1000

            response_queue.put({
                "request_id": request_id,
                "detections": detections,
                "inference_time_ms": round(elapsed_ms, 2),
                "error": None,
            })
        except Exception as exc:
            log.exception("Inference error for request %s", request_id)
            response_queue.put({
                "request_id": request_id,
                "detections": [],
                "inference_time_ms": 0.0,
                "error": str(exc),
            })

    log.info("Inference worker shutting down — releasing NPU resources")
    _cleanup_hailo()


# ─────────────────────────────────────────────────────────────────────────────
# Hailo SDK wrapper (optional dependency)
# ─────────────────────────────────────────────────────────────────────────────

_hailo_infer_model: Any = None          # SDK InferModel object (global, per-process)


def _try_load_hailo(hef_path: str, log: logging.Logger) -> bool:
    """Attempt to load HEF via hailort SDK. Returns False on failure (simulation mode)."""
    global _hailo_infer_model
    if not os.path.exists(hef_path):
        log.warning("HEF not found at %s — using simulation mode", hef_path)
        return False
    try:
        from hailo_platform import (  # type: ignore[import]
            HEF,
            VDevice,
            HailoStreamInterface,
            InferVStreams,
            ConfigureParams,
            InputVStreamParams,
            OutputVStreamParams,
            FormatType,
        )

        hef = HEF(hef_path)
        target = VDevice()
        configure_params = ConfigureParams.create_from_hef(
            hef, interface=HailoStreamInterface.PCIe
        )
        network_groups = target.configure(hef, configure_params)
        network_group = network_groups[0]
        network_group_params = network_group.create_params()

        # Input: UINT8 — pass raw [0,255] pixels; SDK applies HEF's compiled quantization
        # Output: FLOAT32 — SDK dequantizes so existing decode logic works unchanged
        input_vstreams_params = InputVStreamParams.make_from_network_group(
            network_group, quantized=True, format_type=FormatType.UINT8
        )
        output_vstreams_params = OutputVStreamParams.make_from_network_group(
            network_group, quantized=False, format_type=FormatType.FLOAT32
        )

        # Activate network group and pipeline ONCE — reuse across all inferences
        activated_context = network_group.activate(network_group_params)
        activated_context.__enter__()
        pipeline = InferVStreams(
            network_group, input_vstreams_params, output_vstreams_params
        )
        pipeline.__enter__()

        input_name = hef.get_input_vstream_infos()[0].name

        _hailo_infer_model = {
            "target": target,
            "network_group": network_group,
            "network_group_params": network_group_params,
            "input_params": input_vstreams_params,
            "output_params": output_vstreams_params,
            "hef": hef,
            "pipeline": pipeline,
            "activated_context": activated_context,
            "input_name": input_name,
        }
        log.info("Hailo HEF loaded + pipeline activated: %s", hef_path)
        # Log input/output vstream info for debugging input format expectations
        for info in hef.get_input_vstream_infos():
            log.info("HEF INPUT vstream: name=%s shape=%s format=%s",
                     info.name, info.shape, info.format)
            try:
                log.info("HEF INPUT quant: type=%s order=%s",
                         info.format.type, info.format.order)
            except AttributeError:
                pass
        for info in hef.get_output_vstream_infos():
            log.info("HEF OUTPUT vstream: name=%s shape=%s format=%s",
                     info.name, info.shape, info.format)
            try:
                log.info("HEF OUTPUT quant: type=%s order=%s",
                         info.format.type, info.format.order)
            except AttributeError:
                pass
        return True
    except Exception as exc:
        log.warning("Hailo SDK unavailable (%s) — simulation mode", exc)
        return False


def _generate_yolov8_anchors(input_size: int) -> np.ndarray:
    """Generate anchor grid centers for YOLOv8 (strides 8, 16, 32)."""
    strides = [8, 16, 32]
    all_points = []
    for s in strides:
        grid_h, grid_w = input_size // s, input_size // s
        ys, xs = np.meshgrid(
            np.arange(grid_h, dtype=np.float32),
            np.arange(grid_w, dtype=np.float32),
            indexing="ij",
        )
        points = np.stack([xs.ravel(), ys.ravel()], axis=-1) + 0.5
        points *= s
        stride_col = np.full((points.shape[0], 1), s, dtype=np.float32)
        all_points.append(np.concatenate([points, stride_col], axis=-1))
    return np.concatenate(all_points, axis=0)  # (N, 3) — cx, cy, stride


# Pre-computed anchor cache
_anchor_cache: np.ndarray | None = None

def _get_cached_anchors() -> np.ndarray:
    global _anchor_cache
    if _anchor_cache is None:
        _anchor_cache = _generate_yolov8_anchors(INPUT_SIZE)
    return _anchor_cache


# Pre-computed DFL weight vector
_DFL_WEIGHTS = np.arange(16, dtype=np.float32)

def _dfl_decode(dfl_tensor: np.ndarray, reg_max: int = 16) -> np.ndarray:
    """Decode DFL (Distribution Focal Loss) regression to box offsets.

    dfl_tensor: (N, 4 * reg_max) → returns (N, 4) offsets [left, top, right, bottom].
    """
    n = dfl_tensor.shape[0]
    dfl = dfl_tensor.reshape(n, 4, reg_max)
    # softmax over reg_max dimension
    dfl_exp = np.exp(dfl - dfl.max(axis=-1, keepdims=True))
    dfl_softmax = dfl_exp / dfl_exp.sum(axis=-1, keepdims=True)
    return (dfl_softmax * _DFL_WEIGHTS).sum(axis=-1)  # (N, 4)


def _decode_yolov8_decoupled(
    cls_tensor: np.ndarray,
    box_tensors: list[np.ndarray],
    log: logging.Logger,
) -> list[dict]:
    """Decode YOLOv8 decoupled head outputs (class scores + DFL box regression).

    cls_tensor: (N, num_classes) — class probabilities (already sigmoid from HEF)
    box_tensors: list of arrays — one should be (N, 64) for DFL, one (N, 24) for raw regression
    """
    num_classes = cls_tensor.shape[-1]
    n_anchors = cls_tensor.shape[0]

    # Apply sigmoid if values are outside [0, 1] (raw logits from HEF)
    cls_max = float(cls_tensor.max())
    cls_min = float(cls_tensor.min())
    if cls_max > 1.0 or cls_min < 0.0:
        cls_tensor = 1.0 / (1.0 + np.exp(-np.clip(cls_tensor, -50, 50)))

    # Find the DFL tensor (64 = 4 * 16) and raw regression tensor
    dfl_tensor = None
    reg_tensor = None
    for bt in box_tensors:
        if bt.shape[0] != n_anchors:
            continue
        if bt.shape[-1] == 64:
            dfl_tensor = bt
        elif bt.shape[-1] in (24, 4):
            reg_tensor = bt

    if dfl_tensor is None:
        log.warning("No DFL tensor (64-dim) found in box outputs — cannot decode")
        return []

    # Decode DFL to box offsets (left, top, right, bottom)
    offsets = _dfl_decode(dfl_tensor, reg_max=16)  # (N, 4)

    # Generate anchor points (cached — INPUT_SIZE never changes at runtime)
    anchors = _get_cached_anchors()
    if anchors.shape[0] != n_anchors:
        log.warning(
            "Anchor count mismatch: generated=%d, model=%d",
            anchors.shape[0], n_anchors,
        )
        return []

    cx = anchors[:, 0]
    cy = anchors[:, 1]
    stride = anchors[:, 2]

    # Convert offsets to pixel coordinates: center ± offset * stride
    x1 = cx - offsets[:, 0] * stride
    y1 = cy - offsets[:, 1] * stride
    x2 = cx + offsets[:, 2] * stride
    y2 = cy + offsets[:, 3] * stride

    # De-letterbox: map coordinates from letterbox space to full INPUT_SIZE space
    _, pad_x, pad_y = _letterbox_params
    if pad_x > 0 or pad_y > 0:
        x1 = x1 - pad_x
        x2 = x2 - pad_x
        y1 = y1 - pad_y
        y2 = y2 - pad_y
        content_w = INPUT_SIZE - 2 * pad_x
        content_h = INPUT_SIZE - 2 * pad_y
        if content_w > 0:
            x1 = x1 * INPUT_SIZE / content_w
            x2 = x2 * INPUT_SIZE / content_w
        if content_h > 0:
            y1 = y1 * INPUT_SIZE / content_h
            y2 = y2 * INPUT_SIZE / content_h
        np.clip(x1, 0, INPUT_SIZE, out=x1)
        np.clip(y1, 0, INPUT_SIZE, out=y1)
        np.clip(x2, 0, INPUT_SIZE, out=x2)
        np.clip(y2, 0, INPUT_SIZE, out=y2)

    # Get best class per anchor — fully vectorized filtering
    class_ids = np.argmax(cls_tensor, axis=-1)
    class_scores = cls_tensor[np.arange(n_anchors), class_ids]

    # Vectorized boolean mask — no Python loop over 3549 anchors
    bw = x2 - x1
    bh = y2 - y1
    mask = class_scores >= CONF_THRESHOLD
    if SKIP_BACKGROUND:
        mask &= class_ids != 0
    mask &= class_ids < num_classes
    mask &= bw >= 4
    mask &= bh >= 4
    mask &= bw <= INPUT_SIZE * 0.9
    mask &= bh <= INPUT_SIZE * 0.9

    idx = np.where(mask)[0]
    raw_dets = [
        {
            "class_id": int(class_ids[i]),
            "class_name": DEFAULT_CLASS_NAMES[int(class_ids[i])],
            "confidence": round(float(class_scores[i]), 3),
            "bbox": [round(float(x1[i]), 2), round(float(y1[i]), 2),
                     round(float(x2[i]), 2), round(float(y2[i]), 2)],
        }
        for i in idx
    ]

    return _nms(raw_dets, max_det=15)


def _run_hailo_inference(image_bytes: bytes, log: logging.Logger) -> list[dict]:
    """Run real inference via Hailo InferVStreams API.

    DocCheck YOLOv5 models output without integrated NMS (Sigmoid/Transpose end nodes).
    Logs output structure on first inference for debugging.
    """
    global _hailo_output_logged, _inference_count
    _inference_count += 1
    m = _hailo_infer_model
    img_array = _decode_image(image_bytes)

    # Log input diagnostics on first inference only
    if _inference_count <= 1:
        log.info("DIAG input: shape=%s dtype=%s range=[%.3f, %.3f] mean=%.3f normalize=%s",
                 img_array.shape, img_array.dtype,
                 float(img_array.min()), float(img_array.max()),
                 float(img_array.mean()), NORMALIZE_INPUT)

    input_data = {m["input_name"]: img_array[np.newaxis]}
    results = m["pipeline"].infer(input_data)

    # Log output structure on first inference
    if not _hailo_output_logged:
        for k, v in results.items():
            try:
                arr = np.asarray(v)
                log.info("HEF output key=%r shape=%s dtype=%s", k, arr.shape, arr.dtype)
            except Exception as e:
                log.info("HEF output key=%r err=%s", k, e)
        _hailo_output_logged = True

    # ── Flexible output parsing (detect whether NMS is included) ─────────────

    nms_tensor = None
    ragged_nms = None  # YOLOv8 ragged NMS output

    for k, v in results.items():
        try:
            arr = np.asarray(v)
            if arr.ndim == 4 and arr.shape[-1] == 5:
                nms_tensor = arr
                break
            elif arr.ndim == 3 and arr.shape[-1] == 6:
                nms_tensor = arr
                break
        except ValueError:
            # YOLOv8 ragged NMS: [1, 80, variable_per_class, 5]
            # np.asarray fails because each class has different num detections
            ragged_nms = v
            break

    detections = []
    num_classes = len(DEFAULT_CLASS_NAMES)

    if ragged_nms is not None:
        # YOLOv8 ragged NMS output: list[list[ndarray]]
        # Structure: ragged_nms[batch][class_id] = array of shape [num_dets, 5]
        raw_dets = []
        batch = ragged_nms[0] if len(ragged_nms) > 0 else []
        for class_id, class_dets in enumerate(batch):
            if SKIP_BACKGROUND and class_id == 0:
                continue
            if class_id >= num_classes:
                continue
            class_arr = np.asarray(class_dets)
            if class_arr.ndim != 2 or class_arr.shape[-1] != 5:
                continue
            for det in class_arr:
                score = min(float(det[4]), 1.0)
                if score < CONF_THRESHOLD:
                    continue
                y1, x1, y2, x2 = float(det[0]), float(det[1]), float(det[2]), float(det[3])
                if max(x1, y1, x2, y2) <= 1.0:
                    x1, y1, x2, y2 = x1 * INPUT_SIZE, y1 * INPUT_SIZE, x2 * INPUT_SIZE, y2 * INPUT_SIZE
                bw, bh = x2 - x1, y2 - y1
                if bw < 10 or bh < 10:
                    continue
                if bw > INPUT_SIZE * 0.9 or bh > INPUT_SIZE * 0.9:
                    continue
                raw_dets.append({
                    "class_id": class_id,
                    "class_name": DEFAULT_CLASS_NAMES[class_id],
                    "confidence": round(score, 3),
                    "bbox": [round(x1, 2), round(y1, 2), round(x2, 2), round(y2, 2)],
                })
        detections = _nms(raw_dets, max_det=15)

    elif nms_tensor is not None:
        # Regular NMS tensor (homogeneous shape)
        arr = nms_tensor[0]
        raw_dets = []

        if arr.ndim == 3:  # [num_classes, max_boxes, 5]
            for class_id, class_dets in enumerate(arr):
                if SKIP_BACKGROUND and class_id == 0:
                    continue
                if class_id >= num_classes:
                    continue
                for det in class_dets:
                    score = min(float(det[4]), 1.0)
                    if score < CONF_THRESHOLD:
                        continue
                    x1, y1, x2, y2 = float(det[0]), float(det[1]), float(det[2]), float(det[3])
                    if max(x1, y1, x2, y2) <= 1.0:
                        x1, y1, x2, y2 = x1 * INPUT_SIZE, y1 * INPUT_SIZE, x2 * INPUT_SIZE, y2 * INPUT_SIZE
                    bw, bh = x2 - x1, y2 - y1
                    if bw < 10 or bh < 10:
                        continue
                    if bw > INPUT_SIZE * 0.9 or bh > INPUT_SIZE * 0.9:
                        continue
                    raw_dets.append({
                        "class_id": class_id,
                        "class_name": DEFAULT_CLASS_NAMES[class_id],
                        "confidence": round(score, 3),
                        "bbox": [round(x1, 2), round(y1, 2), round(x2, 2), round(y2, 2)],
                    })
        elif arr.ndim == 2:  # [max_boxes, 6] -> [ymin, xmin, ymax, xmax, score, class_id]
            for det in arr:
                score = min(float(det[4]), 1.0)
                if score < CONF_THRESHOLD:
                    continue
                class_id = int(det[5])
                if SKIP_BACKGROUND and class_id == 0:
                    continue
                if class_id >= num_classes:
                    continue
                x1, y1, x2, y2 = float(det[0]), float(det[1]), float(det[2]), float(det[3])
                if max(x1, y1, x2, y2) <= 1.0:
                    x1, y1, x2, y2 = x1 * INPUT_SIZE, y1 * INPUT_SIZE, x2 * INPUT_SIZE, y2 * INPUT_SIZE
                bw, bh = x2 - x1, y2 - y1
                if bw < 10 or bh < 10:
                    continue
                if bw > INPUT_SIZE * 0.9 or bh > INPUT_SIZE * 0.9:
                    continue
                raw_dets.append({
                    "class_id": class_id,
                    "class_name": DEFAULT_CLASS_NAMES[class_id],
                    "confidence": round(score, 3),
                    "bbox": [round(x1, 2), round(y1, 2), round(x2, 2), round(y2, 2)],
                })

        # Secondary NMS (Hailo built-in NMS may not be aggressive enough)
        detections = _nms(raw_dets, max_det=15)
    else:
        # ── YOLOv8 decoupled head (SurgeoNet 416) ──────────────────────
        # Three separate outputs, all sharing the same anchor count:
        #   activation1: (1, 1, N, num_classes) — class probabilities (sigmoid)
        #   concat16:    (1, 1, N, 4*reg_max)   — box regression (DFL or raw)
        #   concat14:    (1, 1, N, 64)           — DFL distribution features
        # Detect by: 3 outputs, one has last_dim == num_classes
        output_arrays = {}
        for k, v in results.items():
            arr = np.asarray(v)
            # Squeeze batch dims: (1, 1, N, C) → (N, C)
            while arr.ndim > 2 and arr.shape[0] == 1:
                arr = arr[0]
            output_arrays[k] = arr

        cls_tensor = None
        box_tensors = []
        for k, arr in output_arrays.items():
            if arr.shape[-1] == num_classes:
                cls_tensor = arr
            else:
                box_tensors.append(arr)

        if cls_tensor is not None and box_tensors:
            detections = _decode_yolov8_decoupled(cls_tensor, box_tensors, log)
        else:
            # Fallback: raw feature map without NMS
            raw_boxes = []
            for k, v in results.items():
                arr = np.asarray(v)[0]
                if arr.shape[-1] > 4:
                    arr = arr.reshape(-1, arr.shape[-1])
                    raw_boxes.append(arr)
            if raw_boxes:
                try:
                    merged = np.vstack(raw_boxes)
                    detections = _postprocess_yolo(merged)
                except ValueError as exc:
                    log.warning("Raw feature vstack failed: %s — returning empty", exc)

    return detections


def _run_simulation_inference(image_bytes: bytes) -> list[dict]:
    """
    Simulation inference (no hardware required). Based on DocCheck 960x960 dimensions.
    """
    time.sleep(0.02)
    rng = np.random.default_rng(int(time.time() * 1000) % 2**32)
    n_objects = rng.integers(1, 5)
    detections = []
    for _ in range(n_objects):
        cls_id = rng.integers(0, len(DEFAULT_CLASS_NAMES))
        x1, y1 = rng.integers(0, 600, size=2).tolist()
        x2, y2 = (x1 + rng.integers(100, 300), y1 + rng.integers(100, 300))

        detections.append({
            "class_id": int(cls_id),
            "class_name": DEFAULT_CLASS_NAMES[cls_id],
            "confidence": round(float(rng.uniform(0.6, 0.99)), 3),
            "bbox": [float(x1), float(y1), float(x2), float(y2)],
        })
    return detections


def _cleanup_hailo() -> None:
    """Release Hailo resources on process exit."""
    global _hailo_infer_model
    if _hailo_infer_model is not None:
        try:
            if "pipeline" in _hailo_infer_model:
                _hailo_infer_model["pipeline"].__exit__(None, None, None)
            if "activated_context" in _hailo_infer_model:
                _hailo_infer_model["activated_context"].__exit__(None, None, None)
            _hailo_infer_model["target"].release()
        except Exception:
            pass
        _hailo_infer_model = None


# ─────────────────────────────────────────────────────────────────────────────
# Image decoding / YOLO post-processing utilities
# ─────────────────────────────────────────────────────────────────────────────

# Letterbox state — updated per frame, read by _decode_yolov8_decoupled for de-letterboxing
_letterbox_params: tuple[float, int, int] = (1.0, 0, 0)  # (scale, pad_x, pad_y)


def _letterbox_resize(img: np.ndarray, target_size: int) -> tuple[np.ndarray, float, int, int]:
    """Letterbox resize: fit image into target_size x target_size with gray padding.

    Returns (letterboxed_image, scale, pad_x, pad_y).
    """
    import cv2  # type: ignore[import]
    h, w = img.shape[:2]
    scale = target_size / max(h, w)
    new_w, new_h = int(w * scale), int(h * scale)
    resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((target_size, target_size, 3), 114, dtype=np.uint8)
    pad_x = (target_size - new_w) // 2
    pad_y = (target_size - new_h) // 2
    canvas[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = resized
    return canvas, scale, pad_x, pad_y


def _decode_image(image_bytes: bytes) -> np.ndarray:
    """Binary → (INPUT_SIZE, INPUT_SIZE, 3) uint8 numpy array with letterbox padding."""
    global _letterbox_params
    import cv2  # type: ignore[import]

    buf = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)  # BGR uint8
    if img is None:
        from PIL import Image  # type: ignore[import]
        pil_img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img = np.asarray(pil_img, dtype=np.uint8)
        if COLOR_ORDER == "bgr":
            img = img[:, :, ::-1]
    elif COLOR_ORDER != "bgr":
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

    letterboxed, scale, pad_x, pad_y = _letterbox_resize(img, INPUT_SIZE)
    _letterbox_params = (scale, pad_x, pad_y)

    if NORMALIZE_INPUT:
        arr = letterboxed.astype(np.float32) * (1.0 / 255.0)
        return arr
    return letterboxed  # uint8, [0, 255]


def _sigmoid(x: Any) -> Any:
    """Numerically stable sigmoid."""
    x = np.clip(x, -50, 50)
    return 1.0 / (1.0 + np.exp(-x))


def _postprocess_yolo(raw: Any) -> list[dict]:
    """
    Post-process YOLO output tensors without integrated NMS.

    YOLOv5 raw feature maps output logits, so sigmoid must be applied
    to convert to probabilities (0~1).

    Filtering:
      1) sigmoid(objectness) * sigmoid(class_score) >= CONF_THRESHOLD
      2) Exclude class_id == 0 (Background)
      3) Exclude class_id >= len(DEFAULT_CLASS_NAMES) (ghost class guard)
      4) Exclude bboxes that are too small or too large
    """
    raw = np.asarray(raw)
    if raw.ndim == 1:
        return []

    results = []
    num_classes = len(DEFAULT_CLASS_NAMES)
    num_elements = raw.shape[-1]
    is_yolov5 = num_elements == (5 + num_classes)

    for row in raw:
        if is_yolov5:
            # YOLOv5: [cx, cy, w, h, obj_logit, cls0_logit, cls1_logit, ...]
            obj_score = float(_sigmoid(row[4]))
            if obj_score < CONF_THRESHOLD:
                continue
            cls_logits = row[5:5 + num_classes]
            cls_probs = _sigmoid(cls_logits)
        else:
            # YOLOv8/other: [cx, cy, w, h, cls0, cls1, ...]
            obj_score = 1.0
            cls_logits = row[4:4 + num_classes]
            if len(cls_logits) == 0:
                continue
            cls_probs = _sigmoid(cls_logits)

        cls_id = int(np.argmax(cls_probs))
        confidence = min(float(cls_probs[cls_id]) * obj_score, 1.0)

        if confidence < CONF_THRESHOLD:
            continue

        if SKIP_BACKGROUND and cls_id == 0:
            continue

        if cls_id >= num_classes:
            continue

        cx, cy, w, h = float(row[0]), float(row[1]), float(row[2]), float(row[3])

        # Convert normalized coords (0~1) to pixel coords
        if cx <= 1.0 and cy <= 1.0 and w <= 1.0 and h <= 1.0:
            cx *= INPUT_SIZE
            cy *= INPUT_SIZE
            w *= INPUT_SIZE
            h *= INPUT_SIZE

        x1, y1 = cx - w / 2, cy - h / 2
        x2, y2 = cx + w / 2, cy + h / 2

        box_w = x2 - x1
        box_h = y2 - y1
        if box_w < 5 or box_h < 5:  # minimum 5px
            continue
        if box_w > INPUT_SIZE * 0.95 or box_h > INPUT_SIZE * 0.95:  # near-full-frame
            continue

        cls_name = DEFAULT_CLASS_NAMES[cls_id]
        results.append({
            "class_id": cls_id,
            "class_name": cls_name,
            "confidence": round(confidence, 3),
            "bbox": [round(float(v), 2) for v in (x1, y1, x2, y2)],
        })

    return _nms(results)


def _nms(detections: list[dict], max_det: int = 15) -> list[dict]:
    """Two-stage NMS for INT8 quantized models.

    Stage 1: Per-class NMS at IOU_THRESHOLD — same-class duplicates suppressed.
    Stage 2: Cross-class NMS at 0.75 — near-identical boxes with different class
             predictions (INT8 flip-flopping) are merged, keeping highest confidence.
    """
    if not detections:
        return []
    detections.sort(key=lambda d: d["confidence"], reverse=True)
    # Stage 1: per-class NMS
    stage1: list[dict] = []
    for det in detections:
        is_dup = False
        for k in stage1:
            if det["class_id"] == k["class_id"] and _iou(det["bbox"], k["bbox"]) >= IOU_THRESHOLD:
                is_dup = True
                break
        if not is_dup:
            stage1.append(det)
    # Stage 2: cross-class NMS for near-identical boxes (same physical object, different class)
    # Threshold is lower than stage 1 (0.55 vs IOU_THRESHOLD) to account for INT8 bbox variance
    # where the same object's box can shift 10-20px between class predictions.
    stage2: list[dict] = []
    for det in stage1:
        if len(stage2) >= max_det:
            break
        is_dup = False
        for k in stage2:
            if _iou(det["bbox"], k["bbox"]) >= 0.55:
                is_dup = True
                break
        if not is_dup:
            stage2.append(det)
    return stage2


def _iou(a: list[float], b: list[float]) -> float:
    inter_x1 = max(a[0], b[0])
    inter_y1 = max(a[1], b[1])
    inter_x2 = min(a[2], b[2])
    inter_y2 = min(a[3], b[3])
    inter = max(0.0, inter_x2 - inter_x1) * max(0.0, inter_y2 - inter_y1)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0

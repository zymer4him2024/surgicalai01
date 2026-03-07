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
import logging
import multiprocessing as mp
import os
import time
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# COCO 80-class labels (YOLOv8 default model — POC demo)
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_CLASS_NAMES: list[str] = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep",
    "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard",
    "sports ball", "kite", "baseball bat", "baseball glove", "skateboard",
    "surfboard", "tennis racket", "bottle", "wine glass", "cup", "fork",
    "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
    "couch", "potted plant", "bed", "dining table", "toilet", "tv",
    "laptop", "mouse", "remote", "keyboard", "cell phone", "microwave",
    "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase",
    "scissors", "teddy bear", "hair drier", "toothbrush",
]

# ─────────────────────────────────────────────────────────────────────────────
# POC mapping: COCO class → surgical instrument name
# Only classes in this mapping are shown in the display.
# ─────────────────────────────────────────────────────────────────────────────
SURGICAL_MAPPING: dict[str, str] = {
    "scissors": "Surgical Scissors",
    "knife": "Scalpel",
    "fork": "Forceps",
    "spoon": "Retractor",
    "bowl": "Bowl",
    "cup": "Container",
    "bottle": "Bottle",
    "cell phone": "Probe Device",
    "remote": "Cautery Pen",
    "toothbrush": "Brush",
}

# Model input resolution (YOLOv8: 640x640)
INPUT_SIZE = 640

# NMS parameters
CONF_THRESHOLD = 0.50
IOU_THRESHOLD = 0.45

# COCO model has no Background class
SKIP_BACKGROUND = False

# Flag to log output structure once on first inference
_hailo_output_logged = False


# ─────────────────────────────────────────────────────────────────────────────
# POC: COCO → surgical instrument mapping filter
# ─────────────────────────────────────────────────────────────────────────────

def _apply_surgical_mapping(detections: list[dict]) -> list[dict]:
    """Convert COCO detection results to surgical instrument names.
    Classes not in SURGICAL_MAPPING are dropped entirely.
    """
    mapped = []
    for det in detections:
        coco_name = det.get("class_name", "")
        if coco_name in SURGICAL_MAPPING:
            mapped.append({
                **det,
                "class_name": SURGICAL_MAPPING[coco_name],
                "coco_class": coco_name,  # preserve original COCO name
            })
    return mapped


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

        input_vstreams_params = InputVStreamParams.make_from_network_group(
            network_group, quantized=False, format_type=FormatType.FLOAT32
        )
        output_vstreams_params = OutputVStreamParams.make_from_network_group(
            network_group, quantized=False, format_type=FormatType.FLOAT32
        )

        _hailo_infer_model = {
            "target": target,
            "network_group": network_group,
            "network_group_params": network_group_params,
            "input_params": input_vstreams_params,
            "output_params": output_vstreams_params,
            "hef": hef,
        }
        log.info("Hailo HEF loaded successfully: %s", hef_path)
        return True
    except Exception as exc:
        log.warning("Hailo SDK unavailable (%s) — simulation mode", exc)
        return False


def _run_hailo_inference(image_bytes: bytes, log: logging.Logger) -> list[dict]:
    """Run real inference via Hailo InferVStreams API.

    DocCheck YOLOv5 models output without integrated NMS (Sigmoid/Transpose end nodes).
    Logs output structure on first inference for debugging.
    """
    from hailo_platform import InferVStreams  # type: ignore[import]

    global _hailo_output_logged
    m = _hailo_infer_model
    img_array = _decode_image(image_bytes)
    input_data = {
        m["hef"].get_input_vstream_infos()[0].name: img_array[np.newaxis]
    }

    with InferVStreams(
        m["network_group"],
        m["input_params"],
        m["output_params"],
    ) as pipeline:
        with m["network_group"].activate(m["network_group_params"]):
            results = pipeline.infer(input_data)

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
        # Raw feature map without NMS (e.g. DocCheck 12-class)
        raw_boxes = []
        for k, v in results.items():
            arr = np.asarray(v)[0]  # remove batch
            if arr.shape[-1] > 4:
                arr = arr.reshape(-1, arr.shape[-1])
                raw_boxes.append(arr)

        if raw_boxes:
            merged = np.vstack(raw_boxes)
            detections = _postprocess_yolo(merged)

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
            _hailo_infer_model["target"].release()
        except Exception:
            pass
        _hailo_infer_model = None


# ─────────────────────────────────────────────────────────────────────────────
# Image decoding / YOLO post-processing utilities
# ─────────────────────────────────────────────────────────────────────────────

def _decode_image(image_bytes: bytes) -> np.ndarray:
    """Binary → (INPUT_SIZE, INPUT_SIZE, 3) float32 numpy array."""
    from PIL import Image  # type: ignore[import]

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB").resize((INPUT_SIZE, INPUT_SIZE))
    # Hailo HEF includes internal normalization — pass [0, 255] float32 as-is
    return np.asarray(img, dtype=np.float32)


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
    """Simple NMS: IoU-based deduplication with max detection cap."""
    if not detections:
        return []
    detections.sort(key=lambda d: d["confidence"], reverse=True)
    kept: list[dict] = []
    for det in detections:
        if len(kept) >= max_det:
            break
        if all(_iou(det["bbox"], k["bbox"]) < IOU_THRESHOLD for k in kept):
            kept.append(det)
    return kept


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

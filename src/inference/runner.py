"""
runner.py — 별도 프로세스에서 실행되는 Hailo-8 추론 워커

설계 목적:
  - FastAPI 메인 프로세스와 분리하여 NPU 드라이버 메모리 누수 격리
  - 추론 프로세스 크래시 시 FastAPI 서버는 영향받지 않음
  - 프로세스 재시작으로 메모리/리소스 완전 정리 가능

통신 방식:
  - request_queue  : (request_id, image_bytes) 전송
  - response_queue : (request_id, result_dict | error_str) 수신
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
# COCO 80-class 레이블 (YOLOv8 기본 모델용 — POC 데모)
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
# POC 매핑: COCO 클래스 → 수술 기구 명칭
# 이 매핑에 포함된 클래스만 디스플레이에 표시됨
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

# 모델 입력 해상도 (YOLOv8: 640x640)
INPUT_SIZE = 640

# NMS 파라미터 (COCO 모델은 정확하므로 낮은 임계값 사용 가능)
CONF_THRESHOLD = 0.50
IOU_THRESHOLD = 0.45

# COCO 모델은 Background 클래스 없음
SKIP_BACKGROUND = False

# 출력 구조 로깅 플래그 (첫 추론 시 1회만 출력)
_hailo_output_logged = False


# ─────────────────────────────────────────────────────────────────────────────
# POC: COCO → 수술 기구 매핑 필터
# ─────────────────────────────────────────────────────────────────────────────

def _apply_surgical_mapping(detections: list[dict]) -> list[dict]:
    """COCO 탐지 결과를 수술 기구 명칭으로 변환.
    SURGICAL_MAPPING에 없는 클래스는 완전히 제거됩니다.
    """
    mapped = []
    for det in detections:
        coco_name = det.get("class_name", "")
        if coco_name in SURGICAL_MAPPING:
            mapped.append({
                **det,
                "class_name": SURGICAL_MAPPING[coco_name],
                "coco_class": coco_name,  # 원본 COCO 이름 보존
            })
    return mapped


# ─────────────────────────────────────────────────────────────────────────────
# 추론 워커 함수 (별도 프로세스에서 실행)
# ─────────────────────────────────────────────────────────────────────────────

def inference_worker(
    hef_path: str,
    request_queue: mp.Queue,  # type: ignore[type-arg]
    response_queue: mp.Queue,  # type: ignore[type-arg]
    stop_event: mp.Event,  # type: ignore[type-arg]
) -> None:
    """
    NPU 추론 루프.
    프로세스가 시작되면 HEF를 로드하고, request_queue를 폴링하며
    추론 결과를 response_queue에 전송합니다.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [InferenceWorker] %(levelname)s %(message)s",
    )
    log = logging.getLogger("inference_worker")

    # Hailo SDK 또는 시뮬레이션 모드 선택
    sdk_available = _try_load_hailo(hef_path, log)

    log.info(
        "Inference worker ready (mode=%s, hef=%s)",
        "hailo" if sdk_available else "simulation",
        hef_path,
    )

    while not stop_event.is_set():
        try:
            # 큐에서 요청 수신 (timeout으로 stop_event 폴링 보장)
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
            # POC: COCO → 수술 기구 매핑 적용 (매핑에 없는 클래스 필터링)
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
# Hailo SDK 래퍼 (선택적 의존성)
# ─────────────────────────────────────────────────────────────────────────────

_hailo_infer_model: Any = None          # SDK InferModel 객체 (전역, 프로세스 내)


def _try_load_hailo(hef_path: str, log: logging.Logger) -> bool:
    """hailort SDK로 HEF 로드 시도. 실패 시 False 반환 (시뮬레이션 모드)."""
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
    """Hailo InferVStreams API로 실제 추론 수행.

    DocCheck YOLOv5 모델은 NMS 미통합 출력 (Sigmoid/Transpose end nodes).
    첫 추론 시 출력 구조를 로깅하여 디버깅에 활용.
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

    # 첫 추론 시 출력 구조 로깅
    if not _hailo_output_logged:
        for k, v in results.items():
            try:
                arr = np.asarray(v)
                log.info("HEF output key=%r shape=%s dtype=%s", k, arr.shape, arr.dtype)
            except Exception as e:
                log.info("HEF output key=%r err=%s", k, e)
        _hailo_output_logged = True

    # ── 유연한 출력 파싱 (NMS 유무 판별) ────────────────────────────────────

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
                # Background 제외 (index 0)
                if SKIP_BACKGROUND and class_id == 0:
                    continue
                # 유효하지 않은 클래스 제외
                if class_id >= num_classes:
                    continue
                for det in class_dets:
                    score = min(float(det[4]), 1.0)
                    if score < CONF_THRESHOLD:
                        continue
                    x1, y1, x2, y2 = float(det[0]), float(det[1]), float(det[2]), float(det[3])
                    if max(x1, y1, x2, y2) <= 1.0:
                        x1, y1, x2, y2 = x1 * INPUT_SIZE, y1 * INPUT_SIZE, x2 * INPUT_SIZE, y2 * INPUT_SIZE
                    # bbox 크기 필터링
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
        
        # 2차 NMS 적용 (Hailo 내장 NMS가 충분히 공격적이지 않을 수 있음)
        detections = _nms(raw_dets, max_det=15)
    else:
        # 2) NMS가 포함되지 않은 Raw Feature Map인 경우 (DocCheck 12-class 등)
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
    하드웨어 없이 작동하는 시뮬레이션 추론 (DocCheck 960x960 기준).
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
    """프로세스 종료 시 Hailo 리소스 해제."""
    global _hailo_infer_model
    if _hailo_infer_model is not None:
        try:
            _hailo_infer_model["target"].release()
        except Exception:
            pass
        _hailo_infer_model = None


# ─────────────────────────────────────────────────────────────────────────────
# 이미지 디코딩 / YOLO 포스트프로세싱 유틸
# ─────────────────────────────────────────────────────────────────────────────

def _decode_image(image_bytes: bytes) -> np.ndarray:
    """바이너리 → (INPUT_SIZE, INPUT_SIZE, 3) float32 numpy 배열 변환."""
    from PIL import Image  # type: ignore[import]

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB").resize((INPUT_SIZE, INPUT_SIZE))
    # Hailo HEF는 내부 정규화 포함 — [0, 255] float32 그대로 전달
    return np.asarray(img, dtype=np.float32)


def _sigmoid(x: Any) -> Any:
    """Numerically stable sigmoid."""
    x = np.clip(x, -50, 50)
    return 1.0 / (1.0 + np.exp(-x))


def _postprocess_yolo(raw: Any) -> list[dict]:
    """
    NMS가 없는 YOLO 출력 텐서 포스트프로세싱.
    
    핵심: YOLOv5 raw feature map은 로짓(logit)을 출력하므로
    반드시 sigmoid를 적용해야 확률(0~1)로 변환됩니다.
    
    필터링:
      1) sigmoid(objectness) * sigmoid(class_score) >= CONF_THRESHOLD
      2) class_id == 0 (Background) 제외
      3) class_id >= len(DEFAULT_CLASS_NAMES) 제외 (유령 클래스 차단)
      4) bbox 면적이 너무 작거나 큰 경우 제외
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
            cls_logits = row[5:5 + num_classes]  # 14개 클래스만 추출
            cls_probs = _sigmoid(cls_logits)
        else:
            # YOLOv8/기타: [cx, cy, w, h, cls0, cls1, ...]
            obj_score = 1.0
            cls_logits = row[4:4 + num_classes]
            if len(cls_logits) == 0:
                continue
            cls_probs = _sigmoid(cls_logits)

        cls_id = int(np.argmax(cls_probs))
        confidence = min(float(cls_probs[cls_id]) * obj_score, 1.0)
        
        if confidence < CONF_THRESHOLD:
            continue

        # Background 클래스 (index 0) 필터링
        if SKIP_BACKGROUND and cls_id == 0:
            continue

        # 유효하지 않은 클래스 ID 필터링
        if cls_id >= num_classes:
            continue

        cx, cy, w, h = float(row[0]), float(row[1]), float(row[2]), float(row[3])
        
        # 정규화 좌표(0~1) → 픽셀 좌표로 변환
        if cx <= 1.0 and cy <= 1.0 and w <= 1.0 and h <= 1.0:
            cx *= INPUT_SIZE
            cy *= INPUT_SIZE
            w *= INPUT_SIZE
            h *= INPUT_SIZE

        x1, y1 = cx - w / 2, cy - h / 2
        x2, y2 = cx + w / 2, cy + h / 2

        # bbox 면적 유효성 검사 (너무 작거나 음수 면적 제거)
        box_w = x2 - x1
        box_h = y2 - y1
        if box_w < 5 or box_h < 5:  # 최소 5px
            continue
        if box_w > INPUT_SIZE * 0.95 or box_h > INPUT_SIZE * 0.95:  # 화면 거의 전체
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
    """단순 NMS: IoU 기반 중복 제거 + 최대 탐지 수 제한."""
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

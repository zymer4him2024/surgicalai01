# Model Conversion Guide: .pt → HEF (Hailo-8)

## Overview

Converts a YOLOv8 `.pt` model to Hailo `.hef` format for deployment on RPi5 + Hailo-8.

**Pipeline:** `.pt` → `.onnx` → `.har` → `_optimized.har` → `.hef`

**Where it runs:** Ubuntu x86 machine (192.168.12.149) using the Hailo AI SW Suite Docker container.

---

## Prerequisites

- Hailo AI SW Suite Docker image loaded: `hailo8_ai_sw_suite_2025-10:1`
- SW Suite container running: `hailo8_ai_sw_suite_2025-10_container`
- Shared dir: `~/Downloads/hailo8_ai_sw_suite_2025-10_docker/shared_with_docker/`

Start the SW Suite container if not running:
```bash
docker start hailo8_ai_sw_suite_2025-10_container
```

---

## Automated Pipeline (via Dashboard)

1. Go to admin dashboard → **Models** → **Convert → HEF**
2. Upload your `.pt` file and fill in the form
3. The converter service running on Ubuntu picks up the job within 10 seconds
4. Monitor progress in the Models page progress bar

The converter service runs permanently in the background. Check logs:
```bash
tail -f ~/converter.log
```

---

## Manual Conversion (Step-by-Step)

Use this if the automated pipeline fails or for debugging.

### Step 0 — Set up working directory

```bash
WORK=~/Downloads/hailo8_ai_sw_suite_2025-10_docker/shared_with_docker/my_model
mkdir -p $WORK
cp /path/to/model.pt $WORK/original.pt
```

### Step 1 — Export .pt → .onnx

```bash
cd $WORK
python3 - << 'EOF'
from ultralytics import YOLO
model = YOLO("original.pt")
model.export(format="onnx", imgsz=640, opset=11, dynamic=False, simplify=True)
EOF
```

Output: `original.onnx`

### Step 2 — Parse .onnx → .har

**For YOLOv8 models** (must use explicit end nodes to avoid unsupported sigmoid/concat output layers):

```bash
docker exec hailo8_ai_sw_suite_2025-10_container bash -c "cd /local/shared_with_docker/my_model && hailo parser onnx original.onnx --net-name original --hw-arch hailo8 -y --end-node-names /model.22/cv2.0/cv2.0.2/Conv /model.22/cv2.1/cv2.1.2/Conv /model.22/cv2.2/cv2.2.2/Conv /model.22/cv3.0/cv3.0.2/Conv /model.22/cv3.1/cv3.1.2/Conv /model.22/cv3.2/cv3.2.2/Conv"
```

Output: `original.har`

> **Why these end nodes?** YOLOv8's default ONNX export includes a sigmoid/concat decode head that Hailo-8 cannot compile (`16x4 kernel not supported`). Cutting at the 6 Conv outputs of the cv2/cv3 detection branches avoids this. HailoRT handles the decode natively at inference time.

**For other ONNX models** (auto end node detection):
```bash
docker exec hailo8_ai_sw_suite_2025-10_container bash -c "cd /local/shared_with_docker/my_model && hailo parser onnx original.onnx --net-name original --hw-arch hailo8 -y"
```

### Step 3 — Optimize .har → _optimized.har (INT8 quantization)

```bash
docker exec hailo8_ai_sw_suite_2025-10_container bash -c "cd /local/shared_with_docker/my_model && hailo optimize original.har --hw-arch hailo8 --use-random-calib-set"
```

Output: `original_optimized.har`

> **Note:** Without GPU, optimization runs at level 0. Accuracy may be slightly reduced. For production, use a calibration dataset (`--calib-set-path /path/to/images`) on a machine with GPU.

### Step 4 — Compile _optimized.har → .hef

```bash
docker exec hailo8_ai_sw_suite_2025-10_container bash -c "cd /local/shared_with_docker/my_model && hailo compiler original_optimized.har --hw-arch hailo8"
```

Output: `original.hef`

This step takes ~45–60 minutes for YOLOv8m. The compiler partitions the model across 3 contexts on Hailo-8.

---

## Run All Steps in One Command

```bash
WORK=/local/shared_with_docker/my_model
docker exec hailo8_ai_sw_suite_2025-10_container bash -c "cd $WORK && hailo parser onnx original.onnx --net-name original --hw-arch hailo8 -y --end-node-names /model.22/cv2.0/cv2.0.2/Conv /model.22/cv2.1/cv2.1.2/Conv /model.22/cv2.2/cv2.2.2/Conv /model.22/cv3.0/cv3.0.2/Conv /model.22/cv3.1/cv3.1.2/Conv /model.22/cv3.2/cv3.2.2/Conv && hailo optimize original.har --hw-arch hailo8 --use-random-calib-set && hailo compiler original_optimized.har --hw-arch hailo8"
```

---

## Troubleshooting

### `hailo optimize` fails: `Bad CRC-32 for file`
The HAR file is corrupted. Re-run hailo parser to regenerate it.

### `hailo compiler` fails: `16x4 is not supported in activation1`
You used the default end nodes which include the sigmoid decode head. Use the explicit YOLOv8 end nodes in Step 2.

### `hailo parser` fails: `Unable to find end node name`
Node names differ from expected. Inspect the ONNX to find the actual Conv node names at the detection head:
```bash
docker exec hailo8_ai_sw_suite_2025-10_container python3 -c "import onnx; m=onnx.load('/local/shared_with_docker/my_model/original.onnx'); [print(n.name) for n in m.graph.node if '22' in n.name and n.op_type=='Conv']"
```

### Compilation takes very long (>60 min)
Normal for YOLOv8m on CPU-only machine. YOLOv8n/s are significantly faster. Do not interrupt.

### `hailo optimize` warning: optimization level 0
No GPU available. Output HEF will work but may have slightly lower accuracy than GPU-optimized version. Acceptable for development and testing.

---

## Timing Reference (Intel i5-2400, CPU only)

| Step | YOLOv8m |
|------|---------|
| Export ONNX | ~20s |
| Parse | ~5s |
| Optimize (calibration) | ~2–3 min |
| Compile | ~45–60 min |
| **Total** | **~50–65 min** |

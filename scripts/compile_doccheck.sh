#!/bin/bash
# DocCheck YOLOv5 HEF Compilation Helper
# Resolution: 960x960, Classes: 12

set -e

echo "=== DocCheck HEF Compilation Helper ==="

# 1. Environment Check
if [[ "$VIRTUAL_ENV" != *"hailo_env"* ]]; then
    echo "ERROR: Please activate your hailo_env first!"
    echo "Run: source ~/hailo_env/bin/activate"
    exit 1
fi

# 2. Dependencies (Strict versions for DFC 3.33.0)
echo "[1/4] Fixing dependency versions..."
pip install onnx==1.16.0 protobuf==3.20.3 ml-dtypes==0.4.1 --quiet
pip install ultralytics onnxslim --quiet

# 3. Export to ONNX
# Check for any .pt file if doccheck_best.pt isn't found
if [ ! -f "doccheck_best.pt" ]; then
    FOUND_PT=$(ls *.pt 2>/dev/null | head -n 1 || true)
    if [ -n "$FOUND_PT" ]; then
        echo "Found $FOUND_PT, using it as source..."
        cp "$FOUND_PT" doccheck_best.pt
    fi
fi

if [ -f "doccheck_best.pt" ]; then
    echo "[2/4] Exporting PT to ONNX (960x960)..."
    python3 -c "from ultralytics import YOLO; model = YOLO('doccheck_best.pt'); model.export(format='onnx', imgsz=960, opset=12, simplify=True)"
    # Ultralytics export usually saves as the same name as the .pt file with .onnx extension
    ONNX_PATH="doccheck_best.onnx"
else
    # Check if an ONNX file already exists
    ONNX_PATH=$(ls *.onnx 2>/dev/null | head -n 1 || true)
fi

if [ -z "$ONNX_PATH" ] || [ ! -f "$ONNX_PATH" ]; then
    echo "ERROR: No ONNX model found and no .pt file found to export!"
    echo "Please upload your model as 'doccheck_best.pt' or any .pt file to this directory."
    exit 1
fi

echo "Using ONNX model: $ONNX_PATH"
if [ ! -f "$ONNX_PATH" ]; then
    echo "ERROR: $ONNX_PATH not found!"
    exit 1
fi

# 4. Hailo Compilation Script
echo "[3/4] Creating Hailo compilation script..."
cat <<EOF > compile_hailo_doccheck.py
import numpy as np
from hailo_sdk_client import ClientRunner

ONNX = "$ONNX_PATH"
OUT = "doccheck_yolov5.hef"

# Hailo8L architecture (for RPi5 + Hailo8L)
runner = ClientRunner(hw_arch="hailo8l")

print("Translating ONNX...")
# Note: YOLOv5 end nodes typically end in /Conv or /BiasAdd depending on the export
# For a generic v5, we might need to inspect or use standard names
hn, npz = runner.translate_onnx_model(
    ONNX, 
    "doccheck_yolov5",
    start_node_names=["images"],
    # These are placeholders; standard YOLOv5 usually has 3 output heads
    # The actual names can be found using netron.app or hailo profiler
)

print("Optimizing with dummy calibration data...")
# 960x960 input
calib = np.random.randint(0, 255, (64, 3, 960, 960)).astype(np.float32).transpose(0, 2, 3, 1)
runner.optimize_full(hn, npz, calib_data=calib)

print("Compiling to HEF...")
hef = runner.compile(hn, npz)

with open(OUT, "wb") as f:
    f.write(hef)

print(f"DONE: {OUT} generated successfully!")
EOF

# 5. Run Compilation
echo "[4/4] Starting Hailo compilation (this may take 10-20 mins)..."
python3 compile_hailo_doccheck.py

echo "=== Success ==="
echo "Transfer doccheck_yolov5.hef to your RPi5."

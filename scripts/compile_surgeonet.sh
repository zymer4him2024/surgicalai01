#!/bin/bash
# compile_surgeonet.sh
# Run this on your Ubuntu machine with Hailo Software Suite installed.

set -e

echo "=== SurgeoNet HEF Compilation Helper ==="

# 1. Clone SurgeoNet if not exists
if [ ! -d "SurgeoNet" ]; then
    echo "[1/4] Cloning SurgeoNet repository..."
    git clone https://github.com/ATAboukhadra/SurgeoNet.git
fi
cd SurgeoNet/yolo

# 2. Setup Environment
echo "[2/4] Setting up Python dependencies..."
pip install ultralytics

# 3. Export to ONNX
# Note: Ensure you have the weights unzipped at runs/pose/s_640/weights/best.pt
if [ ! -f "runs/pose/s_640/weights/best.pt" ]; then
    echo "ERROR: runs/pose/s_640/weights/best.pt not found!"
    echo "Please unzip the yolo_models.zip provided in the instructions."
    exit 1
fi

echo "[3/4] Exporting YOLOv8s-pose to ONNX (imgsz 640)..."
python3 -c "from ultralytics import YOLO; model = YOLO('runs/pose/s_640/weights/best.pt'); model.export(format='onnx', imgsz=640)"

# 4. Compile to HEF using Hailo Model Zoo
# We use the generic yolov8s_pose configuration as a base.
echo "[4/4] Compiling to HEF for Hailo-8..."
mkdir -p output_hef

# IMPORTANT: You need calibration images for quantization.
# We will use the synthetic data from the repo if available.
CALIB_PATH="../data/synthetic_yolo/train/"
if [ ! -d "$CALIB_PATH" ]; then
    echo "WARNING: Calibration data not found at $CALIB_PATH."
    echo "Quantization may use random data, which degrades accuracy."
fi

hailomz compile yolov8s_pose \
    --onnx runs/pose/s_640/weights/best.onnx \
    --hw-arch hailo8l \
    --output-dir ./output_hef

echo "=== Done! ==="
echo "HEF file is located in: ./output_hef/yolov8s_pose.hef"
echo "Copy this file to your RPi5 models directory."

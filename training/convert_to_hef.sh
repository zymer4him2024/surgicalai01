#!/usr/bin/env bash
# =============================================================================
# convert_to_hef.sh — Convert YOLOv8 ONNX to Hailo HEF
#
# Run this on Ubuntu x86 AFTER downloading best.onnx from Kaggle/Colab.
#
# Requirements:
#   - Ubuntu 20.04 or 22.04 (x86_64)
#   - Hailo Dataflow Compiler (download from https://hailo.ai/developer-zone/)
#   - Python 3.8+
#
# Usage:
#   chmod +x convert_to_hef.sh
#   ./convert_to_hef.sh best.onnx
# =============================================================================

set -euo pipefail

ONNX_FILE="${1:-best.onnx}"
HW_ARCH="hailo8"
OUTPUT_DIR="./hef_output"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail()    { echo -e "${RED}[FAIL]${NC}  $*" >&2; exit 1; }

# ── Pre-flight checks ────────────────────────────────────────────────────────
[[ -f "${ONNX_FILE}" ]] || fail "ONNX file not found: ${ONNX_FILE}"

if ! command -v hailo &>/dev/null; then
    fail "Hailo Dataflow Compiler not found. Install from https://hailo.ai/developer-zone/"
fi

info "ONNX file : ${ONNX_FILE}"
info "Target    : ${HW_ARCH}"
info "Output    : ${OUTPUT_DIR}/"

mkdir -p "${OUTPUT_DIR}"

# ── Step 1: Parse & optimize ─────────────────────────────────────────────────
info "Step 1/3 — Parsing ONNX model..."
hailo parser onnx "${ONNX_FILE}" \
    --net-name surgical_yolov8m \
    --hw-arch "${HW_ARCH}"

# ── Step 2: Optimize (quantization) ─────────────────────────────────────────
info "Step 2/3 — Optimizing & quantizing for ${HW_ARCH}..."
# Note: for better accuracy, provide a calibration dataset:
#   --calib-path ./calib_images/
hailo optimize surgical_yolov8m.har \
    --hw-arch "${HW_ARCH}"

# ── Step 3: Compile to HEF ───────────────────────────────────────────────────
info "Step 3/3 — Compiling to HEF..."
hailo compiler surgical_yolov8m_optimized.har \
    --hw-arch "${HW_ARCH}" \
    --output-dir "${OUTPUT_DIR}"

HEF_FILE=$(find "${OUTPUT_DIR}" -name "*.hef" | head -1)

echo ""
echo "══════════════════════════════════════════════"
info "HEF created: ${HEF_FILE}"
echo ""
echo "  Next steps:"
echo "  1. Copy to RPi:"
echo "     scp ${HEF_FILE} digioptics_od@192.168.12.236:~/SurgicalAI01/models/surgical_yolov8m.hef"
echo ""
echo "  2. Update docker-compose.yml on RPi:"
echo "     HEF_PATH=/app/models/surgical_yolov8m.hef"
echo ""
echo "  3. Update DEFAULT_CLASS_NAMES in src/inference/runner.py"
echo "     (use class names printed at end of training notebook)"
echo "══════════════════════════════════════════════"

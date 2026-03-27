#!/usr/bin/env bash
# start_converter.sh — Start the Model Converter Agent on Ubuntu x86
# Run once to set up; run again any time to restart.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv_converter"
HAILO_SUITE_DIR="$HOME/Downloads/hailo8_ai_sw_suite_2025-10_docker"
HAILO_SHARED_DIR="$HAILO_SUITE_DIR/shared_with_docker"
HAILO_CONTAINER="hailo8_ai_sw_suite_2025-10_container"
HAILO_IMAGE="hailo8_ai_sw_suite_2025-10:1"

echo "=== Model Converter Agent ==="

# ── 1. Check prerequisites ────────────────────────────────────────────────────
if [ ! -f "$SCRIPT_DIR/firebase-credentials.json" ]; then
    echo "ERROR: firebase-credentials.json not found in $SCRIPT_DIR"
    exit 1
fi

if [ ! -f "$SCRIPT_DIR/.env" ] || ! grep -q "^FIREBASE_STORAGE_BUCKET=.\+" "$SCRIPT_DIR/.env"; then
    read -rp "Firebase Storage bucket (e.g. surgicalai01.appspot.com): " BUCKET
    echo "FIREBASE_STORAGE_BUCKET=$BUCKET" >> "$SCRIPT_DIR/.env"
fi

source "$SCRIPT_DIR/.env"

# ── 2. Start Hailo SW Suite container (daemon mode) ───────────────────────────
mkdir -p "$HAILO_SHARED_DIR"

if ! docker ps --format '{{.Names}}' | grep -q "^${HAILO_CONTAINER}$"; then
    echo "[...] Starting Hailo AI SW Suite container..."
    if ! docker images --format '{{.Repository}}:{{.Tag}}' | grep -q "^${HAILO_IMAGE}$"; then
        echo "[...] Loading image (this takes a few minutes)..."
        docker load -i "$HAILO_SUITE_DIR/hailo8_ai_sw_suite_2025-10.tar.gz"
    fi
    docker rm -f "$HAILO_CONTAINER" 2>/dev/null || true
    docker run -d \
        --name "$HAILO_CONTAINER" \
        --restart unless-stopped \
        --privileged --net=host --ipc=host \
        -v /dev:/dev \
        -v "$HAILO_SHARED_DIR:/local/shared_with_docker:rw" \
        "$HAILO_IMAGE" sleep infinity
    echo "[OK] Hailo SW Suite container running"
else
    echo "[OK] Hailo SW Suite container already running"
fi

# ── 3. Verify hailo CLI works inside the container ────────────────────────────
if ! docker exec "$HAILO_CONTAINER" hailo --version &>/dev/null; then
    echo "ERROR: hailo CLI not working inside SW Suite container"
    exit 1
fi
echo "[OK] hailo CLI verified"

# ── 4. Set up Python virtualenv ───────────────────────────────────────────────
if [ ! -d "$VENV" ]; then
    echo "[...] Creating Python virtualenv..."
    # Use python3.11 if available, otherwise fall back to python3
    if command -v python3.11 &>/dev/null; then
        python3.11 -m venv "$VENV"
    else
        python3 -m venv "$VENV"
    fi
fi
source "$VENV/bin/activate"

echo "[...] Installing dependencies..."
pip install --quiet --upgrade pip
pip install --quiet -r "$SCRIPT_DIR/requirements.converter.txt"

# ── 5. Run the converter service ──────────────────────────────────────────────
export FIREBASE_CREDENTIALS_PATH="$SCRIPT_DIR/firebase-credentials.json"
export FIREBASE_STORAGE_BUCKET="$FIREBASE_STORAGE_BUCKET"
export HAILO_CONTAINER_NAME="$HAILO_CONTAINER"
export HAILO_SHARED_DIR="$HAILO_SHARED_DIR"
export HAILO_CONTAINER_SHARED_DIR="/local/shared_with_docker"
export PYTHONPATH="$SCRIPT_DIR"

echo ""
echo "[OK] Starting converter service on port 8010..."
echo "     Press Ctrl+C to stop."
echo ""
cd "$SCRIPT_DIR"
python -m uvicorn src.model_converter.main:app --host 0.0.0.0 --port 8010

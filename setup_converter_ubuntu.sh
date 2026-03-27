#!/usr/bin/env bash
# setup_converter_ubuntu.sh — One-time setup for the Model Converter Agent on Ubuntu x86
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

HAILO_SUITE_DIR="$HOME/Downloads/hailo8_ai_sw_suite_2025-10_docker"
HAILO_SHARED_DIR="$HAILO_SUITE_DIR/shared_with_docker"
HAILO_CONTAINER_NAME="hailo8_ai_sw_suite_2025-10_container"
HAILO_IMAGE_NAME="hailo8_ai_sw_suite_2025-10:1"
HAILO_CONTAINER_SHARED_DIR="/local/shared_with_docker"

echo "=== Model Converter Agent — Ubuntu Setup ==="

# ── 1. Check firebase-credentials.json ───────────────────────────────────────
if [ ! -f "$SCRIPT_DIR/firebase-credentials.json" ]; then
    echo "ERROR: firebase-credentials.json not found."
    echo "  Firebase Console → Project Settings → Service Accounts → Generate new private key"
    echo "  Place the file at: $SCRIPT_DIR/firebase-credentials.json"
    exit 1
fi
echo "[OK] firebase-credentials.json found"

# ── 2. Configure .env ─────────────────────────────────────────────────────────
touch "$ENV_FILE"

if ! grep -q "^FIREBASE_STORAGE_BUCKET=.\+" "$ENV_FILE" 2>/dev/null; then
    echo ""
    read -rp "Enter Firebase Storage bucket (e.g. surgicalai01.appspot.com): " BUCKET
    sed -i '/^FIREBASE_STORAGE_BUCKET=/d' "$ENV_FILE"
    echo "FIREBASE_STORAGE_BUCKET=$BUCKET" >> "$ENV_FILE"
fi
echo "[OK] .env configured"

# ── 3. Ensure Hailo SW Suite container is running ─────────────────────────────
mkdir -p "$HAILO_SHARED_DIR"

if [ "$(docker ps -q -f name=$HAILO_CONTAINER_NAME)" ]; then
    echo "[OK] Hailo SW Suite container already running"
else
    echo "[...] Starting Hailo AI SW Suite container in daemon mode..."

    # Load image if not present
    if [ -z "$(docker images -q $HAILO_IMAGE_NAME 2>/dev/null)" ]; then
        echo "[...] Loading SW Suite Docker image (this takes a few minutes)..."
        docker load -i "$HAILO_SUITE_DIR/hailo8_ai_sw_suite_2025-10.tar.gz"
    fi

    # Remove stopped container if exists
    docker rm -f "$HAILO_CONTAINER_NAME" 2>/dev/null || true

    docker run -d \
        --name "$HAILO_CONTAINER_NAME" \
        --restart unless-stopped \
        --privileged \
        --net=host \
        --ipc=host \
        -v /dev:/dev \
        -v /lib/firmware:/lib/firmware \
        -v /lib/modules:/lib/modules \
        -v "$HAILO_SHARED_DIR:$HAILO_CONTAINER_SHARED_DIR:rw" \
        -v /var/run/docker.sock:/var/run/docker.sock \
        -v /etc/machine-id:/etc/machine-id:ro \
        "$HAILO_IMAGE_NAME" \
        sleep infinity

    echo "[OK] Hailo SW Suite container started"
fi

# ── 4. Build and start the converter service ──────────────────────────────────
set -a; source "$ENV_FILE"; set +a

echo ""
echo "[...] Building converter image..."
cd "$SCRIPT_DIR"
docker build -t model_converter_agent -f Dockerfile.converter .

docker rm -f model_converter_agent 2>/dev/null || true

docker run -d \
    --name model_converter_agent \
    --restart unless-stopped \
    -p 8010:8010 \
    -e FIREBASE_CREDENTIALS_PATH=/app/firebase-credentials.json \
    -e FIREBASE_STORAGE_BUCKET="${FIREBASE_STORAGE_BUCKET}" \
    -e POLL_INTERVAL_SEC=10 \
    -e HAILO_CONTAINER_NAME="$HAILO_CONTAINER_NAME" \
    -e HAILO_SHARED_DIR="$HAILO_SHARED_DIR" \
    -e HAILO_CONTAINER_SHARED_DIR="$HAILO_CONTAINER_SHARED_DIR" \
    -v "$SCRIPT_DIR/firebase-credentials.json:/app/firebase-credentials.json:ro" \
    -v "$SCRIPT_DIR/src/model_converter:/app/src/model_converter:ro" \
    -v "$HAILO_SHARED_DIR:$HAILO_SHARED_DIR:rw" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    model_converter_agent

echo ""
echo "[OK] Model Converter Agent is running."
echo "     Both containers restart automatically on reboot."
echo ""
echo "Useful commands:"
echo "  docker logs -f model_converter_agent          # Converter logs"
echo "  docker logs -f $HAILO_CONTAINER_NAME   # Hailo Suite logs"
echo "  curl http://localhost:8010/health              # Health check"
echo "  docker exec -it $HAILO_CONTAINER_NAME bash  # Enter Hailo Suite shell"

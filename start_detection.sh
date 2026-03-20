#!/usr/bin/env bash
set -euo pipefail

# Move to the directory where this script is located (works everywhere)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}" || exit 1

# Auto-detect OS to choose the right compose file
if [[ "$(uname -m)" == "aarch64" ]] && [[ "$(uname -s)" == "Linux" ]]; then
    # Raspberry Pi 5 Hardware
    COMPOSE="docker compose -f docker-compose.yml"
    
    # Fix X11 Permissions for the Display Agent on Pi
    # Always use DISPLAY=:0 explicitly — $DISPLAY may be unset in SSH sessions
    DISPLAY=:0 xhost +local: > /dev/null 2>&1 || true
else
    # Mac / PC Simulation
    export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
    COMPOSE="docker compose -f docker-compose.mac.yml"
fi

GATEWAY="http://localhost:8000"

echo "🚀 Starting Antigravity Edge Containers..."
$COMPOSE up -d

echo "⏳ Waiting for Gateway Agent to become ready..."
for i in $(seq 1 30); do
    if curl -sf "$GATEWAY/health" > /dev/null 2>&1; then
        echo "✅ Gateway ready (${i}s)"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "❌ Gateway timeout! Please check docker logs."
        exit 1
    fi
    sleep 1
done

echo ""
echo "✨ System is LIVE — waiting for QR scan to start detection."
echo "   Status check: curl $GATEWAY/health"

#!/usr/bin/env bash
set -euo pipefail

# Move to project directory
cd ~/SurgicalAI01 || exit 1

COMPOSE="docker compose"
GATEWAY="http://localhost:8000"

# Fix X11 Permissions for the Display Agent
if [ -n "${DISPLAY:-}" ]; then
    xhost +local: > /dev/null 2>&1 || true
fi

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
echo "   Status check: curl $GATEWAY/job/status"

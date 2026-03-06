#!/usr/bin/env bash

echo "🛑 Stopping Antigravity Edge Containers..."
cd ~/SurgicalAI01 && docker compose down
echo "✨ All containers stopped successfully."

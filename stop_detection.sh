#!/usr/bin/env bash

echo "🛑 Stopping Antigravity Edge Containers..."
cd ~/1_Antigravity/SurgicalAI01 && docker compose -f docker-compose.mac.yml down
echo "✨ All containers stopped successfully."

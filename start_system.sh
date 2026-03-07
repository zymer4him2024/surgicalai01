#!/bin/bash
export DISPLAY=:0
xhost +local:
cd ~/SurgicalAI01
docker compose up -d
# Restart display_agent so it picks up xhost permissions
docker restart display_agent
echo ""
echo "SurgicalAI started."
sleep 2

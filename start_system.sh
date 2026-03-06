#!/bin/bash
export DISPLAY=:0
xhost +local:
cd ~/SurgicalAI01
docker compose up -d
echo ""
echo "SurgicalAI started."
sleep 2

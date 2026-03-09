#!/bin/bash
# SurgicalAI startup script
# Grants X11 display access and starts all containers

set -e

cd ~/SurgicalAI01

# Allow local processes (Docker containers) to access the X display
DISPLAY=:0 xhost +local: 2>/dev/null || true

# Start containers
docker compose up -d

echo "SurgicalAI started."

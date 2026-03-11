#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}" || exit 1

if [[ "$(uname -m)" == "aarch64" ]] && [[ "$(uname -s)" == "Linux" ]]; then
    COMPOSE="docker compose -f docker-compose.yml"
else
    export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
    COMPOSE="docker compose -f docker-compose.mac.yml"
fi

echo "🛑 Stopping Antigravity Edge Containers..."
$COMPOSE down
echo "✨ All containers stopped successfully."

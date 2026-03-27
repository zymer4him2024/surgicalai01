#!/usr/bin/env bash
# =============================================================================
# launcher.sh — Antigravity Project Assignment Watcher
#
# Runs on the RPi host (outside Docker). Polls for a project_assignment.json
# written by the firebase_sync container when an admin assigns this device to
# a project from the admin dashboard.
#
# On assignment detected:
#   1. Read app_id, hef_model, compose_file from JSON
#   2. Update .env with new APP_ID and model path
#   3. Bring down the current compose stack
#   4. Start the new compose stack
#   5. Delete project_assignment.json (prevents re-trigger)
#
# Usage:
#   Run via systemd (antigravity-launcher.service) — do not run manually.
# =============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ASSIGNMENT_FILE="${PROJECT_DIR}/data/project_assignment.json"
CURRENT_COMPOSE_FILE="${PROJECT_DIR}/.current_compose"
POLL_INTERVAL=5

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "$(date '+%Y-%m-%d %H:%M:%S') ${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "$(date '+%Y-%m-%d %H:%M:%S') ${GREEN}[ OK ]${NC}  $*"; }
warn()    { echo -e "$(date '+%Y-%m-%d %H:%M:%S') ${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "$(date '+%Y-%m-%d %H:%M:%S') ${RED}[ERR ]${NC}  $*" >&2; }

# ─────────────────────────────────────────────────────────────────────────────
# Map app_id → compose file
# ─────────────────────────────────────────────────────────────────────────────
compose_for_app_id() {
    local app_id="$1"
    case "${app_id}" in
        surgical|od|inventory) echo "docker-compose.yml" ;;
        inventory_count)       echo "docker-compose.gas.yml" ;;
        *)
            error "Unknown app_id: ${app_id}"
            echo ""
            ;;
    esac
}

# ─────────────────────────────────────────────────────────────────────────────
# Update .env with new APP_ID and model
# ─────────────────────────────────────────────────────────────────────────────
update_env() {
    local app_id="$1" hef_model="$2" compose_file="$3"
    local env_file="${PROJECT_DIR}/.env"

    # Ensure APP_ID line exists and is updated
    if grep -q "^APP_ID=" "${env_file}" 2>/dev/null; then
        sed -i "s|^APP_ID=.*|APP_ID=${app_id}|" "${env_file}"
    else
        echo "APP_ID=${app_id}" >> "${env_file}"
    fi

    if [[ -n "${hef_model}" ]]; then
        if [[ "${compose_file}" == "docker-compose.gas.yml" ]]; then
            # Gas: GAS_HEF_MODEL env var
            if grep -q "^GAS_HEF_MODEL=" "${env_file}" 2>/dev/null; then
                sed -i "s|^GAS_HEF_MODEL=.*|GAS_HEF_MODEL=${hef_model}|" "${env_file}"
            else
                echo "GAS_HEF_MODEL=${hef_model}" >> "${env_file}"
            fi
        else
            # Surgical: HEF_PATH env var
            local hef_path="/app/models/${hef_model}"
            if grep -q "^HEF_PATH=" "${env_file}" 2>/dev/null; then
                sed -i "s|^HEF_PATH=.*|HEF_PATH=${hef_path}|" "${env_file}"
            else
                echo "HEF_PATH=${hef_path}" >> "${env_file}"
            fi
        fi
    fi

    info ".env updated: APP_ID=${app_id} model=${hef_model}"
}

# ─────────────────────────────────────────────────────────────────────────────
# Switch Docker Compose stack
# ─────────────────────────────────────────────────────────────────────────────
switch_stack() {
    local new_compose="$1"
    local old_compose=""

    if [[ -f "${CURRENT_COMPOSE_FILE}" ]]; then
        old_compose=$(cat "${CURRENT_COMPOSE_FILE}")
    fi

    cd "${PROJECT_DIR}"

    # Bring down existing stack
    if [[ -n "${old_compose}" && -f "${old_compose}" ]]; then
        info "Stopping current stack: ${old_compose}"
        docker compose -f "${old_compose}" down || warn "Down failed (may already be stopped)"
    else
        # Try common compose files in case no state file exists
        for f in docker-compose.yml docker-compose.gas.yml docker-compose.bootstrap.yml; do
            if [[ -f "${f}" ]]; then
                docker compose -f "${f}" down 2>/dev/null || true
            fi
        done
    fi

    # Start new stack
    info "Starting new stack: ${new_compose}"
    if docker compose -f "${new_compose}" up -d; then
        echo "${new_compose}" > "${CURRENT_COMPOSE_FILE}"
        success "Stack switched to ${new_compose}"
    else
        error "Failed to start ${new_compose}"
        return 1
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Process assignment file
# ─────────────────────────────────────────────────────────────────────────────
process_assignment() {
    local file="$1"

    # Read fields using python3 (available on all RPi OS installs)
    local app_id hef_model compose_file
    app_id=$(python3 -c "import json,sys; d=json.load(open('${file}')); print(d.get('app_id',''))" 2>/dev/null || echo "")
    hef_model=$(python3 -c "import json,sys; d=json.load(open('${file}')); print(d.get('hef_model',''))" 2>/dev/null || echo "")
    compose_file=$(python3 -c "import json,sys; d=json.load(open('${file}')); print(d.get('compose_file',''))" 2>/dev/null || echo "")

    if [[ -z "${app_id}" ]]; then
        warn "Assignment file has no app_id — skipping"
        rm -f "${file}"
        return
    fi

    # Validate compose_file; derive from app_id if missing
    if [[ -z "${compose_file}" ]]; then
        compose_file=$(compose_for_app_id "${app_id}")
    fi

    if [[ -z "${compose_file}" ]]; then
        warn "Cannot determine compose file for app_id=${app_id}"
        rm -f "${file}"
        return
    fi

    if [[ ! -f "${PROJECT_DIR}/${compose_file}" ]]; then
        error "Compose file not found: ${PROJECT_DIR}/${compose_file}"
        rm -f "${file}"
        return
    fi

    info "Project assignment: app_id=${app_id} model=${hef_model} compose=${compose_file}"

    # Update .env
    update_env "${app_id}" "${hef_model}" "${compose_file}"

    # Switch stack
    switch_stack "${compose_file}"

    # Remove assignment file (prevents re-trigger)
    rm -f "${file}"
    info "Assignment complete."
}

# ─────────────────────────────────────────────────────────────────────────────
# Bootstrap: ensure default compose is running on first start
# ─────────────────────────────────────────────────────────────────────────────
ensure_bootstrap() {
    if [[ ! -f "${CURRENT_COMPOSE_FILE}" ]]; then
        info "No current compose — starting bootstrap stack"
        cd "${PROJECT_DIR}"
        mkdir -p data
        if docker compose -f docker-compose.bootstrap.yml up -d 2>/dev/null; then
            echo "docker-compose.bootstrap.yml" > "${CURRENT_COMPOSE_FILE}"
            success "Bootstrap stack started"
        else
            warn "Bootstrap stack failed to start (may need --build first)"
        fi
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────────────────

info "Antigravity Launcher started (project=${PROJECT_DIR}, poll=${POLL_INTERVAL}s)"

mkdir -p "${PROJECT_DIR}/data"
ensure_bootstrap

while true; do
    if [[ -f "${ASSIGNMENT_FILE}" ]]; then
        info "Assignment file detected: ${ASSIGNMENT_FILE}"
        process_assignment "${ASSIGNMENT_FILE}"
    fi
    sleep "${POLL_INTERVAL}"
done

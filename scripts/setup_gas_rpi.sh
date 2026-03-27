#!/usr/bin/env bash
# =============================================================================
# setup_gas_rpi.sh — One-Click Gas Cylinder Inventory RPi Deployment
#
# Target:  Raspberry Pi 5 (64-bit Debian Bookworm) + Hailo-8
# Purpose: Sets up a fresh RPi for the gas cylinder counting application
#          (APP_ID=inventory_count) from scratch.
#
# What it does:
#   Phase 1: Pre-checks (OS, architecture, disk space, internet)
#   Phase 2: Docker CE + Docker Compose installation
#   Phase 3: Hailo-8 driver installation (if NPU present)
#   Phase 4: RPi5 performance optimization
#   Phase 5: Configure .env (interactive DEVICE_ID, LOCATION_NAME prompts)
#   Phase 6: Build and start gas containers
#
# Usage:
#   chmod +x scripts/setup_gas_rpi.sh
#   ./scripts/setup_gas_rpi.sh
#
# Re-run safe: Each phase checks if already completed and skips if so.
# =============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'
BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[ OK ]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail()    { echo -e "${RED}[FAIL]${NC} $*" >&2; }
fatal()   { echo -e "${RED}[FATAL]${NC} $*" >&2; exit 1; }

banner() {
    echo ""
    echo -e "${BOLD}${BLUE}================================================================${NC}"
    echo -e "${BOLD}${BLUE}  $*${NC}"
    echo -e "${BOLD}${BLUE}================================================================${NC}"
    echo ""
}

# ---------------------------------------------------------------------------
# State file — tracks completed phases per hostname
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
STATE_FILE="${PROJECT_DIR}/.gas_deploy_state"
CURRENT_HOST="$(hostname)"
CURRENT_USER="${SUDO_USER:-$(whoami)}"

get_completed_phase() {
    if [[ -f "${STATE_FILE}" ]]; then
        local line
        line=$(grep "^${CURRENT_HOST}:" "${STATE_FILE}" 2>/dev/null || echo "")
        if [[ -n "${line}" ]]; then
            echo "${line##*:}"
            return
        fi
    fi
    echo "0"
}

set_completed_phase() {
    local phase="$1"
    if [[ -f "${STATE_FILE}" ]]; then
        # Remove existing entry for this host
        grep -v "^${CURRENT_HOST}:" "${STATE_FILE}" > "${STATE_FILE}.tmp" 2>/dev/null || true
        mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    fi
    echo "${CURRENT_HOST}:${phase}" >> "${STATE_FILE}"
}

LAST_PHASE=$(get_completed_phase)

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

banner "Gas Cylinder Inventory — RPi Deployment Setup"
echo "  Host:     ${CURRENT_HOST}"
echo "  User:     ${CURRENT_USER}"
echo "  Project:  ${PROJECT_DIR}"
echo "  Last completed phase: ${LAST_PHASE}"
echo ""

[[ $(uname -m) == "aarch64" ]] || fatal "This script is for ARM64 (aarch64) only. Are you on an RPi5?"
[[ $(id -u) -ne 0 ]] || fatal "Do not run as root. Use a regular user with sudo access."

cd "${PROJECT_DIR}"

# Verify project files exist
if [[ ! -f "docker-compose.gas.yml" ]]; then
    fatal "docker-compose.gas.yml not found in ${PROJECT_DIR}. Sync the project first."
fi

if [[ ! -f ".env.gas.example" ]]; then
    fatal ".env.gas.example not found in ${PROJECT_DIR}. Sync the project first."
fi

# =========================================================================
# Phase 1: System Pre-checks
# =========================================================================

if [[ "${LAST_PHASE}" -lt 1 ]]; then
    banner "Phase 1/6: System Pre-checks"

    ERRORS=0

    # OS
    OS_NAME=$(. /etc/os-release && echo "${PRETTY_NAME}")
    info "OS: ${OS_NAME}"

    # Disk space (need at least 5GB for Docker images)
    DISK_FREE_GB=$(df -BG / | awk 'NR==2 {gsub("G",""); print $4}')
    if [[ "${DISK_FREE_GB:-0}" -ge 5 ]]; then
        success "Disk free: ${DISK_FREE_GB}GB"
    else
        fail "Disk free: ${DISK_FREE_GB}GB (need at least 5GB)"
        ERRORS=$((ERRORS + 1))
    fi

    # RAM
    MEM_MB=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)
    if [[ "${MEM_MB}" -ge 3500 ]]; then
        success "RAM: ${MEM_MB}MB"
    else
        fail "RAM: ${MEM_MB}MB (need at least 4GB)"
        ERRORS=$((ERRORS + 1))
    fi

    # Internet
    if curl -sf --max-time 10 "https://download.docker.com" > /dev/null 2>&1; then
        success "Internet: connected"
    else
        fail "Internet: no connection (required for Docker install)"
        ERRORS=$((ERRORS + 1))
    fi

    if [[ "${ERRORS}" -gt 0 ]]; then
        fatal "Pre-checks failed with ${ERRORS} error(s). Fix the issues above and re-run."
    fi

    set_completed_phase 1
    success "Phase 1 complete."
else
    info "Phase 1 (pre-checks): already done, skipping."
fi

# =========================================================================
# Phase 2: Docker CE + Docker Compose
# =========================================================================

if [[ "${LAST_PHASE}" -lt 2 ]]; then
    banner "Phase 2/6: Docker CE + Docker Compose"

    if command -v docker &>/dev/null && docker compose version &>/dev/null; then
        DOCKER_VER=$(docker --version | grep -oP '[\d.]+' | head -1)
        COMPOSE_VER=$(docker compose version | grep -oP '[\d.]+' | head -1)
        success "Docker already installed: ${DOCKER_VER}, Compose: ${COMPOSE_VER}"
    else
        info "Installing Docker CE..."

        # Remove unofficial packages
        for pkg in docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc; do
            if dpkg -l "${pkg}" &>/dev/null 2>&1; then
                sudo apt-get remove -y "${pkg}" 2>/dev/null || true
            fi
        done

        # Add Docker official apt repository
        sudo apt-get update -qq
        sudo apt-get install -y ca-certificates curl

        DOCKER_KEYRING="/etc/apt/keyrings/docker.asc"
        sudo install -m 0755 -d /etc/apt/keyrings
        if [[ ! -f "${DOCKER_KEYRING}" ]]; then
            sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o "${DOCKER_KEYRING}"
            sudo chmod a+r "${DOCKER_KEYRING}"
        fi

        ARCH=$(dpkg --print-architecture)
        CODENAME=$(. /etc/os-release && echo "${VERSION_CODENAME}")
        echo "deb [arch=${ARCH} signed-by=${DOCKER_KEYRING}] https://download.docker.com/linux/debian ${CODENAME} stable" \
            | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

        sudo apt-get update -qq
        sudo apt-get install -y \
            docker-ce \
            docker-ce-cli \
            containerd.io \
            docker-buildx-plugin \
            docker-compose-plugin

        success "Docker CE installed."
    fi

    # Start and enable Docker
    sudo systemctl enable docker
    sudo systemctl start docker

    # Add user to docker group
    if ! id -nG "${CURRENT_USER}" | grep -qw "docker"; then
        sudo usermod -aG docker "${CURRENT_USER}"
        success "Added ${CURRENT_USER} to docker group."
    fi

    # Docker daemon optimization
    sudo mkdir -p /etc/docker
    sudo tee /etc/docker/daemon.json > /dev/null << 'EOF'
{
    "log-driver": "json-file",
    "log-opts": { "max-size": "10m", "max-file": "3" },
    "storage-driver": "overlay2",
    "features": { "buildkit": true },
    "default-ulimits": {
        "nofile": { "name": "nofile", "hard": 65535, "soft": 65535 }
    },
    "live-restore": true
}
EOF
    sudo systemctl restart docker
    success "Docker daemon optimized."

    set_completed_phase 2
    success "Phase 2 complete."
else
    info "Phase 2 (Docker): already done, skipping."
fi

# =========================================================================
# Phase 3: Hailo-8 Driver
# =========================================================================

if [[ "${LAST_PHASE}" -lt 3 ]]; then
    banner "Phase 3/6: Hailo-8 Driver"

    if command -v hailortcli &>/dev/null && [[ -c /dev/hailo0 ]]; then
        success "Hailo-8 already installed and /dev/hailo0 present."
    elif lspci 2>/dev/null | grep -qi "hailo\|1e60"; then
        info "Hailo-8 PCIe device detected. Installing driver..."

        # PCIe config
        BOOT_CONFIG="/boot/firmware/config.txt"
        if ! grep -q "dtparam=pciex1$\|dtparam=pciex1 " "${BOOT_CONFIG}" 2>/dev/null; then
            echo "" | sudo tee -a "${BOOT_CONFIG}" > /dev/null
            echo "# Hailo-8 PCIe (setup_gas_rpi.sh)" | sudo tee -a "${BOOT_CONFIG}" > /dev/null
            echo "dtparam=pciex1" | sudo tee -a "${BOOT_CONFIG}" > /dev/null
        fi
        if ! grep -q "dtparam=pciex1_gen=3" "${BOOT_CONFIG}" 2>/dev/null; then
            echo "dtparam=pciex1_gen=3" | sudo tee -a "${BOOT_CONFIG}" > /dev/null
        fi

        # Install hailo-all
        sudo apt-get update -qq
        if apt-cache show hailo-all &>/dev/null; then
            sudo apt-get install -y hailo-all
        else
            warn "hailo-all not in apt. Install Hailo SDK manually."
        fi

        # Load kernel module
        sudo modprobe hailo_pci 2>/dev/null || true
        if ! grep -q "hailo_pci" /etc/modules 2>/dev/null; then
            echo "hailo_pci" | sudo tee -a /etc/modules > /dev/null
        fi

        # User group
        if getent group hailo &>/dev/null; then
            sudo usermod -aG hailo "${CURRENT_USER}" 2>/dev/null || true
        fi

        # udev rule
        UDEV_RULES_FILE="/etc/udev/rules.d/99-hailo.rules"
        if [[ ! -f "${UDEV_RULES_FILE}" ]]; then
            echo 'SUBSYSTEM=="hailo_chardev", KERNEL=="hailo*", GROUP="hailo", MODE="0660"' \
                | sudo tee "${UDEV_RULES_FILE}" > /dev/null
            sudo udevadm control --reload-rules
            sudo udevadm trigger
        fi

        if [[ -c /dev/hailo0 ]]; then
            success "Hailo-8 installed and /dev/hailo0 present."
        else
            warn "/dev/hailo0 not yet available. A reboot may be needed after this script."
        fi
    else
        warn "No Hailo-8 PCIe device detected. Skipping driver install."
        info "Gas module will still work with mock AI for testing."
    fi

    set_completed_phase 3
    success "Phase 3 complete."
else
    info "Phase 3 (Hailo-8): already done, skipping."
fi

# =========================================================================
# Phase 4: RPi5 Performance Optimization
# =========================================================================

if [[ "${LAST_PHASE}" -lt 4 ]]; then
    banner "Phase 4/6: RPi5 Performance Optimization"

    # CPU Governor — performance mode
    GOVERNORS=$(ls /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null || true)
    if [[ -n "${GOVERNORS}" ]]; then
        for gov in ${GOVERNORS}; do
            echo "performance" | sudo tee "${gov}" > /dev/null 2>&1 || true
        done

        # Persistent via systemd
        sudo tee /etc/systemd/system/cpu-performance.service > /dev/null << 'EOF'
[Unit]
Description=Set CPU Governor to Performance
After=multi-user.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance > $f; done'

[Install]
WantedBy=multi-user.target
EOF
        sudo systemctl daemon-reload
        sudo systemctl enable cpu-performance.service
        success "CPU governor set to performance."
    fi

    # Sysctl tuning
    SYSCTL_FILE="/etc/sysctl.d/99-gas-rpi.conf"
    sudo tee "${SYSCTL_FILE}" > /dev/null << 'EOF'
# Gas RPi performance tuning (setup_gas_rpi.sh)
vm.swappiness = 5
vm.dirty_ratio = 10
vm.dirty_background_ratio = 5
fs.file-max = 131072
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.ipv4.tcp_tw_reuse = 1
kernel.shmmax = 536870912
kernel.shmall = 131072
EOF
    sudo sysctl -p "${SYSCTL_FILE}" 2>/dev/null || true
    success "Kernel parameters tuned."

    # File descriptor limits
    sudo tee /etc/security/limits.d/99-gas-rpi.conf > /dev/null << 'EOF'
*    soft nofile 65535
*    hard nofile 65535
root soft nofile 65535
root hard nofile 65535
EOF
    success "File descriptor limits set."

    set_completed_phase 4
    success "Phase 4 complete."
else
    info "Phase 4 (optimization): already done, skipping."
fi

# =========================================================================
# Phase 5: Configure .env
# =========================================================================

if [[ "${LAST_PHASE}" -lt 5 ]]; then
    banner "Phase 5/6: Configure .env"

    if [[ -f ".env" ]]; then
        info "Existing .env found:"
        grep -E "^(APP_ID|DEVICE_ID|LOCATION_NAME)=" .env 2>/dev/null || true
        echo ""
        read -rp "Overwrite .env with gas config? [y/N]: " OVERWRITE
        if [[ "${OVERWRITE}" != "y" && "${OVERWRITE}" != "Y" ]]; then
            info "Keeping existing .env."
            set_completed_phase 5
            success "Phase 5 complete (existing .env kept)."
        fi
    fi

    # Only run interactive prompts if we didn't skip above
    if [[ "${LAST_PHASE}" -lt 5 ]] && { [[ ! -f ".env" ]] || [[ "${OVERWRITE:-}" == "y" ]] || [[ "${OVERWRITE:-}" == "Y" ]]; }; then
        # Copy template
        cp .env.gas.example .env
        success "Copied .env.gas.example -> .env"

        # Interactive DEVICE_ID
        DEFAULT_DEVICE_ID="US-Gas-$(printf '%03d' $((RANDOM % 100 + 1)))"
        echo ""
        read -rp "DEVICE_ID [${DEFAULT_DEVICE_ID}]: " INPUT_DEVICE_ID
        DEVICE_ID="${INPUT_DEVICE_ID:-${DEFAULT_DEVICE_ID}}"
        sed -i "s|^DEVICE_ID=.*|DEVICE_ID=${DEVICE_ID}|" .env
        success "DEVICE_ID=${DEVICE_ID}"

        # Interactive LOCATION_NAME
        read -rp "LOCATION_NAME [Warehouse A]: " INPUT_LOCATION
        LOCATION="${INPUT_LOCATION:-Warehouse A}"
        sed -i "s|^LOCATION_NAME=.*|LOCATION_NAME=${LOCATION}|" .env
        success "LOCATION_NAME=${LOCATION}"

        # Interactive LOW_STOCK_THRESHOLD
        read -rp "LOW_STOCK_THRESHOLD [5]: " INPUT_THRESHOLD
        THRESHOLD="${INPUT_THRESHOLD:-5}"
        sed -i "s|^LOW_STOCK_THRESHOLD=.*|LOW_STOCK_THRESHOLD=${THRESHOLD}|" .env
        success "LOW_STOCK_THRESHOLD=${THRESHOLD}"

        # Firebase credentials check
        if [[ -f "firebase-credentials.json" ]]; then
            success "firebase-credentials.json found."
        else
            warn "firebase-credentials.json not found."
            info "Firebase sync will run in simulation mode."
            info "To enable real sync, copy the service account key file to:"
            info "  ${PROJECT_DIR}/firebase-credentials.json"
        fi

        echo ""
        info "Final .env configuration:"
        cat .env
        echo ""

        set_completed_phase 5
        success "Phase 5 complete."
    fi
else
    info "Phase 5 (.env): already done, skipping."
fi

# =========================================================================
# Phase 6: Build and Start Containers
# =========================================================================

if [[ "${LAST_PHASE}" -lt 6 ]]; then
    banner "Phase 6/6: Build and Start Gas Containers"

    # Ensure docker group is active in this session
    if ! docker info &>/dev/null 2>&1; then
        info "Docker group not active in current session. Using sg..."
        exec sg docker -c "bash ${BASH_SOURCE[0]}"
    fi

    # Create data directory for SQLite queue
    mkdir -p data

    # X11 display access for HUD
    DISPLAY=:0 xhost +local: 2>/dev/null || true

    info "Building and starting gas containers..."
    docker compose -f docker-compose.gas.yml up -d --build

    echo ""
    info "Waiting for containers to become healthy..."
    sleep 10

    # Health check
    echo ""
    info "Container status:"
    docker compose -f docker-compose.gas.yml ps
    echo ""

    # Test gateway health
    if curl -sf --max-time 5 "http://localhost:8010/health" > /dev/null 2>&1; then
        success "Gas Gateway: healthy (port 8010)"
    else
        warn "Gas Gateway not responding yet. It may still be starting up."
        info "Check logs: docker compose -f docker-compose.gas.yml logs gas_gateway_agent"
    fi

    set_completed_phase 6
    success "Phase 6 complete."
else
    info "Phase 6 (containers): already done, skipping."
fi

# =========================================================================
# Done
# =========================================================================

banner "Gas Cylinder Inventory — Deployment Complete"

echo "  Application:  Gas Cylinder Inventory Counting"
echo "  APP_ID:       inventory_count"
DEVICE_ID_DISPLAY=$(grep "^DEVICE_ID=" .env 2>/dev/null | cut -d= -f2 || echo "unknown")
echo "  DEVICE_ID:    ${DEVICE_ID_DISPLAY}"
echo "  Gateway:      http://localhost:8010"
echo "  Display HUD:  HDMI output (port 8013 internal)"
echo ""
echo "  Useful commands:"
echo "    docker compose -f docker-compose.gas.yml ps          # container status"
echo "    docker compose -f docker-compose.gas.yml logs -f     # follow logs"
echo "    curl http://localhost:8010/health                    # gateway health"
echo "    curl http://localhost:8010/status                    # full status"
echo "    curl -X POST http://localhost:8010/snapshot          # manual snapshot"
echo ""
echo "    docker compose -f docker-compose.gas.yml down        # stop"
echo "    docker compose -f docker-compose.gas.yml up -d       # restart"
echo ""

if [[ ! -c /dev/hailo0 ]] && lspci 2>/dev/null | grep -qi "hailo\|1e60"; then
    warn "Hailo-8 detected but /dev/hailo0 not available."
    echo "  A reboot is required: sudo reboot"
    echo "  After reboot, re-run this script to start containers."
fi

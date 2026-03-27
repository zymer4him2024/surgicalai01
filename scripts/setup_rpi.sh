#!/usr/bin/env bash
# =============================================================================
# setup_rpi.sh — Universal Antigravity RPi Setup
#
# Target:  Raspberry Pi 5 (64-bit Debian Bookworm) + Hailo-8
# Purpose: Prepare a fresh RPi for any Antigravity application.
#          Does NOT require APP_ID — project is assigned from the admin dashboard.
#
# What it does:
#   Phase 1: Pre-checks (OS, disk, internet)
#   Phase 2: Docker CE + Docker Compose
#   Phase 3: Hailo-8 driver (if NPU detected)
#   Phase 4: RPi5 performance optimization
#   Phase 5: Configure .env (DEVICE_ID only)
#   Phase 6: Install antigravity-launcher systemd service
#   Phase 7: Build and start bootstrap stack (firebase_sync only)
#
# After setup:
#   - Device registers in Firestore as app_id=unassigned
#   - Go to admin dashboard → assign this device to a project
#   - The launcher service will automatically start the correct application
#
# Usage:
#   chmod +x scripts/setup_rpi.sh
#   ./scripts/setup_rpi.sh
#
# Re-run safe: each phase checks if already done.
# =============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'
BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[ OK ]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fatal()   { echo -e "${RED}[FATAL]${NC} $*" >&2; exit 1; }

banner() {
    echo ""
    echo -e "${BOLD}${BLUE}================================================================${NC}"
    echo -e "${BOLD}${BLUE}  $*${NC}"
    echo -e "${BOLD}${BLUE}================================================================${NC}"
    echo ""
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
STATE_FILE="${PROJECT_DIR}/.rpi_deploy_state"
CURRENT_HOST="$(hostname)"
CURRENT_USER="${SUDO_USER:-$(whoami)}"

get_completed_phase() {
    if [[ -f "${STATE_FILE}" ]]; then
        local line
        line=$(grep "^${CURRENT_HOST}:" "${STATE_FILE}" 2>/dev/null || echo "")
        [[ -n "${line}" ]] && echo "${line##*:}" && return
    fi
    echo "0"
}

set_completed_phase() {
    grep -v "^${CURRENT_HOST}:" "${STATE_FILE}" > "${STATE_FILE}.tmp" 2>/dev/null || true
    mv "${STATE_FILE}.tmp" "${STATE_FILE}" 2>/dev/null || true
    echo "${CURRENT_HOST}:$1" >> "${STATE_FILE}"
}

LAST_PHASE=$(get_completed_phase)

banner "Antigravity Universal RPi Setup"
echo "  Host:     ${CURRENT_HOST}"
echo "  User:     ${CURRENT_USER}"
echo "  Project:  ${PROJECT_DIR}"
echo "  Last completed phase: ${LAST_PHASE}"
echo ""

[[ $(uname -m) == "aarch64" ]] || fatal "ARM64 (aarch64) only. Is this an RPi5?"
[[ $(id -u) -ne 0 ]] || fatal "Do not run as root. Use a regular user with sudo."

cd "${PROJECT_DIR}"

[[ -f "docker-compose.bootstrap.yml" ]] || fatal "docker-compose.bootstrap.yml not found. Sync the project first."
[[ -f ".env.bootstrap.example" ]]       || fatal ".env.bootstrap.example not found. Sync the project first."

# =========================================================================
# Phase 1: Pre-checks
# =========================================================================
if [[ "${LAST_PHASE}" -lt 1 ]]; then
    banner "Phase 1/7: System Pre-checks"
    ERRORS=0

    info "OS: $(. /etc/os-release && echo "${PRETTY_NAME}")"

    DISK_FREE_GB=$(df -BG / | awk 'NR==2 {gsub("G",""); print $4}')
    [[ "${DISK_FREE_GB:-0}" -ge 5 ]] && success "Disk free: ${DISK_FREE_GB}GB" \
        || { warn "Disk free: ${DISK_FREE_GB}GB (need 5GB+)"; ERRORS=$((ERRORS+1)); }

    MEM_MB=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)
    [[ "${MEM_MB}" -ge 3500 ]] && success "RAM: ${MEM_MB}MB" \
        || { warn "RAM: ${MEM_MB}MB (4GB recommended)"; ERRORS=$((ERRORS+1)); }

    curl -sf --max-time 10 "https://download.docker.com" > /dev/null 2>&1 \
        && success "Internet: connected" \
        || { fatal "No internet connection (required for Docker install)"; }

    [[ "${ERRORS}" -gt 0 ]] && fatal "Pre-checks failed. Fix issues and re-run."
    set_completed_phase 1
    success "Phase 1 complete."
else
    info "Phase 1 (pre-checks): already done."
fi

# =========================================================================
# Phase 2: Docker CE + Docker Compose
# =========================================================================
if [[ "${LAST_PHASE}" -lt 2 ]]; then
    banner "Phase 2/7: Docker CE + Docker Compose"

    if command -v docker &>/dev/null && docker compose version &>/dev/null; then
        DOCKER_VER=$(docker --version | grep -oP '[\d.]+' | head -1)
        success "Docker already installed: ${DOCKER_VER}"
    else
        info "Installing Docker CE..."
        for pkg in docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc; do
            dpkg -l "${pkg}" &>/dev/null 2>&1 && sudo apt-get remove -y "${pkg}" 2>/dev/null || true
        done

        sudo apt-get update -qq
        sudo apt-get install -y ca-certificates curl
        sudo install -m 0755 -d /etc/apt/keyrings
        DOCKER_KEYRING="/etc/apt/keyrings/docker.asc"
        [[ ! -f "${DOCKER_KEYRING}" ]] && sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o "${DOCKER_KEYRING}" && sudo chmod a+r "${DOCKER_KEYRING}"

        ARCH=$(dpkg --print-architecture)
        CODENAME=$(. /etc/os-release && echo "${VERSION_CODENAME}")
        echo "deb [arch=${ARCH} signed-by=${DOCKER_KEYRING}] https://download.docker.com/linux/debian ${CODENAME} stable" \
            | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
        sudo apt-get update -qq
        sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
        success "Docker CE installed."
    fi

    sudo systemctl enable docker
    sudo systemctl start docker

    if ! id -nG "${CURRENT_USER}" | grep -qw "docker"; then
        sudo usermod -aG docker "${CURRENT_USER}"
        success "Added ${CURRENT_USER} to docker group."
    fi

    sudo mkdir -p /etc/docker
    sudo tee /etc/docker/daemon.json > /dev/null << 'EOF'
{
    "log-driver": "json-file",
    "log-opts": { "max-size": "10m", "max-file": "3" },
    "storage-driver": "overlay2",
    "features": { "buildkit": true },
    "default-ulimits": { "nofile": { "name": "nofile", "hard": 65535, "soft": 65535 } },
    "live-restore": true
}
EOF
    sudo systemctl restart docker
    success "Docker daemon configured."

    set_completed_phase 2
    success "Phase 2 complete."
else
    info "Phase 2 (Docker): already done."
fi

# =========================================================================
# Phase 3: Hailo-8 Driver
# =========================================================================
if [[ "${LAST_PHASE}" -lt 3 ]]; then
    banner "Phase 3/7: Hailo-8 Driver"

    if command -v hailortcli &>/dev/null && [[ -c /dev/hailo0 ]]; then
        success "Hailo-8 already installed."
    elif lspci 2>/dev/null | grep -qi "hailo\|1e60"; then
        info "Hailo-8 PCIe device detected. Installing driver..."
        BOOT_CONFIG="/boot/firmware/config.txt"
        grep -q "dtparam=pciex1$\|dtparam=pciex1 " "${BOOT_CONFIG}" 2>/dev/null || {
            echo "" | sudo tee -a "${BOOT_CONFIG}" > /dev/null
            echo "# Hailo-8 PCIe (setup_rpi.sh)" | sudo tee -a "${BOOT_CONFIG}" > /dev/null
            echo "dtparam=pciex1" | sudo tee -a "${BOOT_CONFIG}" > /dev/null
        }
        grep -q "dtparam=pciex1_gen=3" "${BOOT_CONFIG}" 2>/dev/null || \
            echo "dtparam=pciex1_gen=3" | sudo tee -a "${BOOT_CONFIG}" > /dev/null

        sudo apt-get update -qq
        apt-cache show hailo-all &>/dev/null && sudo apt-get install -y hailo-all || warn "hailo-all not found in apt."
        sudo modprobe hailo_pci 2>/dev/null || true
        grep -q "hailo_pci" /etc/modules 2>/dev/null || echo "hailo_pci" | sudo tee -a /etc/modules > /dev/null

        getent group hailo &>/dev/null && sudo usermod -aG hailo "${CURRENT_USER}" 2>/dev/null || true
        UDEV_FILE="/etc/udev/rules.d/99-hailo.rules"
        [[ ! -f "${UDEV_FILE}" ]] && {
            echo 'SUBSYSTEM=="hailo_chardev", KERNEL=="hailo*", GROUP="hailo", MODE="0660"' | sudo tee "${UDEV_FILE}" > /dev/null
            sudo udevadm control --reload-rules && sudo udevadm trigger
        }
        [[ -c /dev/hailo0 ]] && success "Hailo-8 ready: /dev/hailo0" || warn "/dev/hailo0 not available — reboot may be needed."
    else
        warn "No Hailo-8 PCIe device detected. Skipping."
    fi

    set_completed_phase 3
    success "Phase 3 complete."
else
    info "Phase 3 (Hailo-8): already done."
fi

# =========================================================================
# Phase 4: RPi5 Performance Optimization
# =========================================================================
if [[ "${LAST_PHASE}" -lt 4 ]]; then
    banner "Phase 4/7: RPi5 Performance Optimization"

    GOVERNORS=$(ls /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null || true)
    if [[ -n "${GOVERNORS}" ]]; then
        for gov in ${GOVERNORS}; do echo "performance" | sudo tee "${gov}" > /dev/null 2>&1 || true; done
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
        sudo systemctl daemon-reload && sudo systemctl enable cpu-performance.service
        success "CPU governor: performance."
    fi

    sudo tee /etc/sysctl.d/99-antigravity-rpi.conf > /dev/null << 'EOF'
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
    sudo sysctl -p /etc/sysctl.d/99-antigravity-rpi.conf 2>/dev/null || true

    sudo tee /etc/security/limits.d/99-antigravity-rpi.conf > /dev/null << 'EOF'
*    soft nofile 65535
*    hard nofile 65535
root soft nofile 65535
root hard nofile 65535
EOF
    success "Kernel parameters and file limits set."

    set_completed_phase 4
    success "Phase 4 complete."
else
    info "Phase 4 (optimization): already done."
fi

# =========================================================================
# Phase 5: Configure .env
# =========================================================================
if [[ "${LAST_PHASE}" -lt 5 ]]; then
    banner "Phase 5/7: Configure .env"

    if [[ -f ".env" ]]; then
        info "Existing .env found. Keeping it."
        DEVICE_ID_DISPLAY=$(grep "^DEVICE_ID=" .env 2>/dev/null | cut -d= -f2 || echo "unknown")
        info "Current DEVICE_ID: ${DEVICE_ID_DISPLAY}"
    else
        cp .env.bootstrap.example .env
        success "Copied .env.bootstrap.example → .env"

        DEFAULT_ID="US-RPi-$(printf '%03d' $((RANDOM % 100 + 1)))"
        read -rp "DEVICE_ID [${DEFAULT_ID}]: " INPUT_ID
        DEVICE_ID="${INPUT_ID:-${DEFAULT_ID}}"
        sed -i "s|^DEVICE_ID=.*|DEVICE_ID=${DEVICE_ID}|" .env
        success "DEVICE_ID=${DEVICE_ID}"
    fi

    mkdir -p data

    if [[ -f "firebase-credentials.json" ]]; then
        success "firebase-credentials.json found."
    else
        warn "firebase-credentials.json not found."
        info "Copy the Firebase service account key to: ${PROJECT_DIR}/firebase-credentials.json"
        info "Without it, the device will run in simulation mode."
    fi

    set_completed_phase 5
    success "Phase 5 complete."
else
    info "Phase 5 (.env): already done."
fi

# =========================================================================
# Phase 6: Install antigravity-launcher systemd service
# =========================================================================
if [[ "${LAST_PHASE}" -lt 6 ]]; then
    banner "Phase 6/7: Install Launcher Service"

    SERVICE_SRC="${SCRIPT_DIR}/antigravity-launcher.service"
    SERVICE_DEST="/etc/systemd/system/antigravity-launcher.service"

    if [[ ! -f "${SERVICE_SRC}" ]]; then
        fatal "antigravity-launcher.service not found at ${SERVICE_SRC}"
    fi

    chmod +x "${SCRIPT_DIR}/launcher.sh"

    # Fill in placeholders
    sed \
        -e "s|ANTIGRAVITY_USER|${CURRENT_USER}|g" \
        -e "s|ANTIGRAVITY_PROJECT_DIR|${PROJECT_DIR}|g" \
        "${SERVICE_SRC}" | sudo tee "${SERVICE_DEST}" > /dev/null

    sudo systemctl daemon-reload
    sudo systemctl enable antigravity-launcher
    success "antigravity-launcher.service installed and enabled."

    set_completed_phase 6
    success "Phase 6 complete."
else
    info "Phase 6 (launcher service): already done."
fi

# =========================================================================
# Phase 7: Build and start bootstrap stack
# =========================================================================
if [[ "${LAST_PHASE}" -lt 7 ]]; then
    banner "Phase 7/7: Build and Start Bootstrap Stack"

    # Ensure docker group is active
    if ! docker info &>/dev/null 2>&1; then
        info "Re-launching under docker group..."
        exec sg docker -c "bash ${BASH_SOURCE[0]}"
    fi

    DISPLAY=:0 xhost +local: 2>/dev/null || true

    info "Building bootstrap containers..."
    docker compose -f docker-compose.bootstrap.yml up -d --build

    echo "docker-compose.bootstrap.yml" > "${PROJECT_DIR}/.current_compose"

    sleep 5
    if curl -sf --max-time 5 "http://localhost:8004/health" > /dev/null 2>&1; then
        success "Firebase sync agent: healthy"
    else
        warn "Firebase sync not responding yet — check: docker compose -f docker-compose.bootstrap.yml logs"
    fi

    # Start launcher service
    sudo systemctl start antigravity-launcher
    success "Launcher service started."

    set_completed_phase 7
    success "Phase 7 complete."
else
    info "Phase 7 (bootstrap): already done."
fi

# =========================================================================
# Done
# =========================================================================
banner "Antigravity RPi Setup Complete"

DEVICE_ID_DISPLAY=$(grep "^DEVICE_ID=" .env 2>/dev/null | cut -d= -f2 || echo "unknown")
echo "  DEVICE_ID:  ${DEVICE_ID_DISPLAY}"
echo "  Status:     Registered in Firestore as app_id=unassigned"
echo ""
echo "  Next step:"
echo "    1. Go to the admin dashboard"
echo "    2. Find this device under Devices: ${DEVICE_ID_DISPLAY}"
echo "    3. Create or edit a project and assign this device"
echo "    4. The launcher will automatically start the correct application (~10s)"
echo ""
echo "  Monitor launcher:"
echo "    journalctl -u antigravity-launcher -f"
echo ""
echo "  Current container status:"
docker compose -f docker-compose.bootstrap.yml ps 2>/dev/null || true

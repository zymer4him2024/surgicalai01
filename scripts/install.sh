#!/usr/bin/env bash
# =============================================================================
# install.sh — SurgicalAI One-Click Bootstrap Installer
#
# This is a SELF-CONTAINED script. Download this single file to a fresh
# Raspberry Pi 5 and run it. It will:
#   1. Clone the SurgicalAI repository
#   2. Install the Hailo-8 AI Accelerator driver
#   3. Install Docker CE & Compose
#   4. Optimize RPi5 for inference performance
#   5. Verify the system
#   6. Launch all containers
#
# Usage (copy-paste this entire block into your RPi terminal):
#
#   curl -fsSL https://raw.githubusercontent.com/zymer4him2024/surgicalai01/main/scripts/install.sh -o install.sh
#   chmod +x install.sh
#   ./install.sh
#
# The script saves progress to a state file and survives reboots.
# After each reboot, just run ./install.sh again from the same location.
# =============================================================================

set -euo pipefail

# ── Colors & Helpers ─────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[ OK ]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()     { echo -e "${RED}[FAIL]${NC} $*" >&2; exit 1; }
banner()  { echo -e "\n${BOLD}${BLUE}══════════════════════════════════════════${NC}"; \
            echo -e "${BOLD}  $*${NC}"; \
            echo -e "${BOLD}${BLUE}══════════════════════════════════════════${NC}\n"; }

# ── Constants ────────────────────────────────────────────────────────────────
REPO_URL="https://github.com/zymer4him2024/surgicalai01.git"
INSTALL_DIR="${HOME}/SurgicalAI01"
STATE_FILE="${HOME}/.surgicalai_install_state"
BOOT_CONFIG="/boot/firmware/config.txt"
CMDLINE_FILE="/boot/firmware/cmdline.txt"
SYSCTL_FILE="/etc/sysctl.d/99-surgicalai.conf"
CURRENT_USER="${SUDO_USER:-$(whoami)}"

# ── State Machine ────────────────────────────────────────────────────────────
get_phase() { [[ -f "${STATE_FILE}" ]] && cat "${STATE_FILE}" || echo "0"; }
set_phase() { echo "$1" > "${STATE_FILE}"; info "Progress saved → Phase $1"; }
CURRENT_PHASE=$(get_phase)

banner "SurgicalAI One-Click Installer"
echo "  Install Dir: ${INSTALL_DIR}"
echo "  Phase:       ${CURRENT_PHASE} of 6"
echo "  Date:        $(date)"
echo ""

# ── Guard: Must be ARM64 and non-root ────────────────────────────────────────
[[ "$(uname -m)" == "aarch64" ]] || err "ARM64 (Raspberry Pi 5) required. Detected: $(uname -m)"
[[ "$(id -u)" -ne 0 ]]          || err "Do not run as root. Use a regular user with sudo privileges."

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 0: Clone Repository
# ═════════════════════════════════════════════════════════════════════════════
if [[ "${CURRENT_PHASE}" -le 0 ]]; then
    banner "Phase 1/6 — Downloading SurgicalAI Repository"

    if [[ -d "${INSTALL_DIR}/.git" ]]; then
        success "Repository already exists at ${INSTALL_DIR}. Pulling latest..."
        cd "${INSTALL_DIR}" && git pull || warn "Git pull failed, using existing code."
    else
        info "Cloning repository..."
        git clone "${REPO_URL}" "${INSTALL_DIR}"
        success "Repository cloned to ${INSTALL_DIR}"
    fi

    set_phase 1
fi

cd "${INSTALL_DIR}"

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 1: Hailo-8 Driver Installation
# ═════════════════════════════════════════════════════════════════════════════
if [[ "${CURRENT_PHASE}" -le 1 ]]; then
    banner "Phase 2/6 — Hailo-8 AI Accelerator Driver"

    if command -v hailortcli &>/dev/null && [[ -c /dev/hailo0 ]]; then
        success "Hailo-8 already installed and /dev/hailo0 present. Skipping."
    else
        info "Installing Hailo-8 drivers..."

        # ── PCIe config ──
        if ! grep -q "dtparam=pciex1$\|dtparam=pciex1 " "${BOOT_CONFIG}" 2>/dev/null; then
            echo "" | sudo tee -a "${BOOT_CONFIG}" > /dev/null
            echo "# Hailo-8 PCIe (added by install.sh)" | sudo tee -a "${BOOT_CONFIG}" > /dev/null
            echo "dtparam=pciex1" | sudo tee -a "${BOOT_CONFIG}" > /dev/null
        fi
        if ! grep -q "dtparam=pciex1_gen=3" "${BOOT_CONFIG}" 2>/dev/null; then
            echo "dtparam=pciex1_gen=3" | sudo tee -a "${BOOT_CONFIG}" > /dev/null
        fi
        success "PCIe Gen3 configured"

        # ── Install hailo-all ──
        sudo apt-get update -qq
        if apt-cache show hailo-all &>/dev/null; then
            info "Installing hailo-all from RPi official repo..."
        else
            info "Adding Hailo apt repository..."
            HAILO_KEYRING="/usr/share/keyrings/hailo-keyring.gpg"
            if [[ ! -f "${HAILO_KEYRING}" ]]; then
                curl -fsSL https://hailo.ai/apt/hailo-keyring.gpg | sudo gpg --dearmor -o "${HAILO_KEYRING}"
            fi
            CODENAME=$(. /etc/os-release && echo "${VERSION_CODENAME:-bookworm}")
            echo "deb [arch=arm64 signed-by=${HAILO_KEYRING}] https://hailo.ai/apt/ubuntu ${CODENAME} main" \
                | sudo tee /etc/apt/sources.list.d/hailo.list > /dev/null
            sudo apt-get update -qq
        fi
        sudo apt-get install -y hailo-all
        success "hailo-all installed"

        # ── Kernel module ──
        sudo modprobe hailo_pci 2>/dev/null || true
        grep -q "hailo_pci" /etc/modules 2>/dev/null || echo "hailo_pci" | sudo tee -a /etc/modules > /dev/null

        # ── User group & udev ──
        getent group hailo &>/dev/null && sudo usermod -aG hailo "${CURRENT_USER}" 2>/dev/null || true
        if [[ ! -f /etc/udev/rules.d/99-hailo.rules ]]; then
            echo 'SUBSYSTEM=="hailo_chardev", KERNEL=="hailo*", GROUP="hailo", MODE="0660"' \
                | sudo tee /etc/udev/rules.d/99-hailo.rules > /dev/null
            sudo udevadm control --reload-rules && sudo udevadm trigger
        fi
        success "Hailo-8 driver installation complete"
    fi

    set_phase 2

    if [[ ! -c /dev/hailo0 ]]; then
        warn "═══════════════════════════════════════════════════════════"
        warn "  REBOOT REQUIRED to load the Hailo-8 kernel module."
        warn "  After reboot, run this script again:"
        warn ""
        warn "    cd ${INSTALL_DIR} && ./scripts/install.sh"
        warn "═══════════════════════════════════════════════════════════"
        read -rp "  Reboot now? [Y/n] " REPLY
        [[ "${REPLY,,}" == "n" ]] || sudo reboot
        exit 0
    fi
fi

cd "${INSTALL_DIR}"

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 2: Docker CE + Compose
# ═════════════════════════════════════════════════════════════════════════════
if [[ "${CURRENT_PHASE}" -le 2 ]]; then
    banner "Phase 3/6 — Docker CE & Compose"

    if command -v docker &>/dev/null && docker compose version &>/dev/null; then
        success "Docker already installed. Skipping."
    else
        info "Installing Docker CE..."

        # Remove unofficial packages
        for pkg in docker.io docker-doc docker-compose podman-docker containerd runc; do
            dpkg -l "${pkg}" &>/dev/null 2>&1 && sudo apt-get remove -y "${pkg}" 2>/dev/null || true
        done

        # Add Docker official repo
        sudo apt-get update -qq
        sudo apt-get install -y ca-certificates curl
        sudo install -m 0755 -d /etc/apt/keyrings
        DOCKER_KEYRING="/etc/apt/keyrings/docker.asc"
        if [[ ! -f "${DOCKER_KEYRING}" ]]; then
            sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o "${DOCKER_KEYRING}"
            sudo chmod a+r "${DOCKER_KEYRING}"
        fi
        ARCH=$(dpkg --print-architecture)
        CODENAME=$(. /etc/os-release && echo "${VERSION_CODENAME}")
        echo "deb [arch=${ARCH} signed-by=${DOCKER_KEYRING}] https://download.docker.com/linux/debian ${CODENAME} stable" \
            | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
        sudo apt-get update -qq

        # Install
        sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
        sudo systemctl enable docker && sudo systemctl start docker
        success "Docker CE installed"

        # User group
        id -nG "${CURRENT_USER}" | grep -qw "docker" || sudo usermod -aG docker "${CURRENT_USER}"

        # Daemon optimization
        sudo mkdir -p /etc/docker
        sudo tee /etc/docker/daemon.json > /dev/null << 'DJEOF'
{
    "log-driver": "json-file",
    "log-opts": { "max-size": "10m", "max-file": "3" },
    "storage-driver": "overlay2",
    "features": { "buildkit": true },
    "default-ulimits": { "nofile": { "name": "nofile", "hard": 65535, "soft": 65535 } },
    "live-restore": true
}
DJEOF
        sudo systemctl restart docker
        success "Docker daemon optimized"
    fi

    set_phase 3
fi

cd "${INSTALL_DIR}"

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 3: RPi5 Performance Optimization
# ═════════════════════════════════════════════════════════════════════════════
if [[ "${CURRENT_PHASE}" -le 3 ]]; then
    banner "Phase 4/6 — RPi5 Performance Optimization"

    GOV=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo "unknown")
    CMDLINE=$(cat "${CMDLINE_FILE}" 2>/dev/null || echo "")

    if [[ "${GOV}" == "performance" ]] && echo "${CMDLINE}" | grep -q "pcie_aspm=off"; then
        success "System already optimized. Skipping."
    else
        info "Applying performance tuning..."

        # CPU Governor → performance
        for gov in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
            echo "performance" | sudo tee "${gov}" > /dev/null 2>/dev/null || true
        done
        sudo tee /etc/systemd/system/cpu-performance.service > /dev/null << 'CPUEOF'
[Unit]
Description=CPU Governor Performance for Hailo-8
After=multi-user.target
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance > $f; done'
[Install]
WantedBy=multi-user.target
CPUEOF
        sudo systemctl daemon-reload && sudo systemctl enable cpu-performance.service
        success "CPU Governor → performance"

        # Boot config
        sudo cp "${BOOT_CONFIG}" "${BOOT_CONFIG}.bak.$(date +%s)" 2>/dev/null || true
        for kv in "arm_boost=1" "over_voltage_delta=50000" "gpu_mem=64"; do
            key="${kv%%=*}"; val="${kv#*=}"
            grep -q "^${key}=" "${BOOT_CONFIG}" && sudo sed -i "s|^${key}=.*|${key}=${val}|" "${BOOT_CONFIG}" \
                || echo "${key}=${val}" | sudo tee -a "${BOOT_CONFIG}" > /dev/null
        done
        success "Boot config tuned (turbo, GPU mem, voltage)"

        # cmdline.txt — PCIe ASPM off + USB autosuspend
        sudo cp "${CMDLINE_FILE}" "${CMDLINE_FILE}.bak.$(date +%s)" 2>/dev/null || true
        CMDLINE=$(cat "${CMDLINE_FILE}")
        echo "${CMDLINE}" | grep -q "pcie_aspm=off"         || CMDLINE="${CMDLINE} pcie_aspm=off"
        echo "${CMDLINE}" | grep -q "usbcore.autosuspend=-1" || CMDLINE="${CMDLINE} usbcore.autosuspend=-1"
        echo "${CMDLINE}" | sudo tee "${CMDLINE_FILE}" > /dev/null
        success "Kernel cmdline tuned (PCIe ASPM off, USB stable)"

        # sysctl
        sudo tee "${SYSCTL_FILE}" > /dev/null << 'SYSEOF'
vm.swappiness = 5
vm.dirty_ratio = 10
vm.dirty_background_ratio = 5
fs.file-max = 131072
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.core.somaxconn = 1024
net.ipv4.tcp_tw_reuse = 1
net.ipv4.tcp_fin_timeout = 15
kernel.shmmax = 536870912
kernel.shmall = 131072
SYSEOF
        sudo sysctl -p "${SYSCTL_FILE}" 2>/dev/null || true
        success "Kernel parameters tuned"

        # File descriptor limits
        sudo tee /etc/security/limits.d/99-surgicalai.conf > /dev/null << 'LIMEOF'
*    soft nofile 65535
*    hard nofile 65535
root soft nofile 65535
root hard nofile 65535
LIMEOF

        # PCIe power management
        sudo tee /etc/udev/rules.d/99-pcie-performance.rules > /dev/null << 'PCIEOF'
ACTION=="add", SUBSYSTEM=="pci", ATTR{power/control}="on"
PCIEOF
        sudo udevadm control --reload-rules
        success "PCIe power management disabled (no sleep)"
    fi

    set_phase 4

    # Reboot needed for kernel params
    CMDLINE_NOW=$(cat "${CMDLINE_FILE}" 2>/dev/null || echo "")
    GOV_NOW=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo "unknown")
    if [[ "${GOV_NOW}" != "performance" ]] || ! echo "${CMDLINE_NOW}" | grep -q "pcie_aspm=off"; then
        warn "═══════════════════════════════════════════════════════════"
        warn "  REBOOT REQUIRED for kernel parameter changes."
        warn "  After reboot, run this script again:"
        warn ""
        warn "    cd ${INSTALL_DIR} && ./scripts/install.sh"
        warn "═══════════════════════════════════════════════════════════"
        read -rp "  Reboot now? [Y/n] " REPLY
        [[ "${REPLY,,}" == "n" ]] || sudo reboot
        exit 0
    fi
fi

cd "${INSTALL_DIR}"

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 4: System Verification
# ═════════════════════════════════════════════════════════════════════════════
if [[ "${CURRENT_PHASE}" -le 4 ]]; then
    banner "Phase 5/6 — System Verification"

    FAIL=0
    echo "  Checking hardware..."
    [[ -c /dev/hailo0 ]]              && success "/dev/hailo0 exists"            || { warn "/dev/hailo0 missing"; FAIL=1; }
    command -v hailortcli &>/dev/null  && success "hailortcli installed"          || { warn "hailortcli missing"; FAIL=1; }
    command -v docker &>/dev/null      && success "Docker installed"              || { warn "Docker missing";     FAIL=1; }
    docker compose version &>/dev/null && success "Docker Compose installed"      || { warn "Compose missing";    FAIL=1; }

    GOV=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo "?")
    success "CPU Governor: ${GOV}"

    if [[ "${FAIL}" -eq 0 ]]; then
        success "All checks passed!"
    else
        warn "Some checks failed. The system may still work, but review warnings above."
    fi

    set_phase 5
fi

cd "${INSTALL_DIR}"

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 5: Environment Setup & Launch
# ═════════════════════════════════════════════════════════════════════════════
if [[ "${CURRENT_PHASE}" -le 5 ]]; then
    banner "Phase 6/6 — Environment & Launch"

    # .env
    if [[ ! -f ".env" ]]; then
        info "Creating default .env file..."
        cat > .env << 'ENVEOF'
# Default Environment Variables
APP_ID=surgical
DEVICE_ID=rpi-01
FIREBASE_CREDENTIALS_PATH=/app/firebase-credentials.json
ENVEOF
        success "Created default .env"
    else
        success ".env exists"
    fi

    # Firebase Credentials Placeholder
    if [[ ! -f "firebase-credentials.json" ]]; then
        echo "{}" > firebase-credentials.json
        success "Created firebase-credentials.json placeholder"
    fi

    # data directory
    mkdir -p data

    # HDMI display access
    DISPLAY=:0 xhost +local: 2>/dev/null || true

    # Desktop shortcut
    DESKTOP_DIR="${HOME}/Desktop"
    mkdir -p "${DESKTOP_DIR}"
    cat > "${DESKTOP_DIR}/SurgicalAI.desktop" << DEOF
[Desktop Entry]
Name=SurgicalAI
Comment=Start Surgical AI System
Exec=bash -c "DISPLAY=:0 xhost +local: && cd ${INSTALL_DIR} && docker compose up -d"
Icon=utilities-terminal
Terminal=true
Type=Application
DEOF
    chmod +x "${DESKTOP_DIR}/SurgicalAI.desktop"
    success "Desktop shortcut created"

    # Build and launch
    info "Building and launching all containers (this may take a few minutes)..."
    sudo docker compose up -d --build
    success "All containers launched!"

    # Cleanup
    rm -f "${STATE_FILE}"

    echo ""
    banner "Installation Complete!"
    echo "  ┌──────────────────────────────────────────────────────────┐"
    echo "  │  All 6 phases completed successfully!                    │"
    echo "  │                                                          │"
    echo "  │  Gateway:   http://localhost:8000/health                  │"
    echo "  │  Dashboard: https://surgicalai01.web.app/admin           │"
    echo "  │  Desktop:   Double-click ~/Desktop/SurgicalAI.desktop    │"
    echo "  │                                                          │"
    echo "  │  Verify:    docker compose ps                            │"
    echo "  │  Logs:      docker compose logs -f gateway_agent         │"
    echo "  └──────────────────────────────────────────────────────────┘"
    echo ""
fi

#!/usr/bin/env bash
# =============================================================================
# install.sh — SurgicalAI One-Click Installer
#
# Consolidates all setup steps into a single script with automatic
# reboot handling via a state file. After each reboot, re-run this
# script and it will resume from where it left off.
#
# Usage:
#   chmod +x scripts/install.sh
#   ./scripts/install.sh
#
# The script progresses through 5 phases:
#   Phase 1: Hailo-8 driver installation  → reboot required
#   Phase 2: Docker CE installation       → no reboot (re-login)
#   Phase 3: RPi5 performance tuning      → reboot required
#   Phase 4: System verification          → no reboot
#   Phase 5: Launch containers            → done
# =============================================================================

set -euo pipefail

# ── Colors & Helpers ─────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[ OK ]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[FAIL]${NC} $*" >&2; exit 1; }
banner()  { echo -e "\n${BOLD}${BLUE}══════════════════════════════════════════${NC}"; \
            echo -e "${BOLD}  $*${NC}"; \
            echo -e "${BOLD}${BLUE}══════════════════════════════════════════${NC}\n"; }

# ── Resolve project root ─────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
STATE_FILE="${PROJECT_DIR}/.install_state"
cd "${PROJECT_DIR}"

# ── State Machine ────────────────────────────────────────────────────────────
get_phase() {
    if [[ -f "${STATE_FILE}" ]]; then
        cat "${STATE_FILE}"
    else
        echo "1"
    fi
}

set_phase() {
    echo "$1" > "${STATE_FILE}"
    info "Progress saved → Phase $1"
}

CURRENT_PHASE=$(get_phase)

banner "SurgicalAI One-Click Installer"
echo "  Project:  ${PROJECT_DIR}"
echo "  Phase:    ${CURRENT_PHASE} of 5"
echo "  Date:     $(date)"
echo ""

# ── Guard: Must be ARM64 and non-root ────────────────────────────────────────
if [[ "$(uname -m)" != "aarch64" ]]; then
    error "This installer is designed for ARM64 (Raspberry Pi 5). Detected: $(uname -m)"
fi
if [[ "$(id -u)" -eq 0 ]]; then
    error "Do not run as root. Use a regular user with sudo privileges."
fi

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 1: Hailo-8 Driver Installation
# ═════════════════════════════════════════════════════════════════════════════
if [[ "${CURRENT_PHASE}" -le 1 ]]; then
    banner "Phase 1/5 — Hailo-8 AI Accelerator Driver"

    # Skip if already installed
    if command -v hailortcli &>/dev/null && [[ -c /dev/hailo0 ]]; then
        success "Hailo-8 already installed and /dev/hailo0 present. Skipping."
    else
        info "Running Hailo-8 setup..."
        bash "${SCRIPT_DIR}/setup_hailo.sh"
    fi

    set_phase 2

    # Check if reboot is needed (no /dev/hailo0 yet)
    if [[ ! -c /dev/hailo0 ]]; then
        echo ""
        warn "══════════════════════════════════════════════════════════"
        warn "  REBOOT REQUIRED to load the Hailo-8 kernel module."
        warn "  After reboot, run this script again:"
        warn ""
        warn "    ./scripts/install.sh"
        warn "══════════════════════════════════════════════════════════"
        echo ""
        read -rp "  Reboot now? [Y/n] " REPLY
        if [[ "${REPLY,,}" != "n" ]]; then
            sudo reboot
        fi
        exit 0
    fi
fi

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 2: Docker CE + Compose Installation
# ═════════════════════════════════════════════════════════════════════════════
if [[ "${CURRENT_PHASE}" -le 2 ]]; then
    banner "Phase 2/5 — Docker CE & Compose"

    if command -v docker &>/dev/null && docker compose version &>/dev/null; then
        success "Docker already installed. Skipping."
    else
        info "Running Docker setup..."
        bash "${SCRIPT_DIR}/setup_docker.sh"
    fi

    set_phase 3

    # Docker group requires re-login, but we can continue with sudo
    info "Docker group applied. Continuing with sudo for remaining steps."
fi

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 3: RPi5 Performance Optimization
# ═════════════════════════════════════════════════════════════════════════════
if [[ "${CURRENT_PHASE}" -le 3 ]]; then
    banner "Phase 3/5 — RPi5 Performance Optimization"

    # Check if already optimized
    GOV=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo "unknown")
    CMDLINE=$(cat /boot/firmware/cmdline.txt 2>/dev/null || echo "")

    if [[ "${GOV}" == "performance" ]] && echo "${CMDLINE}" | grep -q "pcie_aspm=off"; then
        success "System already optimized. Skipping."
    else
        info "Running RPi5 optimization..."
        bash "${SCRIPT_DIR}/optimize_rpi5.sh"
    fi

    set_phase 4

    # Check if reboot is needed for kernel params
    if ! echo "${CMDLINE}" | grep -q "pcie_aspm=off"; then
        echo ""
        warn "══════════════════════════════════════════════════════════"
        warn "  REBOOT REQUIRED for kernel parameter changes."
        warn "  After reboot, run this script again:"
        warn ""
        warn "    ./scripts/install.sh"
        warn "══════════════════════════════════════════════════════════"
        echo ""
        read -rp "  Reboot now? [Y/n] " REPLY
        if [[ "${REPLY,,}" != "n" ]]; then
            sudo reboot
        fi
        exit 0
    fi
fi

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 4: System Verification
# ═════════════════════════════════════════════════════════════════════════════
if [[ "${CURRENT_PHASE}" -le 4 ]]; then
    banner "Phase 4/5 — System Verification"

    bash "${SCRIPT_DIR}/check_system.sh" || true

    set_phase 5
fi

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 5: Environment Setup & Launch
# ═════════════════════════════════════════════════════════════════════════════
if [[ "${CURRENT_PHASE}" -le 5 ]]; then
    banner "Phase 5/5 — Environment Configuration & Launch"

    # Create .env if missing
    if [[ ! -f "${PROJECT_DIR}/.env" ]]; then
        if [[ -f "${PROJECT_DIR}/.env.example" ]]; then
            cp "${PROJECT_DIR}/.env.example" "${PROJECT_DIR}/.env"
            success "Created .env from template"
            warn "Edit .env to configure APP_ID, DEVICE_ID, and Firebase credentials."
        else
            warn "No .env.example found. Create .env manually before launching."
        fi
    else
        success ".env already exists"
    fi

    # Create data directory
    mkdir -p "${PROJECT_DIR}/data"

    # Setup HDMI display access
    DISPLAY=:0 xhost +local: 2>/dev/null || true

    # Create desktop shortcut
    DESKTOP_DIR="${HOME}/Desktop"
    mkdir -p "${DESKTOP_DIR}"
    cat > "${DESKTOP_DIR}/SurgicalAI.desktop" << DEOF
[Desktop Entry]
Name=SurgicalAI
Comment=Start Surgical AI System
Exec=bash -c "DISPLAY=:0 xhost +local: && cd ${PROJECT_DIR} && docker compose up -d"
Icon=utilities-terminal
Terminal=true
Type=Application
Categories=Application;
DEOF
    chmod +x "${DESKTOP_DIR}/SurgicalAI.desktop"
    success "Desktop shortcut created: ~/Desktop/SurgicalAI.desktop"

    # Build and launch
    info "Building and launching containers..."
    cd "${PROJECT_DIR}"
    sudo docker compose up -d --build

    # Cleanup state file
    rm -f "${STATE_FILE}"
    success "Installation state file cleaned up"

    echo ""
    banner "Installation Complete!"
    echo "  ┌──────────────────────────────────────────────────────────┐"
    echo "  │  All 5 phases completed successfully.                    │"
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

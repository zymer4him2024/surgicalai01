#!/usr/bin/env bash
# =============================================================================
#  one click HW deployment.sh
#  Antigravity SurgicalAI01 — Full Hardware Deployment for Raspberry Pi 5
#
#  Run once on a blank RPi OS (Bookworm 64-bit). The script resumes
#  automatically after each required reboot via a phase-state file.
#
#  Usage (first time and after each reboot):
#    chmod +x "one click HW deployment.sh"
#    ./"one click HW deployment.sh"
#
#  Phases:
#    1 — Hailo-8 driver + PCIe setup          (reboot required)
#    2 — Docker CE + Compose installation
#    3 — RPi5 performance optimisation        (reboot required)
#    4 — Environment & credentials setup
#    5 — System verification
#    6 — Build containers + launch + Desktop shortcut
# =============================================================================

set -euo pipefail

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()      { echo -e "${GREEN}[ OK ]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail()    { echo -e "${RED}[FAIL]${NC}  $*" >&2; exit 1; }
step()    { echo -e "\n${BOLD}${CYAN}━━━  $*  ━━━${NC}\n"; }
banner() {
    echo -e "\n${BOLD}${BLUE}"
    echo "  ╔══════════════════════════════════════════════════════════╗"
    printf  "  ║  %-56s  ║\n" "$*"
    echo "  ╚══════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

# ── Paths ─────────────────────────────────────────────────────────────────────
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="${PROJECT_DIR}/scripts"
STATE_FILE="${PROJECT_DIR}/.deploy_state"
ENV_FILE="${PROJECT_DIR}/.env"
CREDS_FILE="${PROJECT_DIR}/firebase-credentials.json"

cd "${PROJECT_DIR}"

# ── State machine ─────────────────────────────────────────────────────────────
# State file format: "<phase>:<hostname>"
# If the hostname in the state file doesn't match the current host, the state
# was copied from another machine — reset to Phase 1.
get_phase() {
    if [[ ! -f "${STATE_FILE}" ]]; then echo "1"; return; fi
    local saved
    saved=$(cat "${STATE_FILE}")
    local saved_phase saved_host
    saved_phase=$(echo "${saved}" | cut -d: -f1)
    saved_host=$(echo "${saved}" | cut -d: -f2)
    if [[ "${saved_host}" != "$(hostname)" ]]; then
        warn "State file was created on '${saved_host}', current host is '$(hostname)'."
        warn "Resetting installation state to Phase 1 for this machine."
        rm -f "${STATE_FILE}"
        echo "1"
    else
        echo "${saved_phase}"
    fi
}
set_phase() { echo "$1:$(hostname)" > "${STATE_FILE}"; info "Progress saved — resuming from Phase $1 after next run."; }

PHASE=$(get_phase)

# ── Pre-flight guards ─────────────────────────────────────────────────────────
banner "SurgicalAI One-Click Hardware Deployment"
echo "  Project : ${PROJECT_DIR}"
echo "  Phase   : ${PHASE} / 6"
echo "  Host    : $(hostname)  |  $(date)"
echo ""

[[ "$(uname -m)" == "aarch64" ]] || fail "ARM64 (Raspberry Pi 5) required. Detected: $(uname -m)"
[[ "$(id -u)" -ne 0 ]]           || fail "Do not run as root. Use a regular user with sudo."

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 1 — Hailo-8 AI Accelerator Driver
# ═════════════════════════════════════════════════════════════════════════════
if [[ "${PHASE}" -le 1 ]]; then
    step "Phase 1 / 6 — Hailo-8 Driver & PCIe Configuration"

    if command -v hailortcli &>/dev/null && [[ -c /dev/hailo0 ]]; then
        ok "Hailo-8 already installed (/dev/hailo0 present). Skipping."
    else
        info "Installing Hailo-8 driver (hailo-all)..."
        bash "${SCRIPTS_DIR}/setup_hailo.sh"

        # After install, try to load the kernel module immediately without a reboot.
        # This succeeds on many systems and avoids an unnecessary reboot cycle.
        info "Attempting to load hailo_pci kernel module now..."
        if sudo modprobe hailo_pci 2>/dev/null; then
            sleep 2   # give udev a moment to create /dev/hailo0
            if [[ -c /dev/hailo0 ]]; then
                ok "hailo_pci module loaded and /dev/hailo0 created — no reboot needed."
            fi
        fi

        # Add current user to the hailo group
        CURRENT_USER="${SUDO_USER:-$(whoami)}"
        if getent group hailo &>/dev/null; then
            if ! id -nG "${CURRENT_USER}" | grep -qw "hailo"; then
                sudo usermod -aG hailo "${CURRENT_USER}"
                ok "User '${CURRENT_USER}' added to hailo group."
            fi
        fi
    fi

    set_phase 2

    # Only reboot if /dev/hailo0 still does not exist after modprobe attempt
    if [[ ! -c /dev/hailo0 ]]; then
        echo ""
        warn "═══════════════════════════════════════════════════════"
        warn "  REBOOT REQUIRED — Hailo-8 kernel module must load."
        warn "  After reboot, run this script again:"
        warn ""
        warn "    ./'one click HW deployment.sh'"
        warn "═══════════════════════════════════════════════════════"
        echo ""
        read -rp "  Reboot now? [Y/n] " _reply
        [[ "${_reply,,}" != "n" ]] && sudo reboot
        exit 0
    fi
fi

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 2 — Docker CE + Compose
# ═════════════════════════════════════════════════════════════════════════════
if [[ "${PHASE}" -le 2 ]]; then
    step "Phase 2 / 6 — Docker CE & Docker Compose"

    if command -v docker &>/dev/null && docker compose version &>/dev/null 2>&1; then
        ok "Docker already installed ($(docker --version | grep -oP '[\d.]+' | head -1)). Skipping."
    else
        info "Installing Docker CE..."
        bash "${SCRIPTS_DIR}/setup_docker.sh"
    fi

    set_phase 3

    # Activate docker group in the current session without requiring a full re-login.
    # sg runs the remainder of this script under the docker group context.
    if ! id -nG | grep -qw "docker"; then
        info "Re-launching script under docker group context (no re-login needed)..."
        exec sg docker -c "bash \"${PROJECT_DIR}/one click HW deployment.sh\""
    fi

    ok "Docker group is active in this session."
fi

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 3 — RPi5 Performance Optimisation
# ═════════════════════════════════════════════════════════════════════════════
if [[ "${PHASE}" -le 3 ]]; then
    step "Phase 3 / 6 — RPi5 Performance Optimisation"

    GOV=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo "unknown")
    CMDLINE=$(cat /boot/firmware/cmdline.txt 2>/dev/null || echo "")

    if [[ "${GOV}" == "performance" ]] && echo "${CMDLINE}" | grep -q "pcie_aspm=off"; then
        ok "System already optimised. Skipping."
    else
        info "Applying RPi5 performance tuning..."
        bash "${SCRIPTS_DIR}/optimize_rpi5.sh"
    fi

    set_phase 4

    # Reboot required for kernel param changes
    CMDLINE_NEW=$(cat /boot/firmware/cmdline.txt 2>/dev/null || echo "")
    if ! echo "${CMDLINE_NEW}" | grep -q "pcie_aspm=off"; then
        echo ""
        warn "═══════════════════════════════════════════════════════"
        warn "  REBOOT REQUIRED — kernel parameters must take effect."
        warn "  After reboot, run this script again:"
        warn ""
        warn "    ./'one click HW deployment.sh'"
        warn "═══════════════════════════════════════════════════════"
        echo ""
        read -rp "  Reboot now? [Y/n] " _reply
        [[ "${_reply,,}" != "n" ]] && sudo reboot
        exit 0
    fi
fi

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 4 — Environment & Credentials Setup
# ═════════════════════════════════════════════════════════════════════════════
if [[ "${PHASE}" -le 4 ]]; then
    step "Phase 4 / 6 — Environment & Firebase Credentials"

    # ── .env setup ───────────────────────────────────────────────────────────
    if [[ ! -f "${ENV_FILE}" ]]; then
        fail ".env file not found at ${ENV_FILE}. Copy it from your Mac (scp) before running."
    fi
    ok ".env found"

    # ── Validate APP_ID / DEVICE_ID ──────────────────────────────────────────
    APP_ID_VAL=$(grep -E '^APP_ID=' "${ENV_FILE}" | cut -d= -f2 | tr -d '[:space:]' || echo "")
    DEVICE_ID_VAL=$(grep -E '^DEVICE_ID=' "${ENV_FILE}" | cut -d= -f2 | tr -d '[:space:]' || echo "")

    echo ""
    info "Current device identity in .env:"
    echo "    APP_ID   = ${APP_ID_VAL:-<not set>}"
    echo "    DEVICE_ID= ${DEVICE_ID_VAL:-<not set>}"
    echo ""

    read -rp "  Change DEVICE_ID? (enter new value or press Enter to keep '${DEVICE_ID_VAL}'): " _new_id
    if [[ -n "${_new_id}" ]]; then
        sed -i "s|^DEVICE_ID=.*|DEVICE_ID=${_new_id}|" "${ENV_FILE}"
        ok "DEVICE_ID updated to: ${_new_id}"
    else
        ok "DEVICE_ID unchanged: ${DEVICE_ID_VAL}"
    fi

    # ── Firebase credentials ──────────────────────────────────────────────────
    echo ""
    if [[ -f "${CREDS_FILE}" ]]; then
        ok "Firebase credentials found: ${CREDS_FILE}"
    else
        warn "firebase-credentials.json not found."
        echo ""
        echo "  To enable Firebase sync, copy your service account key:"
        echo "    scp ~/Downloads/firebase-service-account.json \\"
        echo "        $(whoami)@$(hostname -I | awk '{print $1}'):${CREDS_FILE}"
        echo ""
        read -rp "  Continue without Firebase credentials (simulation mode)? [Y/n] " _reply
        [[ "${_reply,,}" == "n" ]] && fail "Aborted. Copy credentials and re-run."
        warn "Continuing in simulation mode — Firestore writes will be skipped."
    fi

    # ── Auto-detect Hailo-8 cgroup major number and update docker-compose.yml ─
    if [[ -c /dev/hailo0 ]]; then
        HAILO_MAJOR=$(stat -c '%t' /dev/hailo0 | xargs -I{} printf '%d' 0x{})
        info "Detected /dev/hailo0 major number: ${HAILO_MAJOR}"

        COMPOSE_FILE="${PROJECT_DIR}/docker-compose.yml"
        CURRENT_CGROUP=$(grep -oP 'c \d+:\* rmw' "${COMPOSE_FILE}" | head -1 | grep -oP '\d+' || echo "")

        if [[ "${CURRENT_CGROUP}" != "${HAILO_MAJOR}" ]]; then
            sed -i "s|c [0-9]*:\* rmw|c ${HAILO_MAJOR}:* rmw|g" "${COMPOSE_FILE}"
            ok "docker-compose.yml cgroup rule updated → c ${HAILO_MAJOR}:* rmw"
        else
            ok "docker-compose.yml cgroup rule already correct (${HAILO_MAJOR})"
        fi
    fi

    # ── Ensure data directory exists ──────────────────────────────────────────
    mkdir -p "${PROJECT_DIR}/data"
    ok "data/ directory ready (SQLite queue storage)"

    set_phase 5
fi

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 5 — System Verification
# ═════════════════════════════════════════════════════════════════════════════
if [[ "${PHASE}" -le 5 ]]; then
    step "Phase 5 / 6 — System Verification"

    bash "${SCRIPTS_DIR}/check_system.sh" || {
        echo ""
        warn "Verification reported issues above."
        read -rp "  Continue anyway? [y/N] " _reply
        [[ "${_reply,,}" == "y" ]] || fail "Aborted. Fix reported issues and re-run."
    }

    set_phase 6
fi

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 6 — Build Containers, Launch, Desktop Shortcut
# ═════════════════════════════════════════════════════════════════════════════
if [[ "${PHASE}" -le 6 ]]; then
    step "Phase 6 / 6 — Build & Launch"

    # Verify docker group is active before attempting build.
    # If the user reached Phase 6 directly (e.g. after a reboot that bypassed
    # Phase 2's sg re-exec), re-launch under the docker group context.
    if ! docker info &>/dev/null 2>&1; then
        warn "Docker socket not accessible. Re-launching under docker group context..."
        exec sg docker -c "bash \"${PROJECT_DIR}/one click HW deployment.sh\""
    fi

    # Grant X display access for the Display Agent (HDMI HUD)
    DISPLAY=:0 xhost +local: 2>/dev/null && ok "HDMI display access granted (xhost)" || \
        warn "xhost failed — HDMI display agent may not render. Run: DISPLAY=:0 xhost +local:"

    # Build images and start all containers
    info "Building Docker images (this may take 5–15 minutes on first run)..."
    docker compose up -d --build
    ok "All containers started"

    # Desktop shortcut
    DESKTOP_DIR="${HOME}/Desktop"
    mkdir -p "${DESKTOP_DIR}"
    cat > "${DESKTOP_DIR}/SurgicalAI.desktop" << DEOF
[Desktop Entry]
Name=SurgicalAI
Comment=Start Surgical AI Detection System
Exec=bash -c "DISPLAY=:0 xhost +local: && cd ${PROJECT_DIR} && docker compose up -d && sleep 2 && docker compose ps"
Icon=utilities-terminal
Terminal=true
Type=Application
Categories=Application;
DEOF
    chmod +x "${DESKTOP_DIR}/SurgicalAI.desktop"
    ok "Desktop shortcut created: ~/Desktop/SurgicalAI.desktop"

    # Clean up state file — deployment complete
    rm -f "${STATE_FILE}"

    # ── Final status ──────────────────────────────────────────────────────────
    echo ""
    info "Waiting 10s for containers to become healthy..."
    sleep 10

    echo ""
    docker compose ps
    echo ""

    banner "Deployment Complete"
    echo "  ┌──────────────────────────────────────────────────────────┐"
    echo "  │                                                          │"
    echo "  │  Gateway health  : curl http://localhost:8000/health     │"
    echo "  │  Web dashboard   : https://surgicalai01.web.app/admin   │"
    echo "  │  Desktop launch  : ~/Desktop/SurgicalAI.desktop          │"
    echo "  │                                                          │"
    echo "  │  Logs  : docker compose logs -f gateway_agent            │"
    echo "  │  Status: docker compose ps                               │"
    echo "  │                                                          │"
    echo "  └──────────────────────────────────────────────────────────┘"
    echo ""
    echo "  DEVICE_ID : $(grep -E '^DEVICE_ID=' "${ENV_FILE}" | cut -d= -f2)"
    echo "  APP_ID    : $(grep -E '^APP_ID=' "${ENV_FILE}" | cut -d= -f2)"
    echo ""
fi

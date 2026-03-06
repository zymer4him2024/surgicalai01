#!/usr/bin/env bash
# =============================================================================
# setup_hailo.sh — Hailo-8 드라이버 및 펌웨어 설치 스크립트
#
# 대상:  Raspberry Pi 5 (64-bit OS, Debian Bookworm)
# 설치:  hailo-all (hailort + hailort-pcie-driver-dkms + hailo-firmware)
# 확인:  /dev/hailo0 존재, 온도/펌웨어 버전 조회
#
# 사용법:
#   chmod +x scripts/setup_hailo.sh
#   ./scripts/setup_hailo.sh
# =============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ─────────────────────────────────────────────────────────────────────────────
# 0) 사전 조건 확인
# ─────────────────────────────────────────────────────────────────────────────

[[ $(uname -m) == "aarch64" ]] || error "이 스크립트는 ARM64(aarch64) 전용입니다. RPi5인지 확인하세요."
[[ $(id -u) -ne 0 ]] || error "root로 실행하지 마세요. sudo 권한이 있는 일반 사용자로 실행하세요."

info "===== Hailo-8 드라이버 설치 시작 ====="
info "OS: $(. /etc/os-release && echo "$PRETTY_NAME")"
info "커널: $(uname -r)"
info "아키텍처: $(uname -m)"

# ─────────────────────────────────────────────────────────────────────────────
# 1) PCIe 활성화 확인 (/boot/firmware/config.txt)
# ─────────────────────────────────────────────────────────────────────────────

BOOT_CONFIG="/boot/firmware/config.txt"
info "PCIe 설정 확인: ${BOOT_CONFIG}"

PCIE_ENABLED=false
PCIE_GEN3=false

if grep -q "dtparam=pciex1$\|dtparam=pciex1 " "${BOOT_CONFIG}" 2>/dev/null; then
    PCIE_ENABLED=true
    success "PCIe x1 이미 활성화됨"
else
    warn "PCIe x1이 비활성화되어 있습니다. 자동으로 추가합니다."
    echo "" | sudo tee -a "${BOOT_CONFIG}" > /dev/null
    echo "# Hailo-8 PCIe 설정 (setup_hailo.sh에 의해 추가됨)" | sudo tee -a "${BOOT_CONFIG}" > /dev/null
    echo "dtparam=pciex1" | sudo tee -a "${BOOT_CONFIG}" > /dev/null
    PCIE_ENABLED=true
fi

if grep -q "dtparam=pciex1_gen=3" "${BOOT_CONFIG}" 2>/dev/null; then
    PCIE_GEN3=true
    success "PCIe Gen 3 이미 설정됨"
else
    warn "PCIe Gen 3 설정 추가 (5GT/s — Hailo-8 최대 대역폭)"
    echo "dtparam=pciex1_gen=3" | sudo tee -a "${BOOT_CONFIG}" > /dev/null
    PCIE_GEN3=true
fi

# ─────────────────────────────────────────────────────────────────────────────
# 2) 이미 설치 여부 확인
# ─────────────────────────────────────────────────────────────────────────────

if command -v hailortcli &>/dev/null && [[ -c /dev/hailo0 ]]; then
    HAILORT_VER=$(hailortcli fw-control identify 2>/dev/null | grep -oP 'Firmware Version: \K[\d.]+' || echo "unknown")
    success "Hailo-8 이미 설치됨 (펌웨어 버전: ${HAILORT_VER})"
    success "/dev/hailo0 장치 존재"
    info "재설치하려면 먼저 'sudo apt remove hailo-all'을 실행하세요."
    exit 0
fi

# ─────────────────────────────────────────────────────────────────────────────
# 3) Hailo apt 저장소 설정
# ─────────────────────────────────────────────────────────────────────────────

info "Hailo apt 저장소 설정 중..."

# Raspberry Pi OS 공식 저장소에서 hailo-all 패키지 제공 (2024년 기준)
# RPi OS 업데이트 채널을 통해 hailo-all을 설치
sudo apt-get update -qq

# rpi-connect-lite 또는 rpicam-apps처럼 RPi 공식 apt에 포함되어 있는지 먼저 확인
if apt-cache show hailo-all &>/dev/null; then
    info "RPi 공식 apt 저장소에서 hailo-all 발견"
else
    # Hailo 개발자 저장소 수동 추가
    info "Hailo 공식 저장소 추가 중..."
    HAILO_KEYRING="/usr/share/keyrings/hailo-keyring.gpg"
    HAILO_LIST="/etc/apt/sources.list.d/hailo.list"

    if [[ ! -f "${HAILO_KEYRING}" ]]; then
        curl -fsSL https://hailo.ai/apt/hailo-keyring.gpg | sudo gpg --dearmor -o "${HAILO_KEYRING}"
        success "Hailo GPG 키 추가됨"
    fi

    CODENAME=$(. /etc/os-release && echo "${VERSION_CODENAME:-bookworm}")
    echo "deb [arch=arm64 signed-by=${HAILO_KEYRING}] https://hailo.ai/apt/ubuntu ${CODENAME} main" \
        | sudo tee "${HAILO_LIST}" > /dev/null
    success "Hailo 저장소 추가: ${HAILO_LIST}"
    sudo apt-get update -qq
fi

# ─────────────────────────────────────────────────────────────────────────────
# 4) hailo-all 설치 (hailort + DKMS 드라이버 + 펌웨어)
# ─────────────────────────────────────────────────────────────────────────────

info "hailo-all 패키지 설치 중..."
info "  - hailort (사용자 공간 런타임)"
info "  - hailort-pcie-driver-dkms (PCIe 커널 모듈)"
info "  - hailo-firmware (NPU 펌웨어)"

sudo apt-get install -y hailo-all

# DKMS 모듈 빌드 확인
info "DKMS 커널 모듈 빌드 확인..."
if sudo dkms status | grep -q "hailo_pci"; then
    success "hailo_pci DKMS 모듈 빌드됨:"
    sudo dkms status | grep hailo_pci
else
    warn "DKMS 모듈 상태를 확인할 수 없습니다. 재부팅 후 확인하세요."
fi

# ─────────────────────────────────────────────────────────────────────────────
# 5) 커널 모듈 로드 및 장치 확인
# ─────────────────────────────────────────────────────────────────────────────

info "hailo_pci 커널 모듈 로드 중..."
if sudo modprobe hailo_pci 2>/dev/null; then
    success "hailo_pci 모듈 로드 완료"
else
    warn "hailo_pci 모듈 로드 실패 — 재부팅이 필요할 수 있습니다."
fi

# 모듈 자동 로드 설정
if ! grep -q "hailo_pci" /etc/modules 2>/dev/null; then
    echo "hailo_pci" | sudo tee -a /etc/modules > /dev/null
    success "/etc/modules에 hailo_pci 추가됨 (부팅 시 자동 로드)"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 6) 사용자 hailo 그룹 추가
# ─────────────────────────────────────────────────────────────────────────────

CURRENT_USER="${SUDO_USER:-$(whoami)}"
if getent group hailo &>/dev/null; then
    if id -nG "${CURRENT_USER}" | grep -qw "hailo"; then
        success "${CURRENT_USER}이(가) 이미 hailo 그룹에 속해 있습니다"
    else
        sudo usermod -aG hailo "${CURRENT_USER}"
        success "${CURRENT_USER}를 hailo 그룹에 추가했습니다"
        warn "그룹 변경 적용을 위해 로그아웃 후 재로그인이 필요합니다."
    fi
else
    warn "hailo 그룹이 존재하지 않습니다. hailo-all 설치 후 재시도하세요."
fi

# Docker에서 /dev/hailo0 접근을 위한 udev 규칙 확인
UDEV_RULES_FILE="/etc/udev/rules.d/99-hailo.rules"
if [[ ! -f "${UDEV_RULES_FILE}" ]]; then
    info "udev 규칙 생성 중..."
    echo 'SUBSYSTEM=="hailo_chardev", KERNEL=="hailo*", GROUP="hailo", MODE="0660"' \
        | sudo tee "${UDEV_RULES_FILE}" > /dev/null
    sudo udevadm control --reload-rules
    sudo udevadm trigger
    success "udev 규칙 생성: ${UDEV_RULES_FILE}"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 7) docker-compose.yml의 cgroup rule 업데이트 확인
# ─────────────────────────────────────────────────────────────────────────────

if [[ -c /dev/hailo0 ]]; then
    HAILO_MAJOR=$(stat -c '%t' /dev/hailo0 | xargs -I{} printf '%d' 0x{})
    success "/dev/hailo0 감지됨 (메이저 번호: ${HAILO_MAJOR})"
    info "docker-compose.yml의 device_cgroup_rules를 다음으로 업데이트하세요:"
    echo "    device_cgroup_rules:"
    echo "      - \"c ${HAILO_MAJOR}:* rmw\""
else
    warn "/dev/hailo0가 아직 존재하지 않습니다. 재부팅 후 다시 확인하세요."
    info "예상 메이저 번호: 235 또는 240 (배포판마다 다를 수 있음)"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 8) 설치 검증
# ─────────────────────────────────────────────────────────────────────────────

echo ""
info "===== 설치 검증 ====="

if command -v hailortcli &>/dev/null; then
    success "hailortcli 설치됨: $(hailortcli --version 2>/dev/null | head -1)"
else
    warn "hailortcli 명령어를 찾을 수 없습니다"
fi

if [[ -c /dev/hailo0 ]]; then
    success "/dev/hailo0 장치 존재"
    info "펌웨어 버전 확인 중..."
    hailortcli fw-control identify 2>/dev/null || warn "hailortcli 실행 실패 (재부팅 필요)"
else
    warn "/dev/hailo0 미존재 — 재부팅 필요"
fi

echo ""
success "===== 설치 완료 ====="
echo ""
echo "  다음 단계:"
echo "  1. sudo reboot          ← 커널 모듈 적용을 위해 재부팅 필수"
echo "  2. ls -la /dev/hailo0  ← 장치 노드 확인"
echo "  3. hailortcli fw-control identify  ← 펌웨어 버전 확인"
echo "  4. hailortcli monitor  ← NPU 온도 모니터링"
echo ""
echo "  Docker 환경에서 Hailo-8 사용 설정:"
echo "  docker-compose.yml → devices: /dev/hailo0:/dev/hailo0"
echo "  docker-compose.yml → device_cgroup_rules: \"c MAJOR:* rmw\""

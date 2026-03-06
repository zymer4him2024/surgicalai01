#!/usr/bin/env bash
# =============================================================================
# optimize_rpi5.sh — RPi5 + Hailo-8 최대 성능 최적화 스크립트
#
# 목표:  Hailo-8 NPU 26 TOPS 최대 성능 발휘를 위한 시스템 튜닝
# 대상:  Raspberry Pi 5 (64-bit Debian Bookworm)
#
# 주요 최적화:
#   1. CPU Governor → performance 고정 (2.4GHz turbo)
#   2. PCIe ASPM 비활성화 (레이턴시 감소)
#   3. USB 오토서스펜드 비활성화
#   4. 메모리 스왑 최소화 (ML 워크로드)
#   5. GPU 메모리 최소화 (64MB → 추론 전용)
#   6. 시스템 파일 디스크립터 한도 상향
#   7. 네트워크 버퍼 튜닝 (Firebase 실시간 동기화)
#
# 사용법:
#   chmod +x scripts/optimize_rpi5.sh
#   ./scripts/optimize_rpi5.sh
#   sudo reboot
# =============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

[[ $(uname -m) == "aarch64" ]] || error "ARM64(aarch64) 전용 스크립트입니다."
[[ $(id -u) -ne 0 ]] || error "root로 실행하지 마세요."

BOOT_CONFIG="/boot/firmware/config.txt"
CMDLINE_FILE="/boot/firmware/cmdline.txt"
SYSCTL_FILE="/etc/sysctl.d/99-surgicalai.conf"

info "===== RPi5 Hailo-8 성능 최적화 시작 ====="

# ─────────────────────────────────────────────────────────────────────────────
# 1) CPU Governor — performance 모드 고정
# ─────────────────────────────────────────────────────────────────────────────

info "[1/7] CPU Governor 설정 중..."

# 즉시 적용
GOVERNORS=$(ls /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null || true)
if [[ -n "${GOVERNORS}" ]]; then
    for gov in ${GOVERNORS}; do
        echo "performance" | sudo tee "${gov}" > /dev/null
    done
    CURRENT_GOV=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo "unknown")
    success "CPU Governor 즉시 적용: ${CURRENT_GOV}"
    MAX_FREQ=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq 2>/dev/null || echo "unknown")
    info "  최대 주파수: $((MAX_FREQ / 1000)) MHz"
fi

# systemd 서비스로 영구 적용 (재부팅 후에도 유지)
sudo tee /etc/systemd/system/cpu-performance.service > /dev/null << 'EOF'
[Unit]
Description=Set CPU Governor to Performance for Hailo-8 Inference
After=multi-user.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance > $f; done'
ExecStop=/bin/sh -c 'for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo ondemand > $f; done'

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable cpu-performance.service
success "CPU performance 서비스 등록 완료 (재부팅 후 자동 적용)"

# ─────────────────────────────────────────────────────────────────────────────
# 2) /boot/firmware/config.txt — 성능 파라미터
# ─────────────────────────────────────────────────────────────────────────────

info "[2/7] /boot/firmware/config.txt 성능 파라미터 설정 중..."

# 백업
sudo cp "${BOOT_CONFIG}" "${BOOT_CONFIG}.bak.$(date +%Y%m%d_%H%M%S)"

apply_config_param() {
    local key="$1" val="$2" comment="$3"
    if grep -q "^${key}=" "${BOOT_CONFIG}"; then
        sudo sed -i "s|^${key}=.*|${key}=${val}|" "${BOOT_CONFIG}"
        info "  업데이트: ${key}=${val}  # ${comment}"
    else
        echo "${key}=${val}  # ${comment}" | sudo tee -a "${BOOT_CONFIG}" > /dev/null
        info "  추가: ${key}=${val}  # ${comment}"
    fi
}

# 설정 구분자 추가
if ! grep -q "# === SurgicalAI Performance ===" "${BOOT_CONFIG}"; then
    echo "" | sudo tee -a "${BOOT_CONFIG}" > /dev/null
    echo "# === SurgicalAI Performance (optimize_rpi5.sh) ===" | sudo tee -a "${BOOT_CONFIG}" > /dev/null
fi

apply_config_param "arm_boost"           "1"      "CPU 터보 활성화 (2.4GHz)"
apply_config_param "over_voltage_delta"  "50000"  "+50mV 전압 부스트 (터보 안정성)"
apply_config_param "gpu_mem"             "64"     "VideoCore 메모리 최소화 (추론 전용)"
apply_config_param "dtparam=pciex1"      ""       ""   # 이미 setup_hailo.sh에서 설정
apply_config_param "dtparam=pciex1_gen"  "3"      "PCIe Gen3 (5GT/s, Hailo-8 최대 대역폭)"

# arm_boost 특별 처리 (값 없이 단독으로 추가)
if ! grep -q "^arm_boost=1" "${BOOT_CONFIG}"; then
    echo "arm_boost=1  # CPU 터보 활성화 (2.4GHz)" | sudo tee -a "${BOOT_CONFIG}" > /dev/null
fi
if ! grep -q "^over_voltage_delta=50000" "${BOOT_CONFIG}"; then
    echo "over_voltage_delta=50000  # +50mV 전압 (터보 안정성)" | sudo tee -a "${BOOT_CONFIG}" > /dev/null
fi
if ! grep -q "^gpu_mem=64" "${BOOT_CONFIG}"; then
    echo "gpu_mem=64  # VideoCore 메모리 최소화" | sudo tee -a "${BOOT_CONFIG}" > /dev/null
fi

success "config.txt 업데이트 완료 (백업: ${BOOT_CONFIG}.bak.*)"

# ─────────────────────────────────────────────────────────────────────────────
# 3) cmdline.txt — PCIe ASPM 비활성화
# ─────────────────────────────────────────────────────────────────────────────

info "[3/7] PCIe ASPM 비활성화 (레이턴시 최소화)..."

sudo cp "${CMDLINE_FILE}" "${CMDLINE_FILE}.bak.$(date +%Y%m%d_%H%M%S)"
CMDLINE=$(cat "${CMDLINE_FILE}")

if echo "${CMDLINE}" | grep -q "pcie_aspm=off"; then
    success "pcie_aspm=off 이미 설정됨"
else
    # 줄 끝에 추가 (cmdline.txt는 한 줄이어야 함)
    echo "${CMDLINE} pcie_aspm=off" | sudo tee "${CMDLINE_FILE}" > /dev/null
    success "pcie_aspm=off 추가됨"
    info "  효과: PCIe Active State Power Management 비활성화 → 추론 레이턴시 감소"
fi

# USB 오토서스펜드 비활성화
if echo "$(cat ${CMDLINE_FILE})" | grep -q "usbcore.autosuspend=-1"; then
    success "usbcore.autosuspend=-1 이미 설정됨"
else
    CMDLINE=$(cat "${CMDLINE_FILE}")
    echo "${CMDLINE} usbcore.autosuspend=-1" | sudo tee "${CMDLINE_FILE}" > /dev/null
    success "usbcore.autosuspend=-1 추가됨 (USB 카메라 안정성)"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 4) sysctl 커널 파라미터 튜닝
# ─────────────────────────────────────────────────────────────────────────────

info "[4/7] sysctl 커널 파라미터 튜닝 중..."

sudo tee "${SYSCTL_FILE}" > /dev/null << 'EOF'
# =============================================================================
# /etc/sysctl.d/99-surgicalai.conf
# RPi5 + Hailo-8 성능 최적화 (SurgicalAI01)
# =============================================================================

# ── 메모리 관리 ──────────────────────────────────────────────────────────────
# 스왑 사용 최소화 (ML 워크로드는 메모리 접근 패턴이 예측 불가능)
vm.swappiness = 5
# 더티 페이지 플러시 주기 최적화
vm.dirty_ratio = 10
vm.dirty_background_ratio = 5

# ── 파일 디스크립터 ───────────────────────────────────────────────────────────
# Docker 컨테이너 + Hailo 드라이버 + 카메라 스트림 고려
fs.file-max = 131072

# ── 네트워크 — Firebase 실시간 동기화 최적화 ─────────────────────────────────
# TCP 소켓 버퍼 (Firebase Firestore WebSocket)
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.core.rmem_default = 1048576
net.core.wmem_default = 1048576
net.ipv4.tcp_rmem = 4096 1048576 16777216
net.ipv4.tcp_wmem = 4096 1048576 16777216

# SYN 백로그 (API 서버 동시 연결)
net.core.somaxconn = 1024
net.ipv4.tcp_max_syn_backlog = 1024

# TIME_WAIT 소켓 재사용 (빠른 재연결)
net.ipv4.tcp_tw_reuse = 1
net.ipv4.tcp_fin_timeout = 15

# ── IPC 공유 메모리 (Hailo SDK POSIX SHM) ────────────────────────────────────
# Hailo SDK는 DMA 버퍼 공유에 POSIX 공유 메모리 사용
kernel.shmmax = 536870912
kernel.shmall = 131072
EOF

sudo sysctl -p "${SYSCTL_FILE}" 2>/dev/null && success "sysctl 파라미터 즉시 적용됨" || warn "일부 파라미터는 재부팅 후 적용됩니다"

# ─────────────────────────────────────────────────────────────────────────────
# 5) 시스템 파일 디스크립터 한도 상향 (limits.conf)
# ─────────────────────────────────────────────────────────────────────────────

info "[5/7] 시스템 파일 디스크립터 한도 설정 중..."

LIMITS_FILE="/etc/security/limits.d/99-surgicalai.conf"
sudo tee "${LIMITS_FILE}" > /dev/null << 'EOF'
# SurgicalAI01 — Docker + Hailo-8 운영을 위한 FD 한도
*    soft nofile 65535
*    hard nofile 65535
root soft nofile 65535
root hard nofile 65535
EOF
success "limits.conf 업데이트: ${LIMITS_FILE}"

# ─────────────────────────────────────────────────────────────────────────────
# 6) PCIe 전원 관리 런타임 비활성화 (실시간 적용)
# ─────────────────────────────────────────────────────────────────────────────

info "[6/7] PCIe 장치 전원 관리 비활성화 (런타임)..."

# Hailo-8 PCIe 장치 전원 제어를 'on'으로 고정 (자동 슬립 방지)
PCIE_POWER_RULE="/etc/udev/rules.d/99-pcie-performance.rules"
sudo tee "${PCIE_POWER_RULE}" > /dev/null << 'EOF'
# Hailo-8 PCIe 장치 전원 관리 비활성화 (자동 슬립 방지)
ACTION=="add", SUBSYSTEM=="pci", ATTR{vendor}=="0x1e60", ATTR{class}=="0x120000", \
    ATTR{power/control}="on"

# 모든 PCIe 장치 runtime PM 비활성화 (추론 레이턴시 최소화)
ACTION=="add", SUBSYSTEM=="pci", ATTR{power/control}="on"
EOF
sudo udevadm control --reload-rules
success "PCIe 전원 관리 udev 규칙 적용: ${PCIE_POWER_RULE}"

# 현재 실행 중인 PCIe 장치에도 즉시 적용
if find /sys/bus/pci/devices/ -name "power/control" &>/dev/null; then
    find /sys/bus/pci/devices/ -name "power/control" -exec sudo sh -c 'echo on > {}' \; 2>/dev/null || true
    success "실행 중인 PCIe 장치 전원 관리 비활성화 완료"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 7) 스왑 설정 최적화
# ─────────────────────────────────────────────────────────────────────────────

info "[7/7] 스왑 설정 최적화..."

# 현재 스왑 확인
SWAP_TOTAL=$(free -m | awk '/^Swap:/ {print $2}')
if [[ "${SWAP_TOTAL}" -gt 0 ]]; then
    info "현재 스왑: ${SWAP_TOTAL}MB"
    # dphys-swapfile 설정 (RPi 기본 스왑 관리자)
    if [[ -f /etc/dphys-swapfile ]]; then
        sudo sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=512/' /etc/dphys-swapfile
        success "스왑 크기를 512MB로 제한 (ML 워크로드 안정성)"
    fi
else
    info "스왑이 비활성화되어 있습니다"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 최종 요약
# ─────────────────────────────────────────────────────────────────────────────

echo ""
success "===== 최적화 완료 ====="
echo ""
echo "  적용된 최적화 요약:"
echo ""
echo "  ┌──────────────────────────────────────────────────────────────┐"
echo "  │  CPU Governor    → performance (2.4GHz 터보 고정)            │"
echo "  │  PCIe ASPM       → OFF (추론 레이턴시 ~1ms 감소)            │"
echo "  │  PCIe Gen        → 3 (5GT/s, 최대 Hailo-8 대역폭)          │"
echo "  │  USB Autosuspend → 비활성화 (카메라 안정성)                  │"
echo "  │  GPU 메모리      → 64MB (RAM 최대 추론에 활용)               │"
echo "  │  vm.swappiness   → 5 (스왑 최소화)                          │"
echo "  │  PCIe 전원 관리  → ON (고정, 슬립 방지)                      │"
echo "  │  FD 한도         → 65535 (Docker + Hailo + 카메라)          │"
echo "  │  over_voltage    → +50mV (터보 모드 안정성)                  │"
echo "  └──────────────────────────────────────────────────────────────┘"
echo ""
warn "재부팅이 필요합니다: sudo reboot"
echo ""
echo "  재부팅 후 확인:"
echo "  cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"
echo "  # → performance"
echo ""
echo "  hailortcli monitor"
echo "  # → NPU 온도 및 사용률 모니터링"
echo ""
echo "  cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_cur_freq"
echo "  # → 2400000 (2.4GHz)"

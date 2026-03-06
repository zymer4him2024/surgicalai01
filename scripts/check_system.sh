#!/usr/bin/env bash
# =============================================================================
# check_system.sh — RPi5 + Hailo-8 배포 전 시스템 점검 스크립트
#
# 사용법:
#   chmod +x scripts/check_system.sh
#   ./scripts/check_system.sh
# =============================================================================

set -uo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
pass()  { echo -e "  ${GREEN}✅ PASS${NC}  $*"; }
fail()  { echo -e "  ${RED}❌ FAIL${NC}  $*"; FAIL_COUNT=$((FAIL_COUNT+1)); }
warn()  { echo -e "  ${YELLOW}⚠️  WARN${NC}  $*"; WARN_COUNT=$((WARN_COUNT+1)); }
info()  { echo -e "  ${BLUE}ℹ️  INFO${NC}  $*"; }
title() { echo -e "\n${BOLD}${BLUE}━━━ $* ━━━${NC}"; }

FAIL_COUNT=0
WARN_COUNT=0

echo ""
echo -e "${BOLD}SurgicalAI01 — 배포 전 시스템 점검${NC}"
echo "날짜: $(date)"
echo "호스트: $(hostname)"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
title "1. 하드웨어"
# ─────────────────────────────────────────────────────────────────────────────

ARCH=$(uname -m)
if [[ "${ARCH}" == "aarch64" ]]; then
    pass "아키텍처: ${ARCH} (ARM64 ✓)"
else
    fail "아키텍처: ${ARCH} (ARM64 필요)"
fi

# RPi5 모델 확인
MODEL=$(cat /proc/cpuinfo | grep -i "model name\|Model" | tail -1 | cut -d: -f2 | xargs 2>/dev/null || echo "unknown")
if echo "${MODEL}" | grep -qi "raspberry.*pi.*5\|bcm2712"; then
    pass "모델: ${MODEL}"
else
    warn "모델: ${MODEL} (RPi5 확인 필요)"
fi

# 메모리
MEM_GB=$(awk '/MemTotal/ {printf "%.1f", $2/1024/1024}' /proc/meminfo)
if (( $(echo "${MEM_GB} >= 7" | bc -l 2>/dev/null || echo 0) )); then
    pass "RAM: ${MEM_GB} GB (8GB ✓)"
elif (( $(echo "${MEM_GB} >= 3" | bc -l 2>/dev/null || echo 0) )); then
    warn "RAM: ${MEM_GB} GB (8GB 권장)"
else
    fail "RAM: ${MEM_GB} GB (부족)"
fi

# 디스크
DISK_FREE_GB=$(df -BG / | awk 'NR==2 {gsub("G",""); print $4}')
if [[ "${DISK_FREE_GB:-0}" -ge 10 ]]; then
    pass "디스크 여유: ${DISK_FREE_GB}GB"
elif [[ "${DISK_FREE_GB:-0}" -ge 5 ]]; then
    warn "디스크 여유: ${DISK_FREE_GB}GB (10GB 이상 권장)"
else
    fail "디스크 여유: ${DISK_FREE_GB}GB (부족)"
fi

# ─────────────────────────────────────────────────────────────────────────────
title "2. Hailo-8 NPU"
# ─────────────────────────────────────────────────────────────────────────────

# 장치 노드
if [[ -c /dev/hailo0 ]]; then
    HAILO_PERMS=$(stat -c '%a %G' /dev/hailo0)
    pass "/dev/hailo0 존재 (권한: ${HAILO_PERMS})"
else
    fail "/dev/hailo0 없음 (setup_hailo.sh 실행 필요)"
fi

# 커널 모듈
if lsmod | grep -q "hailo_pci"; then
    HAILO_MOD=$(lsmod | grep hailo_pci)
    pass "hailo_pci 커널 모듈 로드됨"
else
    fail "hailo_pci 커널 모듈 미로드 (modprobe hailo_pci 또는 재부팅 필요)"
fi

# hailortcli
if command -v hailortcli &>/dev/null; then
    HAILORT_VER=$(hailortcli --version 2>/dev/null | head -1 | grep -oP '[\d.]+' | head -1 || echo "unknown")
    pass "hailortcli 설치됨 (버전: ${HAILORT_VER})"

    # 펌웨어 식별
    if FW_INFO=$(hailortcli fw-control identify 2>/dev/null); then
        FW_VER=$(echo "${FW_INFO}" | grep -oP 'Firmware Version: \K[\d.]+' || echo "unknown")
        pass "Hailo-8 펌웨어: ${FW_VER}"
    else
        fail "hailortcli fw-control identify 실패"
    fi
else
    fail "hailortcli 미설치 (setup_hailo.sh 실행 필요)"
fi

# PCIe 확인
if lspci 2>/dev/null | grep -qi "hailo\|1e60"; then
    HAILO_PCI=$(lspci 2>/dev/null | grep -i "hailo\|1e60" | head -1)
    pass "PCIe Hailo-8 감지: ${HAILO_PCI}"
else
    warn "lspci에서 Hailo-8 미감지 (lspci 없거나 다른 슬롯 사용 중)"
fi

# NPU 온도 (과열 확인)
if command -v hailortcli &>/dev/null && [[ -c /dev/hailo0 ]]; then
    NPU_TEMP=$(hailortcli fw-control identify 2>/dev/null | grep -oP 'Temperature: \K[\d.]+' || echo "")
    if [[ -n "${NPU_TEMP}" ]]; then
        if (( $(echo "${NPU_TEMP} < 70" | bc -l 2>/dev/null || echo 0) )); then
            pass "NPU 온도: ${NPU_TEMP}°C (정상)"
        elif (( $(echo "${NPU_TEMP} < 85" | bc -l 2>/dev/null || echo 0) )); then
            warn "NPU 온도: ${NPU_TEMP}°C (주의: 방열판/팬 확인)"
        else
            fail "NPU 온도: ${NPU_TEMP}°C (과열! 즉시 냉각 조치 필요)"
        fi
    fi
fi

# hailo 그룹
CURRENT_USER="${USER:-$(whoami)}"
if id -nG "${CURRENT_USER}" 2>/dev/null | grep -qw "hailo"; then
    pass "${CURRENT_USER} → hailo 그룹 포함"
else
    warn "${CURRENT_USER}이(가) hailo 그룹에 없음 (docker 컨테이너 내 접근에는 불필요)"
fi

# ─────────────────────────────────────────────────────────────────────────────
title "3. Docker"
# ─────────────────────────────────────────────────────────────────────────────

if command -v docker &>/dev/null; then
    DOCKER_VER=$(docker --version | grep -oP '[\d.]+' | head -1)
    DOCKER_MAJOR=$(echo "${DOCKER_VER}" | cut -d. -f1)
    if [[ "${DOCKER_MAJOR}" -ge 24 ]]; then
        pass "Docker: ${DOCKER_VER}"
    else
        warn "Docker: ${DOCKER_VER} (24.x 이상 권장)"
    fi
else
    fail "Docker 미설치 (setup_docker.sh 실행 필요)"
fi

if docker compose version &>/dev/null 2>&1; then
    COMPOSE_VER=$(docker compose version | grep -oP '[\d.]+' | head -1)
    COMPOSE_MAJOR=$(echo "${COMPOSE_VER}" | cut -d. -f1)
    if [[ "${COMPOSE_MAJOR}" -ge 2 ]]; then
        pass "Docker Compose: v${COMPOSE_VER}"
    else
        warn "Docker Compose: v${COMPOSE_VER} (v2.x 이상 필요)"
    fi
else
    fail "docker compose 플러그인 미설치"
fi

if id -nG "${CURRENT_USER}" 2>/dev/null | grep -qw "docker"; then
    pass "${CURRENT_USER} → docker 그룹 포함 (sudo 없이 실행 가능)"
else
    warn "${CURRENT_USER}이(가) docker 그룹에 없음 (sudo 필요)"
fi

# Docker 데몬 상태
if sudo systemctl is-active docker &>/dev/null 2>&1; then
    pass "Docker 데몬 실행 중"
else
    fail "Docker 데몬 중지 상태"
fi

# ─────────────────────────────────────────────────────────────────────────────
title "4. CPU 성능 설정"
# ─────────────────────────────────────────────────────────────────────────────

GOV=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo "unknown")
if [[ "${GOV}" == "performance" ]]; then
    pass "CPU Governor: ${GOV}"
else
    warn "CPU Governor: ${GOV} (performance 권장 — optimize_rpi5.sh 실행)"
fi

FREQ=$(cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_cur_freq 2>/dev/null || echo "0")
FREQ_MHZ=$((FREQ / 1000))
if [[ "${FREQ_MHZ}" -ge 2000 ]]; then
    pass "현재 CPU 주파수: ${FREQ_MHZ} MHz (터보 활성)"
else
    warn "현재 CPU 주파수: ${FREQ_MHZ} MHz (2400MHz 미달)"
fi

# PCIe ASPM
CMDLINE=$(cat /boot/firmware/cmdline.txt 2>/dev/null || echo "")
if echo "${CMDLINE}" | grep -q "pcie_aspm=off"; then
    pass "PCIe ASPM: 비활성화됨 (레이턴시 최적)"
else
    warn "PCIe ASPM: 활성화됨 (optimize_rpi5.sh 실행 권장)"
fi

# ─────────────────────────────────────────────────────────────────────────────
title "5. 카메라 장치"
# ─────────────────────────────────────────────────────────────────────────────

VIDEO_DEVS=$(ls /dev/video* 2>/dev/null || echo "")
if [[ -n "${VIDEO_DEVS}" ]]; then
    for dev in ${VIDEO_DEVS}; do
        pass "카메라 장치: ${dev}"
    done
else
    warn "/dev/video* 장치 없음 (카메라 연결 확인)"
fi

# ─────────────────────────────────────────────────────────────────────────────
title "6. 프로젝트 파일"
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${SCRIPT_DIR}"

for f in docker-compose.yml Dockerfile Dockerfile.inference Dockerfile.display Dockerfile.firebase_sync; do
    if [[ -f "${f}" ]]; then
        pass "${f} 존재"
    else
        fail "${f} 없음"
    fi
done

# .env 파일
if [[ -f ".env" ]]; then
    if grep -q "FIREBASE_CREDENTIALS" .env && grep -v "^#" .env | grep -q "FIREBASE_CREDENTIALS_PATH\|FIREBASE_CREDENTIALS_JSON"; then
        pass ".env 존재 (Firebase 자격증명 설정됨)"
    else
        warn ".env 존재하지만 Firebase 자격증명 미설정 (시뮬레이션 모드)"
    fi
else
    fail ".env 파일 없음"
fi

# HEF 모델 파일
if ls models/*.hef &>/dev/null 2>/dev/null; then
    HEF_FILES=$(ls models/*.hef)
    for hef in ${HEF_FILES}; do
        HEF_SIZE=$(du -sh "${hef}" | cut -f1)
        pass "HEF 모델: ${hef} (${HEF_SIZE})"
    done
else
    fail "models/*.hef 없음 (yolov11.hef 또는 yolov8n.hef 필요)"
fi

# data 디렉터리
if [[ -d "data" ]]; then
    pass "data/ 디렉터리 존재 (SQLite 큐 저장소)"
else
    mkdir -p data
    pass "data/ 디렉터리 생성됨"
fi

# ─────────────────────────────────────────────────────────────────────────────
title "7. 네트워크"
# ─────────────────────────────────────────────────────────────────────────────

# Firebase 연결 확인
if curl -sf --max-time 5 "https://firestore.googleapis.com" > /dev/null 2>&1; then
    pass "Firebase 접근: 연결됨"
else
    warn "Firebase 접근: 연결 실패 (오프라인 모드로 동작)"
fi

# 외부 인터넷 확인
if curl -sf --max-time 5 "https://8.8.8.8" > /dev/null 2>&1 || ping -c 1 -W 3 8.8.8.8 &>/dev/null; then
    pass "인터넷 연결: OK"
else
    warn "인터넷 연결 없음"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 최종 결과
# ─────────────────────────────────────────────────────────────────────────────

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if [[ "${FAIL_COUNT}" -eq 0 && "${WARN_COUNT}" -eq 0 ]]; then
    echo -e "  ${GREEN}${BOLD}모든 점검 통과 ✅ — 배포 준비 완료${NC}"
elif [[ "${FAIL_COUNT}" -eq 0 ]]; then
    echo -e "  ${YELLOW}${BOLD}경고 ${WARN_COUNT}건 (배포 가능, 권장 설정 확인 필요)${NC}"
else
    echo -e "  ${RED}${BOLD}실패 ${FAIL_COUNT}건, 경고 ${WARN_COUNT}건 — 배포 전 수정 필요${NC}"
fi
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

exit "${FAIL_COUNT}"

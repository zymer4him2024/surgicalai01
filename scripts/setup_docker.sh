#!/usr/bin/env bash
# =============================================================================
# setup_docker.sh — Docker CE + Docker Compose 설치/업그레이드 스크립트
#
# 대상:  Raspberry Pi 5 (64-bit Debian Bookworm)
# 설치:  docker-ce, docker-ce-cli, containerd.io,
#        docker-buildx-plugin, docker-compose-plugin
#
# 사용법:
#   chmod +x scripts/setup_docker.sh
#   ./scripts/setup_docker.sh
# =============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

[[ $(uname -m) == "aarch64" ]] || error "ARM64(aarch64) 전용 스크립트입니다."
[[ $(id -u) -ne 0 ]] || error "root로 실행하지 마세요."

CURRENT_USER="${SUDO_USER:-$(whoami)}"

info "===== Docker CE 설치/업그레이드 시작 ====="
info "OS: $(. /etc/os-release && echo "$PRETTY_NAME")"

# ─────────────────────────────────────────────────────────────────────────────
# 1) 현재 설치 버전 확인
# ─────────────────────────────────────────────────────────────────────────────

if command -v docker &>/dev/null; then
    CURRENT_DOCKER=$(docker --version 2>/dev/null | grep -oP '[\d.]+' | head -1)
    info "현재 설치된 Docker 버전: ${CURRENT_DOCKER}"
else
    info "Docker가 설치되어 있지 않습니다. 새로 설치합니다."
fi

if command -v docker &>/dev/null && docker compose version &>/dev/null; then
    CURRENT_COMPOSE=$(docker compose version 2>/dev/null | grep -oP '[\d.]+' | head -1)
    info "현재 Docker Compose 버전: ${CURRENT_COMPOSE}"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 2) 기존 비공식 패키지 제거
# ─────────────────────────────────────────────────────────────────────────────

info "기존 비공식 Docker 패키지 제거 중 (있는 경우)..."
for pkg in docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc; do
    if dpkg -l "${pkg}" &>/dev/null 2>&1; then
        sudo apt-get remove -y "${pkg}" 2>/dev/null || true
        info "  제거됨: ${pkg}"
    fi
done

# ─────────────────────────────────────────────────────────────────────────────
# 3) Docker 공식 apt 저장소 설정
# ─────────────────────────────────────────────────────────────────────────────

info "Docker 공식 apt 저장소 설정 중..."

sudo apt-get update -qq
sudo apt-get install -y ca-certificates curl

DOCKER_KEYRING="/etc/apt/keyrings/docker.asc"
sudo install -m 0755 -d /etc/apt/keyrings

if [[ ! -f "${DOCKER_KEYRING}" ]]; then
    sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o "${DOCKER_KEYRING}"
    sudo chmod a+r "${DOCKER_KEYRING}"
    success "Docker GPG 키 저장됨: ${DOCKER_KEYRING}"
fi

ARCH=$(dpkg --print-architecture)
CODENAME=$(. /etc/os-release && echo "${VERSION_CODENAME}")
DOCKER_LIST="/etc/apt/sources.list.d/docker.list"

echo "deb [arch=${ARCH} signed-by=${DOCKER_KEYRING}] https://download.docker.com/linux/debian ${CODENAME} stable" \
    | sudo tee "${DOCKER_LIST}" > /dev/null
success "Docker 저장소 추가됨: ${DOCKER_LIST}"

sudo apt-get update -qq

# ─────────────────────────────────────────────────────────────────────────────
# 4) Docker CE 최신 버전 설치
# ─────────────────────────────────────────────────────────────────────────────

info "Docker CE 최신 버전 설치 중..."
sudo apt-get install -y \
    docker-ce \
    docker-ce-cli \
    containerd.io \
    docker-buildx-plugin \
    docker-compose-plugin

# ─────────────────────────────────────────────────────────────────────────────
# 5) Docker 서비스 시작 및 자동 시작 등록
# ─────────────────────────────────────────────────────────────────────────────

sudo systemctl enable docker
sudo systemctl start docker
success "Docker 서비스 시작 및 자동 시작 등록 완료"

# ─────────────────────────────────────────────────────────────────────────────
# 6) 사용자 docker 그룹 추가
# ─────────────────────────────────────────────────────────────────────────────

if id -nG "${CURRENT_USER}" | grep -qw "docker"; then
    success "${CURRENT_USER}이(가) 이미 docker 그룹에 속해 있습니다"
else
    sudo usermod -aG docker "${CURRENT_USER}"
    success "${CURRENT_USER}를 docker 그룹에 추가했습니다"
    warn "그룹 변경 적용을 위해 로그아웃 후 재로그인이 필요합니다."
fi

# ─────────────────────────────────────────────────────────────────────────────
# 7) Docker 데몬 최적화 설정 (/etc/docker/daemon.json)
# ─────────────────────────────────────────────────────────────────────────────

DOCKER_DAEMON_JSON="/etc/docker/daemon.json"
info "Docker 데몬 최적화 설정 적용 중..."

sudo mkdir -p /etc/docker
sudo tee "${DOCKER_DAEMON_JSON}" > /dev/null << 'EOF'
{
    "log-driver": "json-file",
    "log-opts": {
        "max-size": "10m",
        "max-file": "3"
    },
    "storage-driver": "overlay2",
    "features": {
        "buildkit": true
    },
    "default-ulimits": {
        "nofile": {
            "name": "nofile",
            "hard": 65535,
            "soft": 65535
        }
    },
    "live-restore": true
}
EOF
success "Docker 데몬 설정 완료: ${DOCKER_DAEMON_JSON}"
info "  - log 드라이버: json-file (10MB × 3 rotation)"
info "  - storage: overlay2"
info "  - BuildKit 활성화"
info "  - live-restore: 컨테이너 실행 중 데몬 재시작 허용"

sudo systemctl restart docker

# ─────────────────────────────────────────────────────────────────────────────
# 8) 설치 검증
# ─────────────────────────────────────────────────────────────────────────────

echo ""
info "===== 설치 검증 ====="

NEW_DOCKER=$(docker --version 2>/dev/null)
NEW_COMPOSE=$(docker compose version 2>/dev/null)
success "Docker:         ${NEW_DOCKER}"
success "Docker Compose: ${NEW_COMPOSE}"

# 권한 테스트 (현재 사용자로 실행 시 sudo 필요할 수 있음)
if sudo docker run --rm hello-world &>/dev/null; then
    success "Docker 동작 확인: hello-world 컨테이너 실행 성공"
else
    warn "hello-world 테스트 실패. 재로그인 후 다시 시도하세요."
fi

# Buildx 확인
BUILDX_VER=$(docker buildx version 2>/dev/null | head -1)
success "Docker Buildx: ${BUILDX_VER}"

echo ""
success "===== Docker 설치 완료 ====="
echo ""
echo "  다음 단계:"
echo "  1. 로그아웃 후 재로그인 (docker 그룹 적용)"
echo "  2. docker run hello-world  ← sudo 없이 실행되면 정상"
echo "  3. docker compose version  ← Compose 버전 확인"
echo ""
echo "  SurgicalAI01 시스템 실행:"
echo "  cd /path/to/SurgicalAI01"
echo "  docker compose up -d --build"

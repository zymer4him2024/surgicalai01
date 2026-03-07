# Antigravity Surgical AI - Global Environment & Design Decisions

## 1. 개요 (Overview)
본 문서는 Raspberry Pi 5와 Hailo-8 기반의 AI 객체 인식 및 카운팅 시스템 구축을 위한 글로벌 환경 설정과 주요 설계 결정 사항을 기록합니다.
CLAUDE.md와 GEMINI.md는 AI 어시스턴트(Claude, Gemini) 간의 컨텍스트 동기화를 위해 동일한 내용을 유지합니다.

---

## 2. 하드웨어 환경 (Hardware)
- **Edge Device**: Raspberry Pi 5 (8GB) - 64-bit OS.
- **AI Acceleration**: Hailo-8 M.2 Module (26 TOPS) - Docker Container 내에서 전 전용 드라이버로 제어.
- **Imaging**: 4K USB Camera (초점 거리 고정, 고휘도 LED 링라이트 장착 권장).
- **Network**: Wi-Fi 6 또는 Ethernet (Firebase 실시간 동기화용).

---

## 3. 소프트웨어 환경 (Software)
- **Python**: 3.10+ (권장 3.11)
- **가상 환경 (Virtual Environment)**: `venv` 사용 (경로: `venv/`)
- **컨테이너 환경**: Docker Compose
- **패키지 관리 및 포맷팅**: `pyproject.toml`
  - **Linting & Formatting**: Ruff
  - **Type Checking**: Pyright
- **테스트 환경**: pytest (TDD & SOLID 원칙 준수)
- **Firebase Hosting**: `https://surgicalai01.web.app` (배포 완료)

---

## 4. 네트워크 및 포트 설계 (Network & Port Design)
모든 모듈은 Docker Internal Bridge 네트워크를 통해 API 통신을 수행합니다.

- **네트워크 이름**: `antigravity_bridge`
- **Subnet**: `172.20.0.0/16`

### 모듈별 할당된 IP 및 Port
| Module 이름 | 컨테이너 이름 | 고정 IP 주소 | 할당 포트 | 외부 노출 | 설명 |
|---|---|---|---|---|---|
| Module B (Main) | `gateway_agent` | `172.20.0.10` | `8000` | ✅ `localhost:8000` | 메인 컨트롤러, QR 해독, 상태 머신 |
| Module A (Inference) | `inference_agent` | `172.20.0.11` | `8001` | ❌ 내부 전용 | Hailo-8 기반 추론 (YOLOv11) |
| Camera Agent | `camera_agent` | `172.20.0.12` | `8002` | ❌ 내부 전용 | 4K 카메라 프레임 캡처 |
| Module C (Display) | `display_agent` | `172.20.0.13` | `8003` | ❌ 내부 전용 | HDMI HUD 출력 (더블 버퍼 렌더링) |
| Module D (Storage) | `firebase_sync_agent` | `172.20.0.14` | `8004` | ❌ 내부 전용 | 비동기 Firestore/Storage 동기화 |
| Device Master | `device_master_agent` | `172.20.0.15` | `8005` | ❌ 내부 전용 | FDA 표준 명칭 매핑 및 MDM 연동 |

---

## 5. Docker Compose 파일 구성 (Compose Variants)

| 파일명 | 사용 환경 | 특징 |
|---|---|---|
| `docker-compose.yml` | **RPi 5 + Hailo-8 타겟** | `/dev/hailo0` 디바이스 매핑, `device_cgroup_rules` 활성 |
| `docker-compose.mac.yml` | **Mac 로컬 개발/시뮬레이션** | 디바이스 매핑 없음, `HEF_PATH=/app/models/simulation.hef` |

### Mac에서 실행 (시뮬레이션 모드)
```bash
docker-compose -f docker-compose.mac.yml up -d --build
```

### Raspberry Pi 5에서 실행 (실제 Hailo-8)
```bash
docker-compose up -d --build
```

---

## 6. 모듈별 상세 아키텍처 (Modular Architecture)

### 🏗️ Module A: AI Inference Container (The "Inference Engine")
오직 "이미지를 받아서 숫자를 뱉는" 역할만 수행.
- **엔드포인트**: `POST /inference` (YOLOv11 기반 객체 탐지)
- **성능 고려**: Shared Memory (메모리 맵핑) 고려, 여러 기구 중첩 시 Batching/Tiling 로직.

### 🛡️ Module B: Main Controller & QR Decoder (The "Orchestrator")
시스템의 상태 머신(State Machine) 및 메인 컨트롤 루프 관리.
- **QR 스캔 루프**: 카메라 프레임에서 QR 코드를 감시.
- **작업 관리**: QR에 기반하여 `current_job` 생성(Target Count).
- **5-Second Logic**: 프레임과 Inference 결과를 대조하고, 5초 이상 Target과 실제 개수가 다르면 Warning/Error 상태로 진입.

### 🖥️ Module C: HDMI Display & UI Overlay (The "Frontend")
비디오 피드와 상태 UI 렌더링 (Raspberry Pi HDMI).
- **READY (Yellow)**: 스캔 중. `[Target: N items]`. 노란색 20px 테두리.
- **MATCH (Green)**: 수량 일치. 테두리 녹색. PASS 사운드 트리거.
- **ERROR (Red)**: 불일치. 빨간색 깜빡임, 초과/부족 아이템 텍스트 표시.

### 📊 Module D: Firebase Cloud Sync (The "Backend Liaison")
비동기로 데이터를 영속화.
- **Firestore**: 검수 이력 기록.
- **Storage**: 에러(ERROR 상태 5초 지속) 발생 시점 기준 0.5초 후 0.1초 간격 3장 원본 스냅샷 캡처 및 업로드.

### 🔍 Device Master Agent (The "Encyclopedia")
YOLO 레이블을 표준 제품명으로 변환.
- **FDA Mapping**: `forceps` → `Tissue Forceps, Ring (FDA Class I)`
- **MDM Bridge**: 고객사 내부 제품 코드 매핑을 위한 확장성 제공.

---

## 7. 현재 시스템 상태 관리 (Phase)

### 완료 상태 ✅
| 항목 | 설명 |
|---|---|
| Module B (Gateway) | `gateway_agent` | QR/Job 연동, 상태 머신 (READY->MATCH->ERROR), 5초 지연 트리거 완료 |
| Module A (Inference) | `inference_agent` | RPi5 + Hailo-8 실기기 가동 완료 (mode=hailo) |
| Firebase Pipeline | `firebase_sync` | 에러 시 스냅샷 트리거 및 Async Storage 업로드 완료 |
| HDMI Display (Module C) | `display_agent` | HDMI 출력 및 ASCII HUD 오버레이 연동 완료 (xhost 권한 해결) |
| Autonomous System | **Internal Logic** | Gateway 기반 자율 카운팅 루프 (Pull-based) 구현 완료 |
| UI/UX Polish | **Favicon/HUD** | SVG Favicon 적용 및 HUD ASCII 문자 대체 완료 |
| SurgeoNet Prep | **Model/Labels** | 14개 수술 도구 라벨 맵핑 및 Device Master 메타데이터 동기화 완료 |
| Preset Cycle | **Admin Dashboard** | 5개 랜덤 프리셋 세트 자동 순환 (Set 1→2→3→4→5→1). Firestore `job_config/rpi`에 `sets[]` + `cursor` 저장. MATCH/ERROR 후 5초 뒤 다음 세트로 자동 전환. |
| QR Flash Indicator | **Display HUD** | QR 스캔 성공 시 하단 중앙에 "QR SCANNED" 배너 3초 표시. `flash_text` 필드가 `/hud` 엔드포인트에 추가됨. display_agent 재빌드 필요. |
| QR Trigger | **Gateway** | `/job` 엔드포인트는 `_pending_preset`에 저장. QR 스캔 시 `_pending_preset → current_job` 전환하여 detection 시작. |
| One-click Launcher | **RPi Desktop** | `~/Desktop/SurgicalAI.desktop` 더블클릭으로 `xhost +local:` 및 `docker compose up -d` 자동 실행. |

### 🌐 Web Dashboard (Firebase Hosting & Authentication)
현재 `Firebase Hosting`으로 배포되어 있으며, **Google Login**을 통한 보안 접속이 필수입니다. (`firestore.rules` 접근 통제)
- **Tech Stack**: HTML, Tailwind CSS, Vanilla JS, Firebase v10 SDK.
- **Admin View (`/admin`)**: `sync_events` 컬렉션을 onSnapshot으로 실시간 감시하며, 에러(mismatch/alert) 트레이 목록만 노출합니다. 항목 클릭 시 Storage에 저장된 스냅샷 3장이 슬라이더 모달 형태로 노출.
- **Company View (`/`)**: 오늘 전체 검수 성공률을 대형 숫자로 시각화하며, Chart.js를 연동하여 시간대별 검수 처리량(Throughput)을 막대 차트로 표시.

==================================================
# 개발 및 실행 가이드 (Quick Start)
==================================================

### 가상 환경 셋업
```bash
chmod +x setup.sh
./setup.sh
```

### Mac 로컬 시뮬레이션 실행
```bash
docker compose -f docker-compose.mac.yml up -d --build
```

### 테스트용 curl 명령어 예측
```bash
# Gateway(Module B) 상태 확인
curl http://localhost:8000/health

# Inference(Module A) 백엔드 단일 접근
docker exec inference_agent curl -s -X POST http://localhost:8001/inference -F "image=@/path/test.jpg"
```

---

## 8. Physical Deployment (RPi5 + Hailo-8)

### 사전 조건 (Prerequisites)
- **OS**: Raspberry Pi OS (64-bit, Debian Bookworm 기반)
- **Hardware**: Raspberry Pi 5 (8GB 권장), Hailo-8 M.2 HAT+
- **Storage**: 32GB+ MicroSD 또는 NVMe SSD (여유 공간 10GB 이상)
- **Network**: 인터넷 연결 필수 (apt 패키지 및 Docker 이미지 다운로드)
- **사용자 권한**: sudo 권한이 있는 일반 사용자 (root 직접 실행 금지)

### 스크립트 목록 (`scripts/` 디렉터리)
| 스크립트 | 역할 | 재부팅 필요 |
|---|---|---|
| `check_system.sh` | 사전/사후 점검 (7개 카테고리) | ❌ |
| `setup_hailo.sh` | Hailo-8 드라이버 설치 (hailo-all, DKMS, udev) | ✅ |
| `setup_docker.sh` | Docker CE + Compose 설치 및 데몬 최적화 | ❌ |
| `optimize_rpi5.sh` | RPi5 성능 최적화 (CPU, PCIe, 커널 파라미터) | ✅ |

### 배포 순서 (Step-by-Step)

```bash
# 1. 스크립트 실행 권한 부여
chmod +x scripts/*.sh

# 2. 사전 점검 (현재 상태 확인)
./scripts/check_system.sh

# 3. Hailo-8 드라이버 설치
./scripts/setup_hailo.sh
# → 완료 후 sudo reboot

# 4. (재부팅 후) Hailo-8 장치 확인
ls -la /dev/hailo0
hailortcli fw-control identify

# 5. Docker 설치
./scripts/setup_docker.sh
# → 로그아웃 후 재로그인 (docker 그룹 적용)

# 6. RPi5 성능 최적화
./scripts/optimize_rpi5.sh
# → 완료 후 sudo reboot

# 7. (재부팅 후) 최종 점검
./scripts/check_system.sh
# → FAIL 0건이면 배포 준비 완료

# 8. 시스템 시작
docker compose up -d --build
```

### 핵심 커널 파라미터 (Key Kernel Parameters)

#### `/boot/firmware/config.txt`
| 파라미터 | 값 | 효과 |
|---|---|---|
| `arm_boost` | `1` | CPU 터보 활성화 (2.4GHz) |
| `over_voltage_delta` | `50000` | +50mV 전압 부스트 (터보 안정성) |
| `gpu_mem` | `64` | VideoCore 메모리 최소화 (추론 RAM 확보) |
| `dtparam=pciex1_gen` | `3` | PCIe Gen3 (5GT/s, Hailo-8 최대 대역폭) |

#### `/boot/firmware/cmdline.txt` (한 줄에 추가)
| 파라미터 | 효과 |
|---|---|
| `pcie_aspm=off` | PCIe Active State Power Management 비활성화 (추론 레이턴시 ~1ms 감소) |
| `usbcore.autosuspend=-1` | USB 오토서스펜드 비활성화 (카메라 안정성) |

#### `/etc/sysctl.d/99-surgicalai.conf`
| 파라미터 | 값 | 효과 |
|---|---|---|
| `vm.swappiness` | `5` | 스왑 최소화 (ML 워크로드) |
| `fs.file-max` | `131072` | 파일 디스크립터 한도 (Docker + Hailo + Camera) |
| `kernel.shmmax` | `536870912` | Hailo SDK POSIX SHM 공유 메모리 (512MB) |

### docker-compose.yml cgroup 규칙 업데이트

`/dev/hailo0`의 실제 메이저 번호는 커널 버전마다 다를 수 있습니다.

```bash
# 실제 메이저 번호 확인
stat -c '%t' /dev/hailo0 | xargs -I{} printf '%d\n' 0x{}

# docker-compose.yml에 반영 (예: 메이저 번호가 235인 경우)
# device_cgroup_rules:
#   - "c 235:* rmw"
```

### Firebase 프로덕션 모드 설정

실제 Firestore/Storage에 데이터를 기록하려면 서비스 계정 키 파일이 필요합니다.

```bash
# 1. Firebase 콘솔에서 서비스 계정 키 다운로드
#    Firebase Console → 프로젝트 설정 → 서비스 계정 → 새 비공개 키 생성

# 2. 키 파일을 프로젝트 루트에 복사
cp ~/Downloads/firebase-service-account.json ./firebase-credentials.json

# 3. .env 파일에 경로 설정
echo "FIREBASE_CREDENTIALS_PATH=/app/firebase-credentials.json" >> .env

# 4. docker-compose.yml에 볼륨 마운트 확인
#    volumes:
#      - ./firebase-credentials.json:/app/firebase-credentials.json:ro
```

### 배포 후 검증 명령어

```bash
# NPU 상태 및 온도 확인
hailortcli fw-control identify
hailortcli monitor  # Ctrl+C로 종료

# CPU 주파수 확인 (2400000 = 2.4GHz가 정상)
cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_cur_freq
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor  # → performance

# 컨테이너 상태 확인
docker compose ps

# 전체 파이프라인 E2E 테스트
curl -X POST http://localhost:8000/job \
  -H "Content-Type: application/json" \
  -d '{"job_id":"DEPLOY-TEST-001","target":{"scalpel":1}}'

# NPU 통계 확인
curl http://localhost:8001/metrics
```

### 🔄 Mac → RPi 코드 동기화 워크플로우

Mac에서 편집한 파일을 RPi에 반영할 때 반드시 아래 순서를 따를 것:

```bash
# 1. Mac → RPi 파일 전송 (Mac 터미널에서 실행)
ssh digioptics_am01@192.168.0.4 "mkdir -p ~/SurgicalAI01/src/<module>"
scp /Users/shawnshlee/1_Antigravity/SurgicalAI01/src/<module>/main.py \
  digioptics_am01@192.168.0.4:~/SurgicalAI01/src/<module>/main.py

# 2. RPi → 컨테이너 적용 (RPi 터미널에서 실행)
docker cp ~/SurgicalAI01/src/<module>/main.py <container_name>:/app/src/<module>/main.py
docker restart <container_name>

# 3. 영구 반영 (이미지 재빌드) — docker compose up -d 시 초기화되지 않도록
cd ~/SurgicalAI01
docker compose build --no-cache <service_name>
docker compose up -d <service_name>
```

**중요**: `docker cp`로 적용한 변경은 `docker compose up -d`로 컨테이너가 재생성되면 사라진다. 반드시 이미지 재빌드로 영구 반영할 것.

### 🛠 주요 트러블슈팅 로그 (Troubleshooting Ledger)

1. **PCIe 미인식 (`/dev/hailo0` 없음)**: `/boot/firmware/config.txt`에서 불완전한 `dtparam=pciex1=` 오타 제거 후 `dtparam=pciex1` 및 `dtparam=pciex1_gen=3` 명시하여 해결.
2. **Inference Agent 권한 오류**: `Dockerfile.inference`를 수정하여 `root` 사용자로 실행하고 홈 디렉토리를 생성하여 HailoRT 로그 및 디바이스 접근 권한 해결.
3. **`HAILO_OUT_OF_PHYSICAL_DEVICES`**: 온도 모니터링 스레드가 SDK를 통해 중복 접근하는 것을 방지하기 위해 `sysfs` 직접 읽기 방식으로 수정.
4. **HDMI 오버레이 미출력**: RPi OS Bookworm의 보안 정책 해결을 위해 호스트에서 `xhost +local:` 명령 실행 후 Display Agent 재시작.
5. **HUD 문자 깨짐 (???)**: OpenCV 기본 폰트의 Unicode 미지원으로 인해 `◈`를 ASCII `[+]`로 대체하여 해결.
6. **실시간성 부족**: Gateway Agent를 수동 요청 기반에서 자율 `_counting_loop` (Pull-based) 방식으로 전환하여 Job 활성화 시 즉시 실시간 추론 수행.
7. **컨테이너 코드 동기화**: 로컬 개발 시 소스 변경이 즉시 반영되도록 `docker-compose.mac.yml`에 볼륨 마운트 (`./src:/app/src`) 적용.
8. **SurgeoNet 통합 준비**: SurgeoNet의 14개 클래스(`Overholt Clamp`, `Scalpel` 등)를 `DEFAULT_CLASS_NAMES` 및 `labels.json`에 반영하여 추론 결과 연동 준비 완료. (Class 0 Background 필터링 포함)
9. **`SyntaxError: name 'current_job' is used prior to global declaration`**: `_counting_loop` 함수 내 `global` 선언이 함수 중간(~line 403)에 있어 루프 상단에서 `current_job`을 먼저 사용한 것이 원인. `global` 선언 전체를 함수 최상단(docstring 바로 아래)으로 이동하여 해결.
10. **`docker cp` 후에도 구버전 실행**: RPi의 `~/SurgicalAI01/src/` 파일이 Mac과 별도로 관리됨. `docker cp ~/SurgicalAI01/src/...`는 RPi 로컬 파일을 복사하므로 Mac에서 수정한 내용이 반영 안 됨. 반드시 Mac → RPi SCP 후 docker cp 실행할 것.
11. **camera_agent 포트 외부 접근 불가 (HTTP 000)**: `camera_agent:8002`는 Docker 내부 네트워크에만 노출 (`expose`). 호스트에서 `curl localhost:8002`는 실패가 정상. 내부 테스트는 `docker exec gateway_agent curl http://camera_agent:8002/frame` 사용.
12. **카메라 재연결 후 camera_agent 인식 실패**: USB 카메라 재연결 시 `docker restart camera_agent` 필요. OpenCV VideoCapture는 시작 시점에 디바이스를 열므로 재시작 없이는 새 연결을 인식하지 못함.
13. **HDMI overlay silent death (service healthy, screen blank)**: `_render_loop()` had zero exception handling — a single numpy/OpenCV error during HUD rendering would silently kill the daemon thread. FastAPI `/health` kept responding normally, making it undetectable via container status alone. Fixed by adding try/except with consecutive error counter in `display/main.py`, and canvas bounds clamping in `display/hud.py` (`_panel_bg`, `_draw_status_text`).

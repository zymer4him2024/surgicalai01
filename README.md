# Surgical Tray Inspection System: Detailed Technical Specification

> [!NOTE]
> **Project Status**: ✅ **Fully Deployed on Raspberry Pi 5 + Hailo-8**
> - **Inference Mode**: HAILO NPU (YOLOv11)
> - **Autonomous Loop**: Gateway-driven real-time counting
> - **Display**: HDMI Native Overlay (X11) with ASCII HUD
> - **Backend**: Firebase Sync (Cloud Snapshots)

## 1. 전제 하드웨어 및 인프라 (Detailed Stack)
* **Edge Device**: Raspberry Pi 5 (8GB) - 64-bit OS.
* **AI Acceleration**: Hailo-8 M.2 Module (26 TOPS) - Docker Container 내에서 전 전용 드라이버로 제어.
* **Imaging**: 4K USB Camera (초점 거리 고정, 고휘도 LED 링라이트 장착 권장).
* **Network**: Wi-Fi 6 또는 Ethernet (Firebase 실시간 동기화용).

## 2. 모듈별 상세 설계 (Modular Architecture)

### 🏗️ Module A: AI Inference Container (The "Inference Engine")
이 모듈은 오직 "이미지를 받아서 숫자를 뱉는" 역할만 수행합니다.
* **구현 방식**: Docker 이미지 내에 `hailo_platform` 라이브러리와 FastAPI 서버 탑재.
* **주요 API 엔드포인트**:
    * `POST /inference`: 바이트 스트림 이미지를 받아 YOLOv11 모델로 추론 후 `{"forceps": 3, "scalpel": 1, ...}` 반환.
    * `GET /health`: NPU 상태 및 온도 모니터링.
* **기술적 특징**: 
    * **Shared Memory**: 대용량 영상 데이터 전송 시 지연을 줄이기 위해 메모리 맵핑(Shared Memory) 방식 고려 가능.
    * **Batching**: 여러 개의 기구가 겹쳐 있을 때를 대비한 Tiling(이미지 분할) 처리 로직 포함.

### 🛡️ Module B: Main Controller & QR Decoder (The "Orchestrator")
시스템의 상태 머신(State Machine) 및 자율 카운팅 루프를 관리합니다.
* **QR 처리**: OpenCV와 `pyzbar`를 별도 스레드로 돌려 실시간으로 QR 코드를 감시합니다.
* **Job Management**: 
    * QR 스캔 시: `current_job = {"id": "TRAY-123", "target": {"forceps": 2, "scissors": 1}}` 생성.
* **Autonomous Counting Loop**: 
    * Job이 활성화되면 Gateway가 직접 카메라 프레임을 가져와 AI 추론을 수행하고 Display Agent를 실시간 업데이트합니다. (Pull-based autonomous loop)

### 🖥️ Module C: HDMI Display & UI Overlay (The "Frontend")
HDMI로 출력되는 실시간 모니터링 화면을 렌더링합니다.
* **상태별 UI 시나리오**:
    * **READY (노란색)**: 화면 테두리에 20px 두께의 노란색 박스 생성. "Scanning... [Target: 5 items]" 텍스트 표시.
    * **MATCH (녹색)**: 수량 일치 시 테두리 녹색 전환. "PASS" 아이콘 및 카운팅 완료 사운드(옵션) 발생.
    * **ERROR (빨간색)**: 불일치 시 테두리 빨간색 점멸. 부족하거나 초과된 아이템 목록을 빨간색 글씨로 강조.
* **ASCII HUD**: OpenCV 폰트 제한을 고려하여 모든 특수 문자를 ASCII `[+]`로 대체하여 텍스트 깨짐 방지.

### 📊 Module D: Firebase Cloud Sync (The "Backend Liaison")
데이터의 영속성을 담당하며, 메인 로직의 성능에 영향을 주지 않도록 **비동기(Async)**로 동작합니다.
* **Firebase Firestore**: 검수 이력(Time, Tray ID, Result) 저장.
* **Firebase Storage**: 에러 발생 시 찍힌 3장의 스냅샷 업로드.
* **Snapshot Logic**: 
    * 에러 발생 5초 지속 시, 0.1s 간격으로 3장의 원본 프레임을 캡처하여 고해상도로 업로드.

## 3. Detailed Workflow (Sequence Diagram)
1. **System Init**: RPi 부팅 시 AI 컨테이너가 로드되고 NPU가 활성화됩니다.
2. **QR Trigger**: 작업자가 트레이 QR을 비추면 Main Controller가 가동됩니다. (UI: Yellow)
3. **Autonomous Check**: 
    * Gateway Agent가 자율적으로 카메라 프레임을 가공하여 실시간 카운팅을 수행합니다.
    * Target: 5, Actual: 5가 되는 순간 UI가 Green으로 바뀝니다.
4. **Error Handling**: 만약 5초 이상 수량이 맞지 않으면 UI는 Red를 유지하고, Cloud Module에 스냅샷 명령을 내립니다.
5. **Data Flush**: 새로운 QR이 스캔되면 이전 데이터는 Firebase로 최종 Push되고 메모리는 초기화됩니다.

## 4. Admin & Company Dashboard 기능 정의

현재 Firebase Hosting (`https://surgicalai01.web.app`)을 통해 Apple-style의 Vanilla HTML/CSS/JS 대시보드가 배포되어 가동 중입니다.

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
| QR Flash Indicator | **Display HUD** | QR 스캔 성공 시 하단 중앙에 "QR SCANNED" 배너 3초 표시. `flash_text` 필드가 `/hud` 엔드포인트에 추가됨. |
| QR Trigger | **Gateway** | `/job` 엔드포인트는 `_pending_preset`에 저장. QR 스캔 시 `_pending_preset → current_job` 전환하여 detection 시작. |
| One-click Launcher | **RPi Desktop** | `~/Desktop/SurgicalAI.desktop` 더블클릭으로 `xhost +local:` 및 `docker compose up -d` 자동 실행. |

## 4. Admin & Company Dashboard 기능 정의

현재 Firebase Hosting (`https://surgicalai01.web.app`)을 통해 Apple-style의 Vanilla HTML/CSS/JS 대시보드가 배포되어 가동 중입니다.

### 👤 Admin Dashboard (관리자용)
* **전체 공정 모니터링**: Firestore의 `sync_events` 기반 검수 이력 리스트 실시간 확인.
* **에러 분석 보드**: Red 상태(mismatch/alert)로 종료된 작업의 스냅샷 3장 모달 연동. AI 오판 및 실제 작업자 실수 판별용 뷰어.
* **Preset Control**: 수술 도구 세트 프리셋 구성 및 자동 순환 제어.

### 🏢 Company Dashboard (고객사용/Viewer)
* **검수 이력 통계**: 당일 기준 총 검수 수량 및 성공률 (Success Rate) 실시간 집계.
* **시각화 리포트**: Chart.js 기반 도넛 차트를 통한 직관적 현황 제공.

## 5. Core Engineering & Security Standards (Global)
This repository strictly enforces SOLID principles, TDD, and Docker container security. Code that relies heavily on global state or God-classes will fail code review. For the master rulebook governing AI agents, code formatting, and architecture decisions, please refer directly to the internal project metadata:
- 👉 **[CLAUDE.md](./CLAUDE.md)**
- 👉 **[GEMINI.md](./GEMINI.md)**

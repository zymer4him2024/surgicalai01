# Antigravity SurgicalAI01 — 시스템 Walkthrough

## 개요
Raspberry Pi 5 + Hailo-8 NPU 기반 수술 도구 AI 검수 시스템의 전체 흐름을 정리한 문서입니다.
4개의 독립적인 Docker 컨테이너가 내부 브리지 네트워크(`antigravity_bridge`)를 통해 통신합니다.

---

## 아키텍처 구성도

```
[QR 스캔 / 외부 클라이언트]
        │
        ▼
┌──────────────────────────────────┐
│  Module B: Gateway Agent         │  Port 8000 (외부 노출)
│  - QR 작업 등록 (POST /job)      │
│  - 추론 결과 State Machine        │
│  - READY / MATCH / ERROR 상태    │
│  - 5초 불일치 → 스냅샷 트리거    │
└───┬──────────────┬───────────────┘
    │              │
    ▼              ▼
┌──────────┐  ┌──────────────────────────────┐
│ Module A │  │  Module C: Display Agent      │
│ Inference│  │  - HUD 렌더링 (1920×1080)     │
│ Port 8001│  │  - 테두리: Yellow/Green/Red   │
│ YOLO 추론│  │  - /snapshot JPEG 스냅샷      │
└──────────┘  │  Port 8003 (내부 전용)        │
              └──────────────────────────────┘
                            │
              ┌─────────────▼───────────────┐
              │  Module D: Firebase Sync      │
              │  - SQLite 로컬 큐 (WAL 모드)  │
              │  - 스냅샷 3장 캡처 (노출 보정)│
              │  - Firebase Storage 업로드    │
              │  - Firestore 이력 기록        │
              │  Port 8004 (내부 전용)        │
              └──────────────────────────────┘
                            │
              ┌─────────────▼───────────────┐
              │  Firebase Cloud (Google)      │
              │  - Firestore: sync_events     │
              │  - Storage: snapshots/        │
              │  - Hosting: surgicalai01.web.app │
              └──────────────────────────────┘
```

---

## Step-by-Step 워크스루

### 1. 시스템 시작

```bash
# Mac 시뮬레이션 환경
docker compose -f docker-compose.mac.yml up -d --build

# 모든 컨테이너 healthy 확인
docker ps
```

5개 컨테이너가 healthy 상태로 기동됩니다:
- `gateway_agent` — 172.20.0.10:8000
- `inference_agent` — 172.20.0.11:8001
- `camera_agent` — 172.20.0.12:8002
- `display_agent` — 172.20.0.13:8003
- `firebase_sync_agent` — 172.20.0.14:8004

---

### 2. QR 스캔 — 작업 등록

수술 트레이의 QR 코드를 스캔하면 `POST /job`이 호출됩니다.

```bash
curl -X POST http://localhost:8000/job \
  -H "Content-Type: application/json" \
  -d '{"job_id": "TRAY-2026-001", "target": {"scalpel": 1, "scissors": 1}}'
```

응답:
```json
{"status": "ok", "job": {"id": "TRAY-2026-001", "target": {"scalpel": 1, "scissors": 1}}, "state": "READY"}
```

- 상태 → **READY** (노란 테두리 표시)

---

### 3. AI 추론 — MATCH 상태

카메라 프레임을 `POST /inference`로 전달하면:
1. Gateway → Inference Agent (YOLOv11 추론)
2. 탐지 결과와 target 비교
3. 일치 시 → MATCH 상태 + 초록 테두리

```bash
curl -X POST http://localhost:8000/inference \
  -F "image=@/path/to/frame.jpg"
```

응답:
```json
{
  "system_state": "MATCH",
  "actual_counts": {"scalpel": 1, "scissors": 1},
  "target": {"scalpel": 1, "scissors": 1},
  "inference_time_ms": 22.1
}
```

---

### 4. AI 추론 — 5초 불일치 → ERROR 상태

탐지 결과와 target이 5초 이상 불일치하면:
1. Gateway State Machine → **ERROR** 상태
2. Display Agent → 빨간 테두리
3. Firebase Sync Agent에 `POST /snap` 자동 전송
4. 3장 스냅샷 (0.1초 간격, 노출 보정 ×1.0/×0.65/×1.45) 캡처
5. Firebase Storage 업로드
6. Firestore `sync_events` 컬렉션에 이력 기록

```bash
# MISMATCH 작업 등록
curl -X POST http://localhost:8000/job \
  -H "Content-Type: application/json" \
  -d '{"job_id": "TRAY-ERROR-001", "target": {"needle_holder": 2}}'

# 추론 (불일치 시작)
curl -X POST http://localhost:8000/inference -F "image=@/path/to/frame.jpg"

# 5초 후 재추론 → ERROR 전환
sleep 5
curl -X POST http://localhost:8000/inference -F "image=@/path/to/frame.jpg"
# → system_state: "ERROR"
# → firebase_sync에 /snap 자동 호출
```

---

### 5. Firebase 업로드 확인

```bash
# 큐 상태 확인
curl http://localhost:8004/queue/status
# → {"total_done": N, "total_pending": 0, ...}

# 특정 이벤트 조회
curl http://localhost:8004/queue/item/{event_id}
# → {"firestore_doc_id": "sim_...", "storage_urls": [...]}
```

---

### 6. Display 스냅샷 확인 (HEADLESS 모드)

Mac에서는 OpenCV 창 없이 `/snapshot` API로 렌더링된 화면을 확인합니다.

```bash
# Gateway 컨테이너 내부에서 Display 스냅샷 가져오기
docker exec gateway_agent curl -s -o /tmp/snap.jpg http://display_agent:8003/snapshot
docker cp gateway_agent:/tmp/snap.jpg ./display_snap.jpg
```

---

### 7. 웹 대시보드 (Firebase Hosting)

URL: **https://surgicalai01.web.app**

#### Admin View
- Firestore `sync_events` 컬렉션에서 최신 100건 실시간 로드
- ERROR 항목은 `View Snaps` 버튼 활성화
- 클릭 시 Glassmorphism 모달에 스냅샷 3장 렌더링

#### Company View
- 오늘 하루 통계: 총 검수 건수, 성공률
- Chart.js Doughnut 차트 (Success vs Error)

> **참고**: Mac 시뮬레이션 모드에서는 실제 Firebase 자격증명이 없어 Firestore에 데이터가 기록되지 않습니다.
> RPi 5 실제 환경에서는 `.env`에 `FIREBASE_CREDENTIALS_PATH` 또는 `FIREBASE_CREDENTIALS_JSON`을 설정하세요.

---

## API 레퍼런스 요약

### Gateway Agent (Port 8000)

| Method | Endpoint | 설명 |
|--------|----------|------|
| `POST` | `/job` | QR 작업 등록 |
| `POST` | `/inference` | 이미지 추론 + State Machine |
| `GET` | `/health` | 전체 시스템 헬스 |
| `GET` | `/status` | 모듈별 상세 메트릭 |

### Inference Agent (Port 8001, 내부)

| Method | Endpoint | 설명 |
|--------|----------|------|
| `POST` | `/inference` | YOLO 추론 (multipart image) |
| `GET` | `/health` | NPU 상태 |
| `GET` | `/metrics` | 누적 통계 |

### Display Agent (Port 8003, 내부)

| Method | Endpoint | 설명 |
|--------|----------|------|
| `POST` | `/hud` | HUD 테두리 색상 업데이트 |
| `POST` | `/frame` | 프레임 + 탐지 결과 전달 |
| `GET` | `/snapshot` | 현재 화면 JPEG 반환 |
| `GET` | `/health` | 렌더링 FPS 포함 상태 |

### Firebase Sync Agent (Port 8004, Mac은 외부 노출)

| Method | Endpoint | 설명 |
|--------|----------|------|
| `POST` | `/sync` | 이벤트 큐 삽입 |
| `POST` | `/snap` | ERROR 상태 스냅샷 즉시 트리거 |
| `GET` | `/queue/status` | 큐 깊이 + Firebase 도달 가능 여부 |
| `GET` | `/queue/item/{id}` | 개별 이벤트 상태 + doc_id |
| `POST` | `/queue/flush` | 큐 즉시 처리 |
| `GET` | `/health` | 모듈 상태 |

---

## 주요 설계 결정

| 결정 | 이유 |
|------|------|
| Hailo 추론을 별도 **subprocess**로 분리 | 메모리 누수 격리, 드라이버 상태 오염 방지 |
| Display에 **더블 버퍼링** 적용 | 30fps 렌더링에서 화면 깜빡임 제거 |
| Firebase Sync에 **SQLite 큐** 사용 | 오프라인 내성 — 네트워크 단절 시 데이터 유실 방지 |
| Gateway에 **FastAPI BackgroundTasks** 사용 | asyncio.create_task 대비 안정적인 배경 작업 실행 |
| 스냅샷 3장 **노출 보정** (×1.0/×0.65/×1.45) | 밝기 조건 불확실 시 최적 이미지 확보 |
| Firebase 자격증명 **환경변수 전용** | 코드/이미지에 절대 포함하지 않음 |

---

## 실행 테스트 결과 (2026-03-01)

| 항목 | 결과 |
|------|------|
| 전체 컨테이너 헬스 | ✅ 5개 모두 healthy |
| Gateway POST /job | ✅ READY 상태 설정 |
| Gateway POST /inference (MATCH) | ✅ system_state: MATCH, ~22ms |
| 5초 불일치 → ERROR 전환 | ✅ system_state: ERROR |
| Firebase /snap 자동 트리거 | ✅ BackgroundTasks 정상 동작 |
| 스냅샷 3장 노출 검증 | ✅ under=14.2 < std=22.8 < over=31.8 |
| pytest 5/5 통과 | ✅ (컨테이너 내부 실행) |
| Display /snapshot | ✅ 67.4KB 유효 JPEG |
| Firebase Hosting | ✅ https://surgicalai01.web.app |

---

## 다음 단계 (TODO)

- [x] Main Controller에 실제 QR 코드 파싱 (pyzbar / zxing) 연동
- [x] Inference Agent에 YOLOv11 HEF 연동 및 실기기 검증
- [x] Firebase 자격증명 설정 후 실제 Firestore/Storage 업로드 검증
- [x] RPi 5 실제 하드웨어 배포 및 Hailo-8 드라이버 연동
- [x] Preset Cycle 및 자동 전환 로직 구현
- [x] QR 스캔 성공 배너 (Flash HUD) 구현
- [x] 1-Click Launcher (Desktop Entry) 생성
- [x] Admin/Company 대시보드 UI 현대화 (Apple-style Glassmorphism)
- [ ] SurgeoNet (Pose) 모델 최종 최적화 및 현장 테스트
- [ ] Firestore Security Rules 세분화 (Admin vs User)

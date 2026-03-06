# Device Master Agent — Internal Integration Guide

## 목적
수술 기구 AI 탐지 시스템이 YOLO 탐지 레이블(`forceps`, `scalpel` 등)을
FDA 공식 표준 기기명 및 분류 코드와 매핑하기 위한 내부 서비스입니다.

실제 고객(병원/업체) 배포 시, 이 컨테이너를 고객사 내부 MDM(Master Data Management)
서버로 교체합니다. 교체는 환경변수 `DEVICE_MASTER_URL` 값만 변경하면 됩니다.

---

## 아키텍처

```
[Gateway Agent]
    │
    ├─ POST /inference 수신
    │       │
    │       ├─ Inference Agent → YOLO 탐지 결과 수신
    │       │
    │       └─ Device Master Agent: 레이블별 병렬 조회 (asyncio.gather)
    │               GET /device/lookup?label=forceps
    │               GET /device/lookup?label=scalpel
    │               (timeout=2s, fail-open)
    │
    ├─► Display Agent: enriched tray_items (device_name, fda_class 포함)
    └─► Firebase Sync: devices_resolved (ERROR 상태 스냅샷 시)
```

---

## 서비스 정보

| 항목 | 값 |
|---|---|
| 컨테이너명 | `device_master_agent` |
| IP | `172.20.0.15` |
| 포트 | `8005` (내부 전용, Mac 개발 시 외부 노출) |
| Dockerfile | `Dockerfile.device_master` |
| 요구사항 | `requirements.device_master.txt` |

---

## 데이터 소스: openFDA Device Classification API

- **엔드포인트**: `https://api.fda.gov/device/classification.json`
- **인증**: 불필요 (공개 API, rate limit: 240 req/min)
- **주요 조회 필드**:
  - `device_name`: 기기 명칭
  - `product_code`: FDA 3자리 제품 코드
  - `device_class`: 규제 등급 (1=Class I, 2=Class II, 3=Class III)
  - `medical_specialty_description`: 진료과 분류

**쿼리 예시**:
```
GET https://api.fda.gov/device/classification.json
  ?search=device_name:"forceps"
  &limit=5
```

**등급 우선순위**: Class I → II → III (낮은 등급 = 저위험 = 일반 수술 기구에 해당)

---

## 캐시 전략

| 단계 | 동작 |
|---|---|
| 컨테이너 시작 | `/app/data/device_cache.json` 존재 & 7일 이내 → 파일 로드 |
| 파일 없음 / 만료 | openFDA API 호출 → 결과 저장 |
| openFDA 실패 | `labels.json` fallback 값 사용 |
| 캐시 미스 (런타임) | openFDA 실시간 조회 (data_source="openfda_live") |

**캐시 TTL**: 기본 168시간 (7일) — `DEVICE_CACHE_TTL_HOURS` 환경변수로 조정
**강제 갱신**: `POST /device/refresh`

---

## 레이블 정규화 설정 (`src/device_master/labels.json`)

YOLO 레이블 → FDA 검색어 매핑 및 fallback 설정 파일입니다.
**새 YOLO 클래스 추가 시 코드 변경 없이 이 파일만 수정**합니다.

```json
{
  "forceps": {
    "fda_search_terms": ["forceps", "tissue forceps"],
    "fallback_name": "Surgical Forceps",
    "fallback_product_code": null,
    "fallback_class": "I"
  }
}
```

| 필드 | 설명 |
|---|---|
| `fda_search_terms` | openFDA 검색 시 순서대로 시도. 첫 번째 히트 사용. |
| `fallback_name` | openFDA 실패 시 표시할 기기명 |
| `fallback_product_code` | 알 수 없는 경우 null |
| `fallback_class` | fallback FDA 등급 |

---

## Gateway 연동 방식

`src/gateway/main.py` — `_enrich_with_device_info()` 함수:

```python
# 탐지된 모든 레이블에 대해 병렬 조회
enriched_items = await _enrich_with_device_info(actual_counts)
# → [{"class_name": "forceps", "count": 2,
#      "device_name": "Tissue Forceps, Ring",
#      "product_code": "GZY", "fda_class": "I"}, ...]
```

- **timeout=2s**: Device Master가 느리거나 다운되어도 inference 응답은 정상 반환 (fail-open)
- **devices_resolved**: ERROR 상태 스냅샷 트리거 시 `_trigger_snapshot(enriched_items)` 경유 Firebase 메타데이터에 포함

---

## API 엔드포인트

### `GET /device/lookup?label={label}`
```bash
curl "http://localhost:8005/device/lookup?label=forceps"
```
```json
{
  "yolo_label": "forceps",
  "device_name": "Tissue Forceps, Ring",
  "product_code": "GZY",
  "device_class": "I",
  "medical_specialty": "General Hospital",
  "data_source": "cache"
}
```

### `GET /device/labels`
```bash
curl "http://localhost:8005/device/labels"
```
```json
{
  "count": 6,
  "labels": {
    "forceps": {...},
    "scalpel": {...}
  }
}
```

### `POST /device/refresh`
```bash
curl -X POST "http://localhost:8005/device/refresh"
```
openFDA 재조회를 강제 실행합니다.

### `GET /health`
```bash
curl "http://localhost:8005/health"
```
```json
{
  "status": "healthy",
  "module": "DeviceMasterAgent",
  "cache": {
    "loaded": true,
    "label_count": 6,
    "cache_age_hours": 2.3,
    "openfda_reachable": true
  }
}
```

---

## 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `DEVICE_CACHE_PATH` | `/app/data/device_cache.json` | 캐시 파일 경로 |
| `DEVICE_CACHE_TTL_HOURS` | `168` | 캐시 만료 시간 (시간 단위) |
| `MODULE_NAME` | `DeviceMasterAgent` | 로그/헬스 체크용 모듈명 |

---

## 실제 고객 MDM으로 교체 방법

1. `docker-compose.yml`에서 `DEVICE_MASTER_URL` 값을 고객사 서버 URL로 변경:
   ```yaml
   - DEVICE_MASTER_URL=https://mdm.hospital.internal/api/v1
   ```
2. `device_master_agent` 서비스 블록 전체 제거 (선택)
3. 고객사 API가 `customer_api_spec.md`의 인터페이스를 구현하는지 확인

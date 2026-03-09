# Customer Device Master API — Integration Specification

**Version**: 1.0
**Issued by**: Antigravity Surgical AI
**Audience**: Hospital IT / Vendor MDM Team

---

## 목적

본 문서는 Antigravity Surgical AI 시스템이 귀사의 내부
의료기기 마스터 데이터(MDM) 시스템과 연동하기 위해
귀사 API 서버가 구현해야 할 인터페이스 명세입니다.

현재 저희 시스템은 공개 데이터(openFDA)를 기반으로 한
모의(mock) API 서버를 사용하고 있습니다.
실제 운영 배포 시, **환경변수 `DEVICE_MASTER_URL` 값만
귀사 서버 URL로 변경**하면 나머지 시스템은 변경 없이 동작합니다.

---

## 연동 구조

고객사 DB는 물리적 Edge Device(장비)와 직접 통신하지 않습니다.
Digioptics 클라우드(Application DB)가 중개자 역할을 수행합니다.

```
[Antigravity RPi5 Edge Device]
        │
        │  (Digioptics Internal API)
        ▼
[Digioptics Application DB (Cloud)]
        │
        │  HTTP/JSON (서버 대 서버 통신)
        ▼
[귀사 MDM API 서버 / Customer DB]  ← 본 문서의 대상
  GET /device/lookup?label=forceps
  GET /health
```

---

## 필수 구현 엔드포인트

### 1. `GET /device/lookup`

수술 기구 레이블에 대한 표준 기기 정보를 반환합니다.

#### 요청

| 파라미터 | 위치 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| `label` | Query String | string | ✅ | YOLO 탐지 레이블 (소문자 영문) |

예시:
```
GET /device/lookup?label=forceps
```

#### 응답 (200 OK)

```json
{
  "yolo_label": "forceps",
  "device_name": "Tissue Forceps, Ring",
  "product_code": "GZY",
  "device_class": "I",
  "medical_specialty": "General Surgery",
  "data_source": "mdm"
}
```

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `yolo_label` | string | ✅ | 요청한 레이블 그대로 반환 |
| `device_name` | string | ✅ | 표준 기기 명칭 |
| `product_code` | string \| null | ✅ | 내부 제품 코드 (모르면 null) |
| `device_class` | string \| null | ✅ | 규제 등급: `"I"`, `"II"`, `"III"`, null |
| `medical_specialty` | string \| null | ✅ | 진료과 분류 (모르면 null) |
| `data_source` | string | ✅ | 고정값 `"mdm"` (또는 귀사 시스템명) |

#### 오류 응답

| 코드 | 상황 |
|---|---|
| `404 Not Found` | 해당 레이블이 DB에 없음 |
| `503 Service Unavailable` | 서비스 일시 중단 |

> **참고**: 저희 시스템은 404 및 503 수신 시 원본 YOLO 레이블을 그대로
> 사용하며 검수 프로세스를 중단하지 않습니다 (fail-open 방식).

---

### 2. `GET /health`

서비스 가용 여부 확인용 헬스 체크 엔드포인트입니다.

#### 응답 (200 OK)

```json
{
  "status": "healthy"
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `status` | string | `"healthy"` 고정 |

> 저희 시스템은 30초 간격으로 이 엔드포인트를 호출합니다.
> 3회 연속 실패 시 장치 화면에 경고가 표시됩니다.

---

## 현재 저희 시스템이 처리하는 YOLO 레이블 목록

귀사 API가 아래 레이블들에 대한 조회를 지원해야 합니다.
모델이 업데이트되면 저희가 사전 통보합니다.

| YOLO 레이블 | 설명 |
|---|---|
| `forceps` | 포셉 (조직 파지용) |
| `scalpel` | 메스/수술용 나이프 |
| `scissors` | 수술용 가위 |
| `needle_holder` | 지침기 |
| `retractor` | 견인기 |
| `clamp` | 클램프/지혈겸자 |

---

## SLA 요구사항

| 항목 | 요구사항 |
|---|---|
| 응답 시간 | 2초 이내 (저희 게이트웨이 timeout 기준) |
| 가용성 | 99% 이상 권장 (다운 시 검수 프로세스는 계속 동작) |
| Rate Limit | 최대 10 req/sec (추론 1회당 최대 6 레이블 동시 요청) |

---

## 인증 (Authentication)

**현재 (모의 서버)**: 인증 없음

**실제 배포 시**: 저희가 API Key를 제공하면 귀사는 아래 방식으로 검증합니다.

```
Authorization: Bearer <API_KEY>
```

저희 시스템은 `DEVICE_MASTER_API_KEY` 환경변수에 키를 설정하며
모든 요청 헤더에 자동 포함합니다.

---

## 마이그레이션 절차

1. 귀사 MDM 팀이 위 두 엔드포인트를 구현합니다.
2. 저희에게 서버 Base URL 및 API Key를 전달합니다.
3. 저희 Digioptics 클라우드(Application DB) 백엔드에 해당 URL을 연동합니다.
   (Edge Device 코드는 수정되지 않습니다.)
4. 저희가 스테이징 환경에서 클라우드 간(S2S) E2E 연동 테스트를 수행합니다.
5. 검수 후 실 운영으로 전환합니다.

---

## 버전 관리

- 현재 버전: **v1.0**
- API 스키마 변경(Breaking Change) 시 **30일 전** 사전 통보
- 하위 호환 변경(Optional 필드 추가 등)은 즉시 적용 가능

---

## 문의

기술 연동 문의: Antigravity Surgical AI 개발팀
Firebase Hosting: `https://surgicalai01.web.app`

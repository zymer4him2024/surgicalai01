# Customer Device Master API — Integration Specification

**Version**: 1.0
**Issued by Digioptics: Digioptics Surgical AI
**Audience**: Hospital IT / Vendor MDM Team

---

## Purpose

This document provides the interface specifications that your API server must implement so that the Digioptics Surgical AI system can integrate with your internal Medical Device Master (MDM) data system.

Currently, our system uses a mock API server based on public data (openFDA).
For actual production deployment, you only need to change the **`DEVICE_MASTER_URL` environment variable to your server's URL**, and the rest of the system will operate without any changes.

---

## Integration Architecture

The customer's DB does not communicate directly with the physical Edge Device.
The Digioptics Cloud (Application DB) acts as an intermediary.

```text
[Antigravity RPi5 Edge Device]
        │
        │  (Digioptics Internal API)
        ▼
[Digioptics Application DB (Cloud)]
        │
        │  HTTP/JSON (Server-to-Server Communication)
        ▼
[Your MDM API Server / Customer DB]  ← Target of this Document
  GET /device/lookup?label=forceps
  GET /health
```

---

## Required Endpoints

### 1. `GET /device/lookup`

Returns standard device information for a given surgical instrument label.

#### Request

| Parameter | Location | Type | Required | Description |
|---|---|---|---|---|
| `label` | Query String | string | ✅ | AI detection label (lowercase English) |

Example:
```http
GET /device/lookup?label=forceps
```

#### Response (200 OK)

```json
{
  "detection_label": "forceps",
  "device_name": "Tissue Forceps, Ring",
  "product_code": "GZY",
  "device_class": "I",
  "medical_specialty": "General Surgery",
  "data_source": "mdm"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `detection_label` | string | ✅ | The requested label returned as-is |
| `device_name` | string | ✅ | Standard device name |
| `product_code` | string \| null | ✅ | Internal product code (null if unknown) |
| `device_class` | string \| null | ✅ | Regulatory class: `"I"`, `"II"`, `"III"`, or null |
| `medical_specialty` | string \| null | ✅ | Medical specialty classification (null if unknown) |
| `data_source` | string | ✅ | Fixed value `"mdm"` (or your system's name) |

#### Error Responses

| Code | Situation |
|---|---|
| `404 Not Found` | The requested label is not found in the DB |
| `503 Service Unavailable` | Service temporarily suspended |

> **Note**: Upon receiving a 404 or 503 error, our system will use the original detection label as-is and will not interrupt the inspection process (fail-open operational design).

---

### 2. `GET /health`

Health check endpoint to verify service availability.

#### Response (200 OK)

```json
{
  "status": "healthy"
}
```

| Field | Type | Description |
|---|---|---|
| `status` | string | Fixed as `"healthy"` |

> Our system will call this endpoint every 30 seconds.
> If it fails 3 consecutive times, a warning will be displayed on the device screen.

---

## Detection Labels Currently Handled By Our System

Your API must support queries for the labels listed below.
We will notify you in advance if the model is updated with new labels.

| Detection Label | Description |
|---|---|
| `forceps` | Forceps (Tissue grasping) |
| `scalpel` | Scalpel / Surgical Knife |
| `scissors` | Surgical Scissors |
| `needle_holder` | Needle Holder |
| `retractor` | Retractor |
| `clamp` | Clamp / Hemostat |

---

## SLA Requirements

| Category | Requirement |
|---|---|
| Response Time | Within 2 seconds (based on our Gateway timeout) |
| Availability | 99% or higher recommended (inspection process continues even during downtime) |
| Rate Limit | Max 10 req/sec (up to 6 labels requested simultaneously per inference cycle) |

---

## Authentication

**Current (Mock Server)**: No authentication

**Production Deployment**: We will provide an API Key, which you must use for verification via the following format:

```http
Authorization: Bearer <API_KEY>
```

Our system automatically includes this key in all request headers by reading the `DEVICE_MASTER_API_KEY` environment variable.

---

## Migration Procedure

1. Your MDM team implements the two endpoints above.
2. Provide your server Base URL and API Key to us.
3. We will link that URL to our Digioptics Cloud (Application DB) backend.
   *(No code changes are required on the Edge Device).*
4. We will perform an End-to-End Server-to-Server (S2S) integration test in the staging environment.
5. Upon successful verification, we will migrate it to the production environment.

---

## Versioning

- Current Version: **v1.0**
- Breaking changes to the API schema require a minimum **30-day advance notice**.
- Backward-compatible changes (e.g., adding optional fields) can be applied immediately.

---

## Contact

Technical Integration Inquiries: Antigravity Surgical AI Development Team  
Firebase Hosting: `https://surgicalai01.web.app`

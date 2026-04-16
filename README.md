# SurgicalAI01

> Edge AI surgical instrument counting system built on Raspberry Pi 5 + Hailo-8 NPU.
> Multi-agent microservices with real-time Firestore-driven state machine and HDMI HUD overlay.

![Platform](https://img.shields.io/badge/platform-Raspberry%20Pi%205-c51a4a)
![AI](https://img.shields.io/badge/AI-Hailo--8%20NPU%2026%20TOPS-blueviolet)
![Inference](https://img.shields.io/badge/inference-YOLOv11%20%2F%20SurgeoNet-blue)
![Backend](https://img.shields.io/badge/backend-Firebase%20Firestore-orange)
![Container](https://img.shields.io/badge/runtime-Docker%20Compose-2496ed)
![License](https://img.shields.io/badge/license-Proprietary-lightgrey)

---

## Overview

SurgicalAI01 is a production-deployed, edge-native surgical tray inspection system. A QR scan triggers an autonomous counting loop: the gateway agent pulls camera frames, runs hardware-accelerated YOLOv8/YOLOv11 inference on the Hailo-8 NPU, evaluates counts against a preset target, and drives an HDMI HUD overlay — all within a Docker Compose microservice mesh running on a Raspberry Pi 5.

Errors automatically trigger asynchronous Firebase Storage snapshot uploads with full Firestore audit trail. A web dashboard (Firebase Hosting) provides admin monitoring and company-facing pass-rate reporting.

**Live deployment**: `https://surgicalai01.web.app`

---

## Architecture

```mermaid
graph TD
    QR[QR Code Scan] --> GW[gateway_agent :8000]
    GW -->|POST /inference| INF[inference_agent :8001\nHailo-8 NPU]
    GW -->|GET /frame| CAM[camera_agent :8002\n4K USB Camera]
    GW -->|POST /hud| DSP[display_agent :8003\nHDMI HUD]
    GW -->|async| FB[firebase_sync_agent :8004\nFirestore + Storage]
    GW -->|catalog lookup| DM[device_master_agent :8005\nFDA Label Mapping]
    GW -.->|3rd-party adapter| MOCK[mock_external_ai :8006\nAir-gapped Network]
    FB --> CLOUD[(Firebase Cloud\nFirestore / Storage)]
    CLOUD --> DASH[Web Dashboard\nsurgicalai01.web.app]
```

### Network Topology

| Bridge | Subnet | Purpose |
|--------|--------|---------|
| `antigravity_bridge` | `172.20.0.0/16` | Internal service mesh — all Antigravity containers |
| `isolated_ai_bridge` | `172.20.1.0/24` | Air-gapped (`internal: true`) — 3rd-party AI containers |
| `gas_bridge` | `172.21.0.0/16` | Gas cylinder inventory application (separate RPi) |

---

## Agent Roles

| Agent | Container | Port | Responsibility |
|-------|-----------|------|----------------|
| **Gateway** | `gateway_agent` | 8000 | QR decode, state machine (READY → MATCH → ERROR), autonomous counting loop |
| **Inference** | `inference_agent` | 8001 | Hailo-8 NPU inference — YOLOv11 / SurgeoNet (14-class surgical tools) |
| **Camera** | `camera_agent` | 8002 | 4K USB frame capture (internal only) |
| **Display** | `display_agent` | 8003 | HDMI HUD overlay — double-buffered, bounding boxes, status borders |
| **Firebase Sync** | `firebase_sync_agent` | 8004 | Async Firestore history + Storage snapshot upload on error |
| **Device Master** | `device_master_agent` | 8005 | FDA label mapping: `forceps` → `Tissue Forceps, Ring (FDA Class I)` |
| **Mock External AI** | `mock_external_ai` | 8006 | Simulated 3rd-party inference for adapter integration testing |

### State Machine

```
[IDLE] ──QR scan──→ [READY / Yellow] ──count match──→ [MATCH / Green]
                          │                                    │
                     5s mismatch                         auto-advance
                          │                                    │
                          └──────────→ [ERROR / Red] ←─────────┘
                                             │
                                      async snapshot
                                      → Firebase Storage
```

---

## Tech Stack

- **Edge device**: Raspberry Pi 5 (8GB), 64-bit OS
- **AI accelerator**: Hailo-8 M.2 (26 TOPS), PCIe Gen3
- **Models**: YOLOv11 / SurgeoNet (14 surgical instrument classes), CONF_THRESHOLD=0.35
- **Inference runtime**: HailoRT SDK inside Docker (`/dev/hailo0` device passthrough)
- **Orchestration**: Docker Compose, `antigravity_bridge` internal network
- **Backend**: Firebase Firestore (state, audit log), Firebase Storage (error snapshots)
- **Dashboard**: Firebase Hosting — Vanilla JS + Tailwind CSS + Chart.js
- **Language**: Python 3.11, FastAPI, OpenCV
- **Quality**: Ruff (lint/format), Pyright (type check), pytest (TDD)

---

## Quick Start

### Mac (Simulation Mode)

```bash
docker compose -f docker-compose.mac.yml up -d --build
```

### Raspberry Pi 5 + Hailo-8 (Production)

```bash
# 1. Install Hailo-8 driver and Docker
chmod +x scripts/*.sh
./scripts/setup_hailo.sh   # reboot after
./scripts/setup_docker.sh

# 2. Optimize RPi5 performance
./scripts/optimize_rpi5.sh  # reboot after

# 3. Pre-flight check (expect 0 FAILs)
./scripts/check_system.sh

# 4. Launch
docker compose up -d --build
```

### E2E Test (Post-Deploy Verification)

```bash
# Trigger a test job
curl -X POST http://localhost:8000/job \
  -H "Content-Type: application/json" \
  -d '{"job_id":"DEPLOY-TEST-001","target":{"scalpel":1}}'

# Check system health
curl http://localhost:8000/health
curl http://localhost:8001/metrics
```

---

## Multi-Application Support (`APP_ID`)

The same codebase runs multiple deployments via the `APP_ID` environment variable:

| APP_ID | Application | Network | Notes |
|--------|-------------|---------|-------|
| `surgical` | Surgical instrument counting | `antigravity_bridge` | Primary — RPi5 |
| `inventory_count` | Gas cylinder inventory (Bringel) | `gas_bridge` | Separate RPi, ports 8010/8013 |

---

## Firebase Production Setup

```bash
# Download service account key from Firebase Console
cp ~/Downloads/firebase-service-account.json ./firebase-credentials.json
echo "FIREBASE_CREDENTIALS_PATH=/app/firebase-credentials.json" >> .env
```

---

## Documentation

| Document | Description |
|----------|-------------|
| [CLAUDE.md](./CLAUDE.md) | Agentic OS config — architecture, dev commands, design rules |
| [docs/rpi_onboarding_guide.md](./docs/rpi_onboarding_guide.md) | RPi5 setup and SSH troubleshooting |
| [docs/integration_architecture.md](./docs/integration_architecture.md) | Full system integration spec |
| [docs/3rd_party_ai_inference_spec.md](./docs/3rd_party_ai_inference_spec.md) | 3rd-party AI adapter protocol |
| [docs/customer_api_spec.md](./docs/customer_api_spec.md) | Device Master API reference |
| [GEMINI.md](./GEMINI.md) | Gemini AI context (mirrors CLAUDE.md) |

---

## Engineering Standards

This project enforces SOLID principles, TDD, and container security boundaries.

- **No God classes.** Each agent has exactly one responsibility.
- **No global state leakage** across container boundaries — all inter-agent communication via HTTP on the internal bridge.
- **Async-first** for any I/O that doesn't affect the counting loop (Firebase writes, snapshot uploads).
- **Hardware isolation**: Hailo-8 device access restricted to `inference_agent` via `device_cgroup_rules`.
- Code review authority: **[CLAUDE.md](./CLAUDE.md)**

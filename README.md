# SurgicalAI01

![Edge AI](https://img.shields.io/badge/Edge%20AI-Hailo--8%2026TOPS-blue)
![Platform](https://img.shields.io/badge/Platform-Raspberry%20Pi%205-red)
![Runtime](https://img.shields.io/badge/Runtime-Docker%20Compose-informational)
![Status](https://img.shields.io/badge/Status-Production%20Deployed-brightgreen)
![License](https://img.shields.io/badge/License-MIT-green)

Production-deployed surgical instrument counting system running on Raspberry Pi 5 with Hailo-8 NPU (26 TOPS). A multi-agent microservices platform that performs real-time YOLOv11 inference on surgical trays, enforces count verification against pre-configured instrument sets, and syncs audit records to Firebase asynchronously.

> Live dashboard: `https://surgicalai01.web.app`

---

## Architecture

```
┌─────────────────────────── antigravity_bridge 172.20.0.0/16 ──────────────────────────┐
│                                                                                        │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐         │
│  │ camera_agent │    │inference_agent│   │ display_agent│    │firebase_sync │         │
│  │   :8002      │───▶│   :8001      │    │   :8003      │    │   :8004      │         │
│  │ 4K USB Cam   │    │ Hailo-8 NPU  │    │ HDMI HUD     │    │ Async Sync   │         │
│  └──────┬───────┘    └──────┬───────┘    └──────▲───────┘    └──────▲───────┘         │
│         │                   │                   │                   │                 │
│         └───────────────────▼───────────────────┤                   │                 │
│                    ┌─────────────────┐           │                   │                 │
│                    │  gateway_agent  │───────────┴───────────────────┘                 │
│                    │     :8000       │  Orchestrator — state machine + counting loop   │
│                    │  QR → READY →   │                                                 │
│                    │  COUNT → MATCH  │                                                 │
│                    └────────┬────────┘                                                 │
│                             │                                                          │
│                    ┌────────▼────────┐                                                 │
│                    │ device_master   │  FDA label mapping + customer catalog           │
│                    │   :8005         │                                                 │
│                    └─────────────────┘                                                 │
└────────────────────────────────────────────────────────────────────────────────────────┘
                             │
                    ┌────────▼────────┐         ┌─────────────────────┐
                    │    Firebase     │────────▶ │  Web Dashboard      │
                    │  Firestore +    │          │  surgicalai01.web.app│
                    │   Storage       │          │  (Admin + Company)  │
                    └─────────────────┘          └─────────────────────┘
```

### Agent Roster

| Agent | Container | IP | Port | Role |
|---|---|---|---|---|
| Gateway | `gateway_agent` | 172.20.0.10 | 8000 | Orchestrator, QR decode, autonomous counting loop |
| Inference | `inference_agent` | 172.20.0.11 | 8001 | Hailo-8 YOLOv11 — 14-class surgical instrument detection |
| Camera | `camera_agent` | 172.20.0.12 | 8002 | 4K USB camera frame capture |
| Display | `display_agent` | 172.20.0.13 | 8003 | HDMI HUD, bounding box overlay, double-buffered rendering |
| Firebase Sync | `firebase_sync_agent` | 172.20.0.14 | 8004 | Async Firestore write + error snapshot upload |
| Device Master | `device_master_agent` | 172.20.0.15 | 8005 | FDA instrument label mapping, customer catalog bridge |

---

## State Machine

The Gateway Agent runs a pull-based autonomous loop. No external trigger is needed once a job is active.

```
IDLE
  │  (QR scan detected)
  ▼
READY  ── yellow HUD border ── "Scan QR to Begin"
  │  (job loaded from Firestore preset)
  ▼
COUNTING  ── live inference every frame ── bounding boxes on HDMI
  │
  ├── count == target  ──▶  MATCH  (green HUD, Firebase sync, 5s auto-advance)
  │
  └── count ≠ target for 5s  ──▶  ERROR  (red blinking HUD, 3-snapshot upload to Storage)
                                      │
                                      ▼
                                  WAIT_CLEAR  ── waits for empty tray ──▶  IDLE
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| Edge hardware | Raspberry Pi 5 (8GB) + Hailo-8 M.2 HAT+ (26 TOPS) |
| Inference model | YOLOv11 / SurgeoNet — 14 surgical instrument classes |
| Agent runtime | Python 3.11, FastAPI, Docker Compose |
| Inter-agent comms | HTTP/JSON over Docker bridge network |
| State store | Firebase Firestore (`job_config/{deviceId}`, `sync_events`) |
| Media storage | Firebase Storage (error snapshots, 3 frames on mismatch) |
| HDMI display | OpenCV X11 overlay, double-buffered, 15 FPS |
| Web dashboard | Vanilla HTML/CSS/JS + Firebase v10 SDK |
| Code quality | Ruff (lint/format), Pyright (type check), pytest (TDD) |

---

## Quick Start

### Mac — Simulation Mode
```bash
docker compose -f docker-compose.mac.yml up -d --build
```

### Raspberry Pi 5 — Production
```bash
# First-time setup (run once, requires reboots between steps)
chmod +x scripts/*.sh
./scripts/setup_hailo.sh    # Hailo-8 driver — reboot after
./scripts/setup_docker.sh   # Docker CE — re-login after
./scripts/optimize_rpi5.sh  # RPi5 perf tuning — reboot after

# Start system
docker compose up -d --build
```

### Health Check
```bash
curl http://localhost:8000/health
curl http://localhost:8000/status
```

### Trigger Manual Job
```bash
curl -X POST http://localhost:8000/job \
  -H "Content-Type: application/json" \
  -d '{"job_id":"TRAY-001","target":{"scalpel":1,"forceps":2}}'
```

---

## Extended Application: Gas Cylinder Inventory

A second application (`APP_ID=inventory_count`) for gas cylinder counting runs on a separate RPi using the same agent architecture on an isolated `gas_bridge` (172.21.0.0/16) network.

```bash
docker compose -f docker-compose.gas.yml up -d --build      # RPi
docker compose -f docker-compose.gas.mac.yml up -d --build  # Mac simulation
```

---

## Network Isolation for Third-Party AI

Third-party AI containers are confined to `isolated_ai_bridge` (172.20.1.0/24, `internal: true`) — no outbound internet access. The Gateway is dual-homed and includes a `_normalize_inference_response` adapter to translate external schemas.

---

## Documentation

| Document | Description |
|---|---|
| [CLAUDE.md](./CLAUDE.md) | Full AI assistant context, design decisions, troubleshooting ledger |
| [docs/rpi_onboarding_guide.md](./docs/rpi_onboarding_guide.md) | RPi5 + Hailo-8 first-time deployment |
| [docs/integration_architecture.md](./docs/integration_architecture.md) | System integration specification |
| [docs/3rd_party_ai_inference_spec.md](./docs/3rd_party_ai_inference_spec.md) | External AI adapter protocol |
| [docs/device_master_catalog_spec.md](./docs/device_master_catalog_spec.md) | FDA instrument catalog and mapping |

---

## Engineering Standards

- SOLID principles — no God-classes, no global state
- TDD with pytest — business logic requires tests
- All secrets via `.env` — never committed to version control
- Docker security: non-root users, scoped `device_cgroup_rules` for Hailo-8 only
- Firestore rules enforce Google authentication on all dashboard access
- CLAUDE.md and GEMINI.md maintained in sync as AI assistant context

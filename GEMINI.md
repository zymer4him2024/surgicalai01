# Antigravity Surgical AI - Global Environment & Design Decisions

## 1. Overview
This document records global environment settings and key design decisions for the AI object detection and counting system built on Raspberry Pi 5 and Hailo-8.
CLAUDE.md and GEMINI.md maintain identical content for context synchronization across AI assistants.

---

## 2. Hardware Environment
- **Edge Device**: Raspberry Pi 5 (8GB) - 64-bit OS.
- **AI Acceleration**: Hailo-8 AI Accelerator (e.g., M.2 Module or AI Kit) - controlled via dedicated driver inside Docker container.
- **Imaging**: 4K USB Camera (fixed focal length, high-brightness LED ring light recommended).
- **Network**: Wi-Fi 6 or Ethernet (for Firebase real-time sync).

---

## 3. Software Environment
- **Python**: 3.10+ (3.11 recommended)
- **Virtual Environment**: `venv` (path: `venv/`)
- **Container Environment**: Docker Compose
- **Package Management & Formatting**: `pyproject.toml`
  - **Linting & Formatting**: Ruff
  - **Type Checking**: Pyright
- **Testing**: pytest (TDD & SOLID principles)
- **Firebase Hosting**: `https://surgicalai01.web.app` (deployed)

---

## 4. Network & Port Design
All modules communicate via the Docker internal bridge network.

- **Network Name**: `antigravity_bridge`
- **Subnet**: `172.20.0.0/16`

### Module IP & Port Assignments
| Module | Container Name | Fixed IP | Port | External | Description |
|---|---|---|---|---|---|
| Module B (Main) | `gateway_agent` | `172.20.0.10` | `8000` | `localhost:8000` | Main controller, QR decode, state machine |
| Module A (Inference) | `inference_agent` | `172.20.0.11` | `8001` | Internal only | Hailo-8 inference (SurgeoNet YOLOv8m) |
| Camera Agent | `camera_agent` | `172.20.0.12` | `8002` | Internal only | 4K camera frame capture |
| Module C (Display) | `display_agent` | `172.20.0.13` | `8003` | Internal only | HDMI HUD output (double-buffer rendering + bounding boxes) |
| Module D (Storage) | `firebase_sync_agent` | `172.20.0.14` | `8004` | Internal only | Async Firestore/Storage sync |
| Device Master | `device_master_agent` | `172.20.0.15` | `8005` | Internal only | FDA mapping & Cloud Meta DB bridge |
| Mock External AI | `mock_external_ai` | `172.20.0.16` | `8006` | Internal only | Simulated 3rd-party edge AI (API Mock) |

### Network Isolation for 3rd Party AI
- **`antigravity_bridge`** (`172.20.0.0/16`): Internal service mesh. All Antigravity containers.
- **`isolated_ai_bridge`** (`172.20.1.0/24`): Air-gapped network (`internal: true`) for 3rd party AI containers. No outbound internet. Gateway is dual-homed (172.20.1.10) to reach it.
- 3rd party containers must be assigned **only** to `isolated_ai_bridge` — never `antigravity_bridge`.

---

## 5. Docker Compose File Variants

| File | Environment | Notes |
|---|---|---|
| `docker-compose.yml` | **RPi 5 + Hailo-8 target** | `/dev/hailo0` device mapping, `device_cgroup_rules` enabled |
| `docker-compose.mac.yml` | **Mac local dev/simulation** | No device mapping, `HEF_PATH=/app/models/simulation.hef` |

### Run on Mac (simulation mode)
```bash
docker-compose -f docker-compose.mac.yml up -d --build
```

### Run on Raspberry Pi 5 (real Hailo-8)
```bash
docker-compose up -d --build
```

---

## 6. Modular Architecture

### Module A: AI Inference Container (The "Inference Engine")
Sole responsibility: receive an image, return counts.
- **Endpoint**: `POST /inference` (YOLOv11-based object detection)
- **Performance**: Shared memory (mmap) considered; batching/tiling for overlapping instruments.

### Module B: Main Controller & QR Decoder (The "Orchestrator")
Manages the system state machine and main control loop.
- **QR Scan Loop**: Monitors camera frames for QR codes.
- **Job Management**: Creates `current_job` with target count based on QR data.
- **5-Second Logic**: If actual count differs from target for 5+ seconds, transitions to Warning/Error state.

### Module C: HDMI Display & UI Overlay (The "Frontend")
Renders video feed and status UI to Raspberry Pi HDMI output.
- **READY (Yellow)**: Scanning. `[Target: N items]`. 20px yellow border.
- **MATCH (Green)**: Count matches. Green border. PASS sound triggered.
- **ERROR (Red)**: Mismatch. Red blinking border, over/under item text displayed.

### Module D: Firebase Cloud Sync (The "Backend Liaison")
Persists data asynchronously.
- **Firestore**: Records inspection history.
- **Storage**: On ERROR state lasting 5 seconds, captures 3 snapshots (0.5s delay, 0.1s interval) and uploads.

### Device Master Agent (The "Encyclopedia")
Translates YOLO labels to standardized product names.
- **FDA Mapping**: `forceps` -> `Tissue Forceps, Ring (FDA Class I)`
- **Cloud Bridge**: Connects securely to the Digioptics Application DB to fetch latest customer-mapped catalog. Does not connect directly to Customer DB.

### Mock External AI (The "Adapter Tester")
Simulates 3rd-party edge inference for integration testing.
- **Protocol**: HTTP/JSON (Schema intentionally differs from native Module A).
- **Gateway Adapter**: Gateway Agent includes a translation layer (`_normalize_inference_response`) to handle external schemas.

---

## 7. System Status (Phase)

### Completed
| Item | Description |
|---|---|
| Module B (Gateway) | QR/Job integration, state machine (READY->MATCH->ERROR), 5-second delay trigger |
| Module A (Inference) | RPi5 + Hailo-8 hardware running (mode=hailo) |
| Firebase Pipeline | Error snapshot trigger and async Storage upload |
| HDMI Display (Module C) | HDMI output and ASCII HUD overlay (xhost permission resolved) |
| Autonomous System | Gateway-based autonomous counting loop (pull-based) |
| UI/UX Polish | SVG favicon and HUD ASCII character substitution |
| SurgeoNet Prep | 14 surgical tool label mapping and Device Master metadata sync |
| Preset Cycle | 5 random preset sets auto-cycle (Set 1->2->3->4->5->1). Stored in Firestore `job_config/rpi` as `sets[]` + `cursor`. Auto-advances 5s after MATCH/ERROR. |
| QR Flash Indicator | "QR SCANNED" banner shown for 3s at bottom center on successful QR scan. `flash_text` field added to `/hud` endpoint. |
| QR Trigger | `/job` endpoint stores to `_pending_preset`. QR scan transitions `_pending_preset -> current_job` to start detection. |
| One-click Launcher | `~/Desktop/SurgicalAI.desktop` double-click runs `xhost +local:` and `docker compose up -d`. |
| 3rd Party AI Mock | Mock service (`mock_external_ai`) and Gateway adapter logic implemented and verified via local simulation. |
| Label Sync (POC) | Unified instrument names across Dashboard, Inference, and Device Master (e.g., "Sur. Scissor"). |
| HUD Update | Status text changed from "YES MATCH" to "GOOD" for matched state. |
| Multitenancy | System supports `APP_ID` (surgical, od, inventory) and `DEVICE_ID` for isolated domain and device management. |
| Bounding Box Overlay | Tracker-smoothed bounding boxes with label + confidence rendered on HDMI display in real time. |
| SurgicalTracker Improvements | Class voting (10-frame majority vote suppresses jitter). Split EMA: center coordinates responsive (alpha=0.25), bbox size locked after 12 frames (alpha=0.01). |
| CONF_THRESHOLD Tuned | Inference `CONF_THRESHOLD=0.35` (SurgeoNet). Raised from 0.20 to suppress background hallucinations; 0.55 was too aggressive (zero detections). |
| Temperature Thresholds Raised | Gateway thermal throttle: NORMAL=75°C/12fps, WARM=82°C/5fps, HOT=88°C/2fps (was 65/75/82). Prevents false throttle at typical RPi5 operating temp ~77°C. |
| 20-Preset Tray Rotation | TRAY-001..TRAY-020 seeded to Firestore `job_config/{DEVICE_ID}`. Each preset has named targets. Auto-advances 5s after MATCH/ERROR. |
| Preset Format Updated | Firebase `sets[]` now supports `{"job_id": "TRAY-001", "target": {...}}` structure. `_do_load_current_set` and `_do_advance_set` both handle this. Job ID shown in DATA INFO panel. |
| DATA INFO Panel | HDMI HUD shows active preset targets (instrument name + required count) in DATA INFO box above TRAY INFO. |
| Network Isolation (3rd Party AI) | `isolated_ai_bridge` (`internal: true`) added to docker-compose. Gateway dual-homed. 3rd party containers air-gapped from internet. |
| Semantic SKU Mapper | `scripts/semantic_map_skus.py`: multilingual embedding pipeline (translate → English embed → cosine similarity) maps manufacturer SKUs to SurgeoNet classes. Thresholds: AUTO=0.60, REVIEW=0.40. |
| Manufacturer Adapters | `adapters/edlo_adapter.py`, `adapters/rhosse_adapter.py`, `adapters/bahadir_adapter.py`: normalize Edlo (PT), Rhosse (PT), Bahadir (TR/DE/EN) API responses to standard `{sku, name, manufacturer}` format. |

### Web Dashboard (Firebase Hosting & Authentication)
Deployed on Firebase Hosting. Google Login required (enforced via `firestore.rules`).
- **Tech Stack**: HTML, Tailwind CSS, Vanilla JS, Firebase v10 SDK.
- **Admin View (`/admin`)**: Monitors `sync_events` collection via onSnapshot; shows only error (mismatch/alert) tray items. Click to view 3 Storage snapshots in a slider modal.
- **Company View (`/`)**: Displays today's overall inspection pass rate as a large number; Chart.js bar chart for hourly throughput.

==================================================
# Development & Execution Guide (Quick Start)
==================================================

### Virtual Environment Setup
```bash
chmod +x setup.sh
./setup.sh
```

### Run Mac Local Simulation
```bash
docker compose -f docker-compose.mac.yml up -d --build
```

### Useful curl Commands
```bash
# Check Gateway (Module B) status
curl http://localhost:8000/health

# Access Inference (Module A) backend directly from inside network
docker exec inference_agent curl -s -X POST http://localhost:8001/inference -F "image=@/path/test.jpg"

### API & Deployment Documentation
- [RPi Onboarding & Deployment Guide](docs/rpi_onboarding_guide.md)
- [System Integration Architecture](docs/integration_architecture.md)
- [3rd Party AI Inference Integration](docs/3rd_party_ai_inference_spec.md)
- [Customer Device Master API](docs/customer_api_spec.md)
- [Device Master Catalog & Mapping](docs/device_master_catalog_spec.md)
```

---

## 8. Physical Deployment (RPi5 + Hailo-8)

### Prerequisites
- **OS**: Raspberry Pi OS (64-bit, Debian Bookworm-based)
- **Hardware**: Raspberry Pi 5 (8GB recommended), Hailo-8 AI Accelerator (M.2 HAT+, AI Kit, or compatible PCIe/USB module)
- **Storage**: 32GB+ MicroSD or NVMe SSD (10GB+ free space)
- **Network**: Internet connection required (apt packages and Docker image pulls)
- **User Permissions**: Regular user with sudo (do not run as root)

### Scripts (`scripts/` directory)
| Script | Role | Reboot Required |
|---|---|---|
| `check_system.sh` | Pre/post check (7 categories) | No |
| `setup_hailo.sh` | Hailo-8 driver install (hailo-all, DKMS, udev) | Yes |
| `setup_docker.sh` | Docker CE + Compose install and daemon optimization | No |
| `optimize_rpi5.sh` | RPi5 performance tuning (CPU, PCIe, kernel params) | Yes |

### Deployment Steps

```bash
# 1. Grant script permissions
chmod +x scripts/*.sh

# 2. Pre-check
./scripts/check_system.sh

# 3. Install Hailo-8 driver
./scripts/setup_hailo.sh
# -> sudo reboot after completion

# 4. (After reboot) Verify Hailo-8 device
ls -la /dev/hailo0
hailortcli fw-control identify

# 5. Install Docker
./scripts/setup_docker.sh
# -> Log out and back in (to apply docker group)

# 6. RPi5 performance optimization
./scripts/optimize_rpi5.sh
# -> sudo reboot after completion

# 7. (After reboot) Final check
./scripts/check_system.sh
# -> 0 FAILs = ready for deployment

# 8. Start system
docker compose up -d --build
```

### Key Kernel Parameters

#### `/boot/firmware/config.txt`
| Parameter | Value | Effect |
|---|---|---|
| `arm_boost` | `1` | Enable CPU turbo (2.4GHz) |
| `over_voltage_delta` | `50000` | +50mV voltage boost (turbo stability) |
| `gpu_mem` | `64` | Minimize VideoCore memory (more RAM for inference) |
| `dtparam=pciex1_gen` | `3` | PCIe Gen3 (5GT/s, max Hailo-8 bandwidth) |

#### `/boot/firmware/cmdline.txt` (append to single line)
| Parameter | Effect |
|---|---|
| `pcie_aspm=off` | Disable PCIe ASPM (reduces inference latency ~1ms) |
| `usbcore.autosuspend=-1` | Disable USB auto-suspend (camera stability) |

#### `/etc/sysctl.d/99-surgicalai.conf`
| Parameter | Value | Effect |
|---|---|---|
| `vm.swappiness` | `5` | Minimize swap (ML workload) |
| `fs.file-max` | `131072` | File descriptor limit (Docker + Hailo + Camera) |
| `kernel.shmmax` | `536870912` | Hailo SDK POSIX SHM shared memory (512MB) |

### docker-compose.yml cgroup Rule Update

The actual major number of `/dev/hailo0` may vary by kernel version.

```bash
# Check actual major number
stat -c '%t' /dev/hailo0 | xargs -I{} printf '%d\n' 0x{}

# Reflect in docker-compose.yml (e.g., if major number is 235)
# device_cgroup_rules:
#   - "c 235:* rmw"
```

### Firebase Production Mode Setup

A service account key file is required to write to real Firestore/Storage.

```bash
# 1. Download service account key from Firebase Console
#    Firebase Console -> Project Settings -> Service Accounts -> Generate new private key

# 2. Copy key file to project root
cp ~/Downloads/firebase-service-account.json ./firebase-credentials.json

# 3. Set path in .env
echo "FIREBASE_CREDENTIALS_PATH=/app/firebase-credentials.json" >> .env

# 4. Verify volume mount in docker-compose.yml
#    volumes:
#      - ./firebase-credentials.json:/app/firebase-credentials.json:ro
```

### Post-Deployment Verification

```bash
# NPU status and temperature
hailortcli fw-control identify
hailortcli monitor  # Ctrl+C to exit

# CPU frequency check (2400000 = 2.4GHz is normal)
cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_cur_freq
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor  # -> performance

# Container status
docker compose ps

# Full pipeline E2E test
curl -X POST http://localhost:8000/job \
  -H "Content-Type: application/json" \
  -d '{"job_id":"DEPLOY-TEST-001","target":{"scalpel":1}}'

# NPU stats
curl http://localhost:8001/metrics
```

### Mac -> RPi Code Sync Workflow

Always follow this sequence when syncing Mac edits to RPi:

```bash
# 1. Mac -> RPi file transfer (run from Mac terminal)
ssh digioptics_am01@192.168.0.4 "mkdir -p ~/SurgicalAI01/src/<module>"
scp /Users/shawnshlee/1_Antigravity/SurgicalAI01/src/<module>/main.py \
  digioptics_am01@192.168.0.4:~/SurgicalAI01/src/<module>/main.py

# 2. RPi -> Container apply (run from RPi terminal)
docker cp ~/SurgicalAI01/src/<module>/main.py <container_name>:/app/src/<module>/main.py
docker restart <container_name>

# 3. Permanent apply (image rebuild) — required so changes survive docker compose up -d
cd ~/SurgicalAI01
docker compose build --no-cache <service_name>
docker compose up -d <service_name>
```

**Important**: Changes applied via `docker cp` are lost when the container is recreated by `docker compose up -d`. Always rebuild the image for permanent changes.

### Troubleshooting Ledger

1. **PCIe not detected (`/dev/hailo0` missing)**: Removed malformed `dtparam=pciex1=` typo from `/boot/firmware/config.txt`; explicitly set `dtparam=pciex1` and `dtparam=pciex1_gen=3`.
2. **Inference Agent permission error**: Modified `Dockerfile.inference` to run as `root` and create home directory, resolving HailoRT log and device access permissions.
3. **`HAILO_OUT_OF_PHYSICAL_DEVICES`**: Prevented duplicate SDK access from temperature monitoring thread by switching to direct `sysfs` reads.
4. **HDMI overlay not displaying**: Resolved RPi OS Bookworm security policy by running `xhost +local:` on host before restarting Display Agent.
5. **HUD character corruption (???)**: OpenCV default font does not support Unicode; replaced `◈` with ASCII `[+]`.
6. **Insufficient real-time response**: Converted Gateway Agent from manual request-based to autonomous `_counting_loop` (pull-based), enabling immediate real-time inference when job is active.
7. **Container code sync**: Applied volume mount (`./src:/app/src`) in `docker-compose.mac.yml` for immediate local dev changes.
8. **SurgeoNet integration prep**: Reflected SurgeoNet's 14 classes (`Overholt Clamp`, `Scalpel`, etc.) in `DEFAULT_CLASS_NAMES` and `labels.json`; includes Class 0 Background filtering.
9. **`SyntaxError: name 'current_job' is used prior to global declaration`**: `global` declaration in `_counting_loop` was mid-function (~line 403); moved all `global` declarations to top of function (just below docstring).
10. **`docker cp` not reflecting Mac changes**: RPi's `~/SurgicalAI01/src/` is managed separately from Mac. `docker cp ~/SurgicalAI01/src/...` copies RPi local files, not Mac edits. Always SCP from Mac to RPi first, then `docker cp`.
11. **`camera_agent` port unreachable from host (HTTP 000)**: `camera_agent:8002` is only exposed on Docker internal network (`expose`). `curl localhost:8002` from host will fail by design. Use `docker exec gateway_agent curl http://camera_agent:8002/frame` for internal testing.
12. **Camera reconnect not detected**: USB camera reconnect requires `docker restart camera_agent`. OpenCV VideoCapture opens the device at startup and cannot detect new connections without a restart.
13. **HDMI overlay silent death (service healthy, screen blank)**: `_render_loop()` had no exception handling — a single numpy/OpenCV error would silently kill the daemon thread while FastAPI `/health` kept responding normally. Fixed by adding try/except with consecutive error counter in `display/main.py`, and canvas bounds clamping in `display/hud.py` (`_panel_bg`, `_draw_status_text`).
14. **`IndentationError` in Gateway Adapter**: Accidentally introduced an indentation error while injecting the `_normalize_inference_response` function into `gateway/main.py`. Resolved by reverting to git state and carefully re-applying chunks.
15. **`net::ERR_QUIC_PROTOCOL_ERROR`**: Chrome console error indicating transient network protocol timeout. Harmless; Firebase SDK automatically recovers via TCP/WebSocket fallback.
16. **HDMI low FPS + overlay not displaying (fresh install)**: Two root causes: (a) `start_detection.sh` only ran `xhost +local:` if `$DISPLAY` was set in the current shell — SSH sessions without X11 forwarding skip this, leaving the container unable to connect to X. Fixed by always running `DISPLAY=:0 xhost +local:`. (b) Without `ipc: host` in docker-compose, the container is IPC-isolated from the host, blocking X11 MIT-SHM shared memory. OpenCV must then copy the full ~6MB frame over the X11 socket every render (~180MB/s at 30fps). Fixed by adding `ipc: host` to `display_agent` and removing `QT_X11_NO_MITSHM=1`.
17. **`/dev/hailo0` missing after `hailo-all` install (no reboot)**: `hailortcli` and PCIe device detected but kernel module not loaded. Fix: `sudo modprobe hailo_pci` loads the module immediately without a reboot. Also add user to hailo group: `sudo usermod -aG hailo <user>`. The one-click deployment script now runs `modprobe` automatically after `setup_hailo.sh` and only reboots if `/dev/hailo0` still does not exist.
18. **`permission denied while trying to connect to Docker API` in deployment script**: User was added to docker group in Phase 2 but the group is not active in the current shell session — requires re-login or `newgrp`. Fix: deployment script now calls `exec sg docker -c "bash ..."` to re-launch itself under the docker group context without requiring a logout/login cycle.
19. **`version` attribute obsolete warning in docker-compose.yml**: Docker Compose v2.x ignores and warns about the top-level `version:` field. Removed `version: '3.8'` from `docker-compose.yml`.
20. **One-click deployment skipped Phase 4 (DEVICE_ID prompt) on new RPi**: Copying the project folder from RPi1 to RPi2 via `scp -r` also copies `.deploy_state`, which records RPi1's completed phase. The new RPi reads it, sees Phase 6 already done, and skips all setup. Fix: state file now stores `<phase>:<hostname>`. On startup, if hostname doesn't match, the script resets to Phase 1. Manual recovery: `sed -i 's/^DEVICE_ID=.*/DEVICE_ID=<new-id>/' .env` then `docker compose up -d --force-recreate`.
21. **`POST /frame` returning 422 (bounding boxes not showing)**: Display agent container had old `FrameUpdate` schema with `image_b64` as required field. Gateway sends detection-only payloads (no image). Fixed by making `image_b64: Optional[str] = Field(None)` in `src/display/schemas.py`.
22. **Bounding boxes bouncing**: Gateway was forwarding `raw_dets` (unsmoothed per-frame inference). Fixed by forwarding `tracked_dets` (EMA-smoothed confirmed tracks from SurgicalTracker). Also reduced `ema_alpha` from 0.5 to 0.25 for smoother movement.
23. **`is_confirmed` ignoring `min_hits` parameter**: Tracker had `return self.total_hits >= 3` hardcoded. Fixed to `return self.total_hits >= 2`.
24. **`localhost:8004` unreachable from RPi host**: `firebase_sync_agent` uses `expose` not `ports` — only accessible inside Docker network. Use `docker exec gateway_agent curl -s -X POST http://firebase_sync_agent:8004/load_current_set` to trigger preset loads from RPi host.
25. **Firestore preset not loading as job**: `/load_current_set` returns 202 immediately (fire-and-forget). Old `sets[]` format was plain target dicts; new format is `{"job_id": "TRAY-001", "target": {...}}`. Both `_do_load_current_set` and `_do_advance_set` updated to handle both formats.
26. **Semantic mapper scores too low with multilingual embeddings**: `paraphrase-multilingual-mpnet-base-v2` clusters all surgical instruments together (domain clustering). "scalpel" ↔ "forceps" scored 0.77 (wrong). Fixed with two-stage pipeline: translate to English first (`deep-translator`), then embed English-to-English with `all-mpnet-base-v2`. Auto-mapped threshold lowered to 0.60 (reality-calibrated for surgical domain).

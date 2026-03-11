# Raspberry Pi 5 Deployment & Onboarding Guide

**Version**: 1.0
**Target Device**: Raspberry Pi 5 (8GB) + Hailo-8 AI Accelerator
**Purpose**: Instructions for setting up a "blank" RPi to run the Surgical AI Gateway.

---

## Phase 1: Hardware Assembly

1.  **RPi5 Preparation**: Ensure you have the active cooler installed on the RPi5.
2.  **Hailo-8 Installation**: Connect the Hailo-8 AI Accelerator (M.2 HAT+, AI Kit, or compatible PCIe/USB module).
3.  **Imaging**: Connect the 4K USB Camera to one of the blue USB 3.0 ports.
4.  **Display**: Connect an HDMI cable to the Micro-HDMI port (Port 0 recommended) if local HUD is required.

---

## Phase 2: OS Flashing

1.  Use **Raspberry Pi Imager** on your PC/Mac.
2.  **OS Selection**: `Raspberry Pi OS (64-bit)` — **Bookworm** based is required.
3.  **OS Customization** (Cmd+Shift+X):
    - Set hostname (e.g., `surgical-ai-01`).
    - Enable SSH.
    - Set user (e.g., `digioptics_am01`) and password.
    - Configure Wi-Fi or Ethernet.

---

## Phase 3: One-Click Installation (Recommended)

Download and run a single file — it handles **everything** automatically:

```bash
curl -fsSL https://raw.githubusercontent.com/zymer4him2024/surgicalai01/main/scripts/install.sh -o install.sh
chmod +x install.sh
./install.sh
```

The installer progresses through 6 phases and saves its state between reboots:

| Phase | What it does | Reboot? |
|---|---|---|
| 1 | Clone the SurgicalAI repository | ❌ No |
| 2 | Hailo-8 driver & firmware | ✅ Yes |
| 3 | Docker CE & Compose | ❌ No |
| 4 | CPU/PCIe/Memory tuning | ✅ Yes |
| 5 | System verification | ❌ No |
| 6 | .env setup & container launch | ❌ No |

After each reboot, just re-run `./install.sh` from the same location and it resumes automatically.

### Manual Installation (Advanced)

If you prefer to run each step individually, first clone the repository:
```bash
git clone https://github.com/zymer4him2024/surgicalai01.git ~/SurgicalAI01
cd ~/SurgicalAI01
```

Then run each script in order:

| Step | Script | Purpose | Reboot? |
|---|---|---|---|
| **1** | `./scripts/setup_hailo.sh` | Installs HailoRT drivers and firmware. | ✅ Yes |
| **2** | `./scripts/setup_docker.sh` | Installs Docker CE and Compose. | ❌ No |
| **3** | `./scripts/optimize_rpi5.sh` | Overclocks CPU to 2.4GHz & sets PCIe Gen3. | ✅ Yes |
| **4** | `./scripts/check_system.sh` | Final verification of hardware and software. | ❌ No |

---

## Phase 4: Environment Configuration

1.  **Copy Environment Template**:
    ```bash
    cp .env.example .env
    ```
2.  **Configure `.env`**:
    - Add your `FIREBASE_API_KEY`.
    - Set `DEVICE_MASTER_URL` to your hospital's MDM server or leave default for mock service.
3.  **Firebase Credentials**:
    - Place your `firebase-credentials.json` in the root directory if cloud sync is required.

---

## Phase 5: Launching the System

### Standard Launch
```bash
docker compose up -d --build
```

### Local HUD Launch (HDMI Overlay)
If you are connected to an HDMI monitor, run this before starting:
```bash
xhost +local:
```

---

## Phase 6: Post-Onboarding Verification

Run these checks to ensure the "blank" Pi is now a "surgical" Pi:

1.  **Physical Access**: `ls -la /dev/hailo0` (Should return a device file).
2.  **AI Engine**: `hailortcli fw-control identify` (Should show firmware version).
3.  **System Health**: Access `http://<rpi-ip>:8000/health` from your browser.
4.  **Admin UI**: Check the local device list in the [Admin Dashboard](https://surgicalai01.web.app/admin).

---

## Help & Troubleshooting

Refer to the **Troubleshooting Ledger** in [CLAUDE.md](../CLAUDE.md) for common issues related to PCIe detection or container permissions.

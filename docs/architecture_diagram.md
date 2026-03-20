# Teleios Gateway — System Architecture

## Updated Architecture Diagram

```mermaid
graph TB
    subgraph CLOUD["☁️ Application DB (Firebase)"]
        FS["Firestore<br/>(Device Catalog, Presets,<br/>Sync Events, Inspections)"]
        ST["Storage<br/>(Error Snapshots)"]
        WEB["Firebase Hosting<br/>(Admin / Company Dashboard)"]
    end

    subgraph CUSTOMER["🏥 Customer's DB"]
        CDB["Customer MDM Database"]
        CMAP["Instrument Mapping<br/>(List for mapping)"]
    end

    subgraph MODELS["🤖 Models"]
        INT["Internal Models<br/>(Hailo-8 NPU)"]
        EXT["3rd Party Models<br/>(External Edge AI)"]
    end

    subgraph EDGE["📦 Edge Device (RPi5 + Hailo-8)"]
        GW["Gateway Agent<br/>(Orchestrator)<br/>State Machine · QR Decode"]
        INF["Inference Agent<br/>(Mapped Inference)"]
        DM["Device Master<br/>(Meta Data + Local Cache)"]
        SYNC["Firebase Sync Agent<br/>(Cloud Data Pipeline)"]
        CAM["Camera Agent<br/>(4K USB Capture)"]
        DISP["Display Agent<br/>(HDMI HUD Overlay)"]
    end

    %% Cloud ↔ Edge
    SYNC -->|"Upload snapshots"| ST
    SYNC <-->|"Read/Write inspections,<br/>presets, sync_events"| FS
    DM -->|"Pull device catalog<br/>(API)"| FS

    %% Cloud ↔ Customer (via App DB only)
    FS <-->|"Secure API bridge<br/>(never direct to Edge)"| CDB
    CDB --- CMAP

    %% Dashboard ↔ Edge (via Firebase)
    WEB -->|"Preset cycle,<br/>control toggles"| FS

    %% Models ↔ Edge
    INT -->|"HEF model on NPU"| INF
    EXT -->|"HTTP/JSON API<br/>(Adapter Pattern)"| GW

    %% Internal Edge flows
    CAM -->|"JPEG frame"| GW
    GW -->|"Frame → inference"| INF
    INF -->|"Detections"| GW
    GW -->|"Label lookup"| DM
    DM -->|"Enriched metadata"| GW
    GW -->|"HUD payload<br/>(border, items, status)"| DISP
    GW -->|"Inspection results,<br/>error snapshots"| SYNC
    CAM -->|"Live frame<br/>(for overlay)"| DISP

    %% Styling
    classDef cloud fill:#e3f2fd,stroke:#1565c0,color:#0d47a1
    classDef customer fill:#fce4ec,stroke:#c62828,color:#b71c1c
    classDef edge fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20
    classDef models fill:#fff3e0,stroke:#e65100,color:#bf360c

    class FS,ST,WEB cloud
    class CDB,CMAP customer
    class GW,INF,DM,SYNC,CAM,DISP edge
    class INT,EXT models
```

## Key Differences from Original Diagram

| Area | Original | Updated |
|---|---|---|
| **Gateway Agent** | Missing — inference was shown as the central node | Added as the orchestrator hub that coordinates all agents |
| **Firebase Sync Agent** | Missing | Added — handles all cloud data upload/download |
| **Meta Data** | Two ambiguous "Meta Data" boxes | Clarified: **Firestore** (cloud catalog) and **Device Master** (local cache) |
| **Customer DB → Edge** | Direct path unclear | Made explicit: Customer DB → App DB → Device Master (never direct) |
| **Display ↔ Camera** | Bidirectional arrow | Corrected: Camera sends frames to Gateway AND Display; Display never calls Camera |
| **QR Scanner** | Not shown | Included as part of Gateway Agent responsibilities |

## Data Flow Summary

```mermaid
sequenceDiagram
    participant Admin as Admin Dashboard
    participant FS as Firestore
    participant Sync as Firebase Sync Agent
    participant GW as Gateway Agent
    participant CAM as Camera Agent
    participant INF as Inference Agent
    participant DM as Device Master
    participant DISP as Display Agent

    Admin->>FS: Write preset (sets[], cursor)
    FS-->>Sync: onSnapshot trigger
    Sync->>GW: POST /job (target counts)
    
    loop Counting Loop (every frame)
        GW->>CAM: GET /frame
        CAM-->>GW: JPEG frame
        GW->>INF: POST /inference (image)
        INF-->>GW: Detections [{class, count, bbox}]
        GW->>DM: GET /lookup?label=Sur. Scissor
        DM-->>GW: {device_name, fda_class}
        GW->>GW: Compare target vs actual
        GW->>DISP: POST /hud (border, items, status)
    end

    alt MATCH (counts equal for 5s)
        GW->>DISP: border=green, text="GOOD"
        GW->>Sync: Log "GOOD" result
    else MISMATCH (counts differ for 5s)
        GW->>DISP: border=red, text="NO MATCH"
        GW->>Sync: Log "NO MATCH" + 3 snapshots
        Sync->>FS: Upload snapshots to Storage
    end
```

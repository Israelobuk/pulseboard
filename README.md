# PulseBoard DS Lab

PulseBoard DS Lab is the analytics-forward branch of PulseBoard: a desktop system intelligence app that combines live telemetry, historical storage, and explainable analytics in one downloadable experience.

The main product is still the desktop app in `Pulseboard.py`. This branch adds a PostgreSQL-backed telemetry layer and an R analytics engine that feeds insights back into the desktop UI.

## What It Does

- Monitors live CPU, RAM, GPU, battery, and process activity
- Stores historical telemetry in PostgreSQL for session comparison and baselines
- Uses SQL to query rollups, deltas, baselines, and replay events
- Uses R as an analytics engine for:
  - workload fingerprinting
  - anomaly detection
  - forecasting
  - explainable session scoring
  - session replay reconstruction
- Surfaces those analytics inside the desktop app through:
  - Analytics Engine
  - Session Fingerprint
  - Replay Timeline
  - Insight Engine
  - Advisor

## Stack

- `Python`: live telemetry collection, app logic, actions, Postgres integration
- `PySide6 / Qt`: custom desktop UI and animations
- `PostgreSQL`: historical telemetry, session storage, analytics outputs
- `SQL`: schema design, inserts, rollups, retrieval queries
- `R`: analytics engine for higher-order scoring and intelligence

## Key Features

### Desktop Telemetry
- Real-time CPU, RAM, GPU, and battery monitoring
- Process table with refresh, terminate, cleanup, and common-app controls
- Animated gauges and performance train synced to live system data

### Historical Intelligence
- 24-hour baseline comparisons
- Historical delta cards
- Session-aware analytics and process context

### Workload Fingerprinting
- Classifies sessions like `coding`, `gaming`, `browsing`, `idle`, and `mixed_heavy`
- Compares the current session to the user’s own historical baseline
- Explains why the session matches or differs from that baseline

### Session Replay
- Reconstructs key session events such as:
  - RAM spikes
  - GPU activation
  - efficiency drops
  - process-driven pressure changes

### Analytics Engine
- Runs in R and writes outputs back to PostgreSQL
- Produces explainable insights, anomaly text, session scoring, and forecasts

## Project Layout

- `Pulseboard.py`
  Main desktop app
- `desktop_store.py`
  PostgreSQL integration layer for the desktop app
- `collector.py`
  Standalone telemetry collector path
- `sql/schema.sql`
  PostgreSQL schema including telemetry and analytics tables
- `sql/workload_fingerprint_queries.sql`
  Retrieval and analysis query examples
- `r_engine/session_analytics.R`
  Main R analytics pipeline
- `r_engine/workload_helpers.R`
  Workload fingerprinting and replay helpers
- `shiny_app/`
  Optional R Shiny sidecar analytics prototype

## Quick Start

### 1. Set the database connection

```powershell
$env:PGPASSWORD = "postgres"
$env:PULSEBOARD_DB_DSN = "postgresql://postgres:postgres@localhost:5432/pulseboard"
```

### 2. Initialize the schema

```powershell
.\init-db.ps1
```

### 3. Install Python dependencies

```powershell
python -m pip install -r .\requirements-ds.txt
```

### 4. Install R packages

```powershell
$env:R_LIBS_USER = "$HOME\Documents\R\win-library\4.5"
$env:Path += ";C:\Program Files\PostgreSQL\17\bin"
$rBin = Get-ChildItem "C:\Program Files\R" -Directory | Sort-Object Name -Descending | Select-Object -First 1 | ForEach-Object { Join-Path $_.FullName "bin\x64" }
$env:Path += ";$rBin"
.\install-r-packages.ps1
```

### 5. Run the desktop app

```powershell
.\run-desktop.ps1
```

## Optional Commands

Run the standalone collector:

```powershell
.\run-collector.ps1 -Interval 2 -WorkloadLabel "coding"
```

Run the optional Shiny sidecar:

```powershell
.\run-shiny.ps1
```

## Notes

- The desktop app is the main product experience.
- R is used as an analytics engine behind the desktop app, not as the primary UI.
- The Shiny app is optional and exists as a sidecar analytics prototype.
- If R analytics are unavailable, the desktop app should continue running without crashing.

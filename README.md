# Pulseboard

Pulseboard is a Windows desktop performance board that turns raw system telemetry into a live, readable session view. It combines CPU, RAM, GPU, battery, process activity, baseline comparison, and analytics-driven interpretation in a single app.

## What It Does

- Tracks live CPU, RAM, GPU, battery, and active process pressure
- Scores the current session and shows workload mode in real time
- Compares the current run against saved history to surface a 24-hour baseline and historical delta
- Uses an analytics pipeline to generate session fingerprinting, anomaly scoring, replay events, and forecast cards
- Gives plain-language insight and advisor readouts instead of only raw charts

## How It Works

- `Pulseboard.py` runs the desktop UI and collects live system telemetry
- `desktop_store.py` writes session data into PostgreSQL and reads analytics outputs back into the app
- `sql/schema.sql` defines the storage layer for metrics, sessions, replay events, baselines, and analytics scores
- `r_engine/session_analytics.R` and `r_engine/workload_helpers.R` compute forecasts, anomaly signals, workload classification, and replay summaries

## Product Experience

- **Live telemetry**: animated gauges, process view, GPU activity, and recent event feed
- **Historical intelligence**: 24-hour baseline, delta cards, session-aware context
- **Session fingerprint**: workload match, confidence, anomaly score, baseline similarity, and readout summary
- **Analytics engine**: forecast and scoring cards backed by stored telemetry and analytics output
- **Action layer**: refresh, kill process, cleanup, and common background-app controls

## Distribution

For normal users, Pulseboard should be shipped as a packaged Windows build rather than run from source. The packaged build is the intended product path and avoids manual Python package installation.

The repo includes `Pulseboard.spec`, and the build should bundle:

- the Python runtime and Python dependencies
- the desktop app code
- `r_engine/`
- `sql/`

## Source Setup

If you are developing or testing from source and want the full analytics path, you need:

- Python dependencies from `requirements.txt` and `requirements-ds.txt`
- PostgreSQL running locally
- R plus the packages installed by `install-r-packages.ps1`

Then:

```powershell
$env:PULSEBOARD_DB_DSN = "postgresql://postgres@localhost:5432/pulseboard"
.\init-db.ps1
.\run-desktop.ps1
```

## Project Files

- `Pulseboard.py`: main desktop application
- `desktop_store.py`: PostgreSQL telemetry and analytics bridge
- `collector.py`: standalone collector entrypoint
- `run-desktop.ps1`: local launcher
- `Pulseboard.spec`: packaged Windows build definition

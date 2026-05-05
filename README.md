# Pulseboard

Pulseboard is a Windows desktop system intelligence app that shows live telemetry (CPU/RAM/GPU/battery), process activity, and lightweight “advisor” style insights in a single UI.

## What It Does

- Live CPU, RAM, GPU, battery, and process monitoring
- Process table + actions (refresh, terminate, cleanup, common-app controls)
- Animated gauges + timeline-style display synced to live system data

## Project Layout

- `Pulseboard.py`: main desktop app (PySide6 / Qt)
- `collector.py`: standalone telemetry collector
- `desktop_store.py`: optional local/desktop telemetry storage utilities

## Quick Start (Windows)

```powershell
python -m pip install -r .\requirements.txt
.\run-desktop.ps1
```

## Optional

Run the standalone collector:

```powershell
.\run-collector.ps1 -Interval 2 -WorkloadLabel "coding"
```

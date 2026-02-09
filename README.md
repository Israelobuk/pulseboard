# Pulseboard

**Download (Windows EXE):** https://github.com/Israelobuk/pulseboard/releases/tag/v1.0.0

**Pulseboard** is a native Windows system monitor + cleanup utility built with Python, PySide6, and psutil.  
It provides real-time resource insights and a suite of tools to manage performance and background apps.

---

## Features

### Live System Metrics
- Auto-updating dashboard (configurable refresh interval)
- CPU / RAM / GPU usage gauges with smooth animation
- Animated **Usage Train** that chugs based on system load

### Insight Engine
Provides high-level performance insights including:
- **Efficiency Score**
- **Usage Mode** (Quiet / Balanced / Workload)
- **Pulse Grade (A–D)**
- Trend and peak summaries

### Tools
- GPU engine utilization + top GPU processes
- Process list with ability to end selected processes
- **One-Click Cleanup**: closes common apps and clears user temp files

---

## Download

Get the latest Windows build in the [Release](https://github.com/Israelobuk/pulseboard/releases/tag/v1.0.0) page.  
Download `Pulseboard-Windows.zip`, extract, and run **Pulseboard.exe**.

> Windows SmartScreen may warn because the app is unsigned.

---

## Run from Source

### Requirements
- Python 3.9+  
- Dependencies from `requirements.txt`

### Install
```bash
pip install -r requirements.txt

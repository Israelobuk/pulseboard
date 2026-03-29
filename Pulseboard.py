import os
import sys
import time
import math
import shutil
import psutil
import platform
import subprocess
from collections import deque
from statistics import mean
from datetime import datetime, timedelta

try:
    from desktop_store import DesktopTelemetryStore
except Exception:
    DesktopTelemetryStore = None

from PySide6.QtCore import (
    Qt,
    QTimer,
    QPointF,
    QRectF,
    QPropertyAnimation,
    QEasingCurve,
    QObject,
    Signal,
    QThread,
)
from PySide6.QtGui import QFont, QFontMetrics, QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QLabel,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QGroupBox,
    QPushButton,
    QDoubleSpinBox,
    QCheckBox,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QAbstractItemView,
    QHeaderView,
    QSplitter,
    QFrame,
    QScrollArea,
    QGraphicsOpacityEffect,
    QSizePolicy,
)

APP_TITLE = "Pulseboard"
REFRESH_SECONDS_DEFAULT = 0.25
HISTORY_MAX_POINTS = 300
GPU_REFRESH_SECONDS = 1.0
LIVE_REFRESH_MS = 150
LIVE_PEAK_HISTORY_MAX_POINTS = 5000
TASKMGR_SYNC_WINDOW = 4
TASKMGR_SYNC_ALPHA = 0.65
DISPLAY_SMOOTH_ALPHA = 0.35
CPU_DISPLAY_SMOOTH_ALPHA = 0.18
CPU_INPUT_SMOOTH_ALPHA = 0.22
GPU_INPUT_SMOOTH_ALPHA = 0.30
RAM_INPUT_SMOOTH_ALPHA = 0.26

PROTECTED = {
    "system",
    "registry",
    "smss.exe",
    "csrss.exe",
    "wininit.exe",
    "services.exe",
    "lsass.exe",
    "svchost.exe",
    "explorer.exe",
    "dwm.exe",
    "taskhostw.exe",
    "sihost.exe",
    "runtimebroker.exe",
    "searchhost.exe",
    "securityhealthservice.exe",
    "securityhealthsystray.exe",
}

COMMON_APPS = {
    "discord.exe",
    "spotify.exe",
    "teams.exe",
    "ms-teams.exe",
    "slack.exe",
    "zoom.exe",
    "steam.exe",
    "steamwebhelper.exe",
    "epicgameslauncher.exe",
    "battle.net.exe",
    "riotclientservices.exe",
    "chrome.exe",
    "msedge.exe",
    "firefox.exe",
    "opera.exe",
    "onedrive.exe",
    "notion.exe",
    "obs64.exe",
}

PROTECTED = {x.lower() for x in PROTECTED}
COMMON_APPS = {x.lower() for x in COMMON_APPS}


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, float(v)))


def safe_mean(xs):
    return float(sum(xs) / len(xs)) if xs else 0.0


def fingerprint_badge_state(anomaly_score=None, preview=False):
    if preview or anomaly_score is None:
        return "Preview", "preview"
    if anomaly_score >= 65:
        return "Abnormal", "alert"
    if anomaly_score >= 35:
        return "Watch", "watch"
    return "Within Norm", "normal"


def smooth_toward(current, target, alpha):
    if current is None:
        return float(target)
    return float(current) + (float(target) - float(current)) * float(alpha)


def get_cpu_taskmgr_like():
    # Task Manager tracks closer to Processor Utility than a plain % Processor Time read.
    ps = r"""
    try {
      $c = Get-Counter '\Processor Information(_Total)\% Processor Utility' -MaxSamples 1 -ErrorAction Stop
      $c.CounterSamples | Select-Object CookedValue | ConvertTo-Json -Compress
    } catch {
      ""
    }
    """
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )
        txt = (p.stdout or "").strip()
        if not txt:
            return None

        import json

        data = json.loads(txt)
        value = float(data["CookedValue"]) if isinstance(data, dict) else float(data[0]["CookedValue"])
        return clamp(value, 0.0, 100.0)
    except Exception:
        return None


def get_live(cpu_value=None):
    b = psutil.sensors_battery()

    cpu = cpu_value
    if cpu is None:
        # Use a Task-Manager-like Windows utility counter first, then fall back to psutil.
        cpu = get_cpu_taskmgr_like()
        if cpu is None:
            cpu = psutil.cpu_percent(interval=None)

    ram = psutil.virtual_memory().percent

    out = {
        "time": datetime.now(),
        "cpu": float(cpu),
        "ram": float(ram),
        "battery": None,
        "plugged": None,
        "temp_c": None,
        "disk_read_mbs": 0.0,
        "disk_write_mbs": 0.0,
        "disk_used_pct": None,
    }

    if b:
        out["battery"] = float(b.percent)
        out["plugged"] = bool(b.power_plugged)

    return out


def get_gpu_activity():
    ps = r"""
    try {
      $c = Get-Counter '\GPU Engine(*)\Utilization Percentage' -ErrorAction Stop
      $c.CounterSamples | Select-Object InstanceName, CookedValue | ConvertTo-Json -Compress
    } catch {
      ""
    }
    """
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )
    except Exception:
        return None, []

    txt = (p.stdout or "").strip()
    if not txt:
        return None, []

    try:
        import json

        data = json.loads(txt)
    except Exception:
        return None, []

    samples = data if isinstance(data, list) else [data]

    total = 0.0
    per_pid = {}
    for s in samples:
        try:
            inst = s.get("InstanceName", "")
            val = float(s.get("CookedValue", 0.0))
        except Exception:
            continue

        total += val
        pid = None
        parts = inst.split("_")
        if len(parts) >= 2 and parts[0] == "pid":
            try:
                pid = int(parts[1])
            except Exception:
                pid = None

        if pid is not None:
            per_pid[pid] = per_pid.get(pid, 0.0) + val

    if not per_pid:
        return total, []

    top = sorted(per_pid.items(), key=lambda x: x[1], reverse=True)[:5]
    top_list = []
    for pid, util in top:
        try:
            name = psutil.Process(pid).name()
        except Exception:
            name = f"PID {pid}"
        top_list.append((pid, name, util))

    return total, top_list


class DataWorker(QObject):
    finished = Signal(dict)
    request = Signal()

    def __init__(self):
        super().__init__()
        self._cancel = False
        self._last_gpu_at = 0.0
        self._last_gpu_total = None
        self._last_gpu_top = []
        self._cpu_sync_values = deque(maxlen=TASKMGR_SYNC_WINDOW)
        self._cpu_synced = None
        # IMPORTANT FIX: persistent worker thread; request signal triggers sampling
        self.request.connect(self.run, Qt.QueuedConnection)

    def cancel(self):
        self._cancel = True

    def _taskmgr_sync_cpu(self):
        raw_cpu = get_cpu_taskmgr_like()
        if raw_cpu is None:
            raw_cpu = psutil.cpu_percent(interval=None)

        raw_cpu = clamp(raw_cpu, 0.0, 100.0)
        self._cpu_sync_values.append(raw_cpu)
        window_avg = mean(self._cpu_sync_values) if self._cpu_sync_values else raw_cpu

        if self._cpu_synced is None:
            self._cpu_synced = window_avg
        else:
            self._cpu_synced = (
                (1.0 - TASKMGR_SYNC_ALPHA) * self._cpu_synced
                + TASKMGR_SYNC_ALPHA * window_avg
            )

        return clamp(self._cpu_synced, 0.0, 100.0)

    def run(self):
        if self._cancel:
            return
        try:
            synced_cpu = self._taskmgr_sync_cpu()
            live = get_live(cpu_value=synced_cpu)
            now_ts = time.time()
            if (now_ts - self._last_gpu_at) >= GPU_REFRESH_SECONDS:
                gpu_total, gpu_top = get_gpu_activity()
                self._last_gpu_total = gpu_total
                self._last_gpu_top = gpu_top
                self._last_gpu_at = now_ts
            else:
                gpu_total, gpu_top = self._last_gpu_total, self._last_gpu_top
            if self._cancel:
                return
            self.finished.emit({"live": live, "gpu_total": gpu_total, "gpu_top": gpu_top})
        except Exception:
            synced_cpu = self._taskmgr_sync_cpu()
            live = get_live(cpu_value=synced_cpu)
            self.finished.emit({"live": live, "gpu_total": None, "gpu_top": []})


class ActionWorker(QObject):
    finished = Signal(object)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.finished.emit(self.fn())
        except Exception as e:
            self.finished.emit(e)


def get_live_fast(gpu_total=0.0):
    b = psutil.sensors_battery()
    out = {
        "time": datetime.now(),
        "cpu": float(psutil.cpu_percent(interval=None)),
        "ram": float(psutil.virtual_memory().percent),
        "battery": None,
        "plugged": None,
        "gpu_total": float(gpu_total or 0.0),
    }
    if b:
        out["battery"] = float(b.percent)
        out["plugged"] = bool(b.power_plugged)
    return out


def recent_window(history, minutes=5):
    if not history:
        return []
    cutoff = history[-1]["time"] - timedelta(minutes=minutes)
    return [h for h in history if h["time"] >= cutoff]


def trend_slope(values):
    if not values or len(values) < 5:
        return 0.0
    n = len(values)
    sum_x = (n - 1) * n / 2.0
    sum_x2 = (n - 1) * n * (2 * n - 1) / 6.0
    sum_y = sum(values)
    sum_xy = sum(i * v for i, v in enumerate(values))
    denom = n * sum_x2 - sum_x * sum_x
    if denom == 0:
        return 0.0
    return (n * sum_xy - sum_x * sum_y) / denom


def trend_label(values, up=0.15, down=-0.15):
    slope = trend_slope(values)
    if slope >= up:
        return "rising"
    if slope <= down:
        return "falling"
    return "stable"


def safe_std(values):
    if not values:
        return 0.0
    m = safe_mean(values)
    var = sum((v - m) ** 2 for v in values) / max(1, len(values) - 1)
    return math.sqrt(var)


def efficiency_score(history, live):
    r = recent_window(history, minutes=5)
    if not r:
        return None, "N/A"
    cpu_avg = safe_mean([x["cpu"] for x in r])
    ram_avg = safe_mean([x["ram"] for x in r])
    plugged = live.get("plugged")
    if plugged is True:
        score = 100.0 - (cpu_avg * 0.45 + ram_avg * 0.35)
    else:
        score = 100.0 - (cpu_avg * 0.6 + ram_avg * 0.4)
        if plugged is False:
            score -= 3.0

    score = clamp(score, 0.0, 100.0)
    return score, f"{score:.0f}/100"


def usage_mode(cpu_avg, ram_avg, disk_avg):
    if cpu_avg < 25 and ram_avg < 50 and disk_avg < 5:
        return "QUIET"
    if cpu_avg < 60 and ram_avg < 75 and disk_avg < 20:
        return "BALANCED"
    return "WORKLOAD"


def detect_events(history, gpu_hist, limit=6):
    events = []

    r = recent_window(history, minutes=20)
    for row in r:
        t = row["time"].strftime("%H:%M:%S")
        if row["cpu"] >= 90:
            events.append((t, f"CPU spike {row['cpu']:.0f}%"))
        if row["ram"] >= 90:
            events.append((t, f"RAM high {row['ram']:.0f}%"))

    g = recent_window(gpu_hist, minutes=20)
    for row in g:
        t = row["time"].strftime("%H:%M:%S")
        if row["gpu"] >= 70:
            events.append((t, f"GPU spike {row['gpu']:.0f}%"))

    events.sort(key=lambda x: x[0], reverse=True)
    return events[:limit]


def list_processes_snapshot(limit=40):
    procs = []
    for p in psutil.process_iter(attrs=["pid", "name"]):
        try:
            with p.oneshot():
                name = (p.info.get("name") or "unknown")
                mem_mb = p.memory_info().rss / (1024 * 1024)
            procs.append((p.pid, name, mem_mb))
        except Exception:
            continue

    procs.sort(key=lambda x: x[2], reverse=True)
    return procs[:limit]


def terminate_pid(pid: int):
    try:
        p = psutil.Process(pid)
        name = (p.name() or "").lower()
        if name in PROTECTED:
            return False, "Protected process."
        p.terminate()
        psutil.wait_procs([p], timeout=0.45)
        if p.is_running():
            p.kill()
        return True, f"Terminated PID {pid}"
    except Exception as e:
        return False, str(e)


def close_common_apps():
    killed = 0
    for p in psutil.process_iter(attrs=["pid", "name"]):
        try:
            name = (p.info.get("name") or "").lower()
            if name in COMMON_APPS and name not in PROTECTED:
                p.terminate()
                try:
                    p.wait(timeout=0.35)
                except Exception:
                    p.kill()
                killed += 1
        except Exception:
            pass
    return killed


def clear_user_temp():
    temp_dir = os.environ.get("TEMP", "")
    if not temp_dir or not os.path.isdir(temp_dir):
        return 0, "Temp folder not found."
    removed = 0
    errors = 0
    for name in os.listdir(temp_dir):
        path = os.path.join(temp_dir, name)
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
            else:
                os.remove(path)
                removed += 1
        except Exception:
            errors += 1
    if errors:
        return removed, f"Completed with {errors} errors."
    return removed, "Completed."


class MetricCard(QFrame):
    def __init__(self, title, value="--", sub=""):
        super().__init__()
        self.setObjectName("metricCard")
        self.setProperty("cardVariant", "default")
        self.title = QLabel(title)
        self.value = QLabel(value)
        self.sub = QLabel(sub)

        self.title.setObjectName("metricTitle")
        self.value.setObjectName("metricValue")
        self.sub.setObjectName("metricSub")
        self.title.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.value.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.sub.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.title.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self.value.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self.sub.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)

        self._layout = QVBoxLayout(self)
        self._layout.setAlignment(Qt.AlignTop)
        self._layout.addWidget(self.title)
        self._layout.addWidget(self.value)
        self._layout.addWidget(self.sub)
        self.set_typography(14, 26, 13)
        self.set_roomy(False)

    def set_value(self, v, sub=""):
        self.value.setText(v)
        self.sub.setText(sub)

    def set_typography(self, title_size, value_size, sub_size):
        title_font = QFont("Bahnschrift", title_size, QFont.Medium)
        value_font = QFont("Bahnschrift", value_size, QFont.Bold)
        sub_font = QFont("Bahnschrift", sub_size)
        self.title.setFont(title_font)
        self.value.setFont(value_font)
        self.sub.setFont(sub_font)
        self.title.setMinimumHeight(QFontMetrics(title_font).height() + 4)
        self.value.setMinimumHeight(QFontMetrics(value_font).height() + 6)
        self.sub.setMinimumHeight(QFontMetrics(sub_font).height() + 4)

    def set_roomy(self, roomy=True):
        if roomy:
            self._layout.setContentsMargins(20, 18, 20, 18)
            self._layout.setSpacing(6)
        else:
            self._layout.setContentsMargins(16, 14, 16, 14)
            self._layout.setSpacing(5)


class FingerprintTile(QFrame):
    def __init__(self, title, value="--", detail=""):
        super().__init__()
        self.setObjectName("fingerprintTile")
        self.title = QLabel(title)
        self.value = QLabel(value)
        self.detail = QLabel(detail)

        self.title.setObjectName("fingerprintTileTitle")
        self.value.setObjectName("fingerprintTileValue")
        self.detail.setObjectName("fingerprintTileDetail")
        self.detail.setWordWrap(True)

        self.title.setFont(QFont("Bahnschrift", 13, QFont.Medium))
        self.value.setFont(QFont("Bahnschrift", 24, QFont.Bold))
        self.detail.setFont(QFont("Bahnschrift", 12))
        self.title.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.value.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.detail.setAlignment(Qt.AlignLeft | Qt.AlignTop)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(6)
        layout.addWidget(self.title)
        layout.addWidget(self.value)
        layout.addWidget(self.detail)

    def set_content(self, value, detail=""):
        self.value.setText(value)
        self.detail.setText(detail)

    def set_feature_style(self):
        self.setProperty("cardVariant", "feature")
        self.set_typography(15, 28, 13)
        self._layout.setContentsMargins(22, 20, 22, 18)
        self._layout.setSpacing(8)
        self.style().unpolish(self)
        self.style().polish(self)


class HoverButton(QPushButton):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setCursor(Qt.PointingHandCursor)


class TrainWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(104)
        self.steam_phase = 0.0
        self.load = 0.0
        self.display_load = 0.0
        self.cpu = 0.0
        self.gpu = 0.0
        self.ram = 0.0
        self.target_cpu = 0.0
        self.target_gpu = 0.0
        self.target_ram = 0.0
        self.anim = QTimer(self)
        self.anim.timeout.connect(self.advance)
        self.anim.start(60)

    def set_load(self, load: float):
        self.load = clamp(load, 0.0, 100.0)
        self.display_load = self.load

    def set_metrics(self, cpu: float, gpu: float, ram: float):
        self.target_cpu = clamp(cpu, 0.0, 100.0)
        self.target_gpu = clamp(gpu, 0.0, 100.0)
        self.target_ram = clamp(ram, 0.0, 100.0)
        self.cpu = self.target_cpu
        self.gpu = self.target_gpu
        self.ram = self.target_ram

    def advance(self):
        self.steam_phase += 0.15 + (self.display_load / 100.0) * 0.35
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing)
            w = self.width()
            h = self.height()

            p.setPen(QPen(QColor("#1f2937"), 3))
            p.drawLine(12, h - 14, w - 12, h - 14)

            base_y = h - 32
            engine = QRectF(20, base_y, w * 0.25, 22)
            p.setPen(QPen(QColor("#475569"), 2))
            p.setBrush(QColor(12, 18, 32, 230))
            p.drawRoundedRect(engine, 6, 6)

            cab = QRectF(engine.left() + 6, base_y - 16, 24, 18)
            p.setBrush(QColor(30, 41, 59, 230))
            p.drawRoundedRect(cab, 4, 4)

            chimney = QRectF(engine.left() + 34, base_y - 18, 10, 16)
            p.setBrush(QColor("#64748b"))
            p.drawRoundedRect(chimney, 3, 3)

            car1 = QRectF(engine.right() + 16, base_y + 2, w * 0.22, 18)
            car2 = QRectF(car1.right() + 14, base_y + 2, w * 0.22, 18)
            p.setBrush(QColor(12, 18, 32, 230))
            p.drawRoundedRect(car1, 6, 6)
            p.drawRoundedRect(car2, 6, 6)

            p.setPen(QColor("#e2e8f0"))
            p.setFont(QFont("Bahnschrift", 8, QFont.Medium))
            p.drawText(engine, Qt.AlignCenter, f"CPU {int(self.cpu)}%")
            p.drawText(car1, Qt.AlignCenter, f"GPU {int(self.gpu)}%")
            p.drawText(car2, Qt.AlignCenter, f"RAM {int(self.ram)}%")

            wheel_y = h - 12
            p.setBrush(QColor("#0b1220"))
            p.setPen(QPen(QColor("#334155"), 1))
            for i in range(4):
                cx = engine.left() + 10 + i * 18
                p.drawEllipse(QPointF(cx, wheel_y), 6, 6)
            for rect in (car1, car2):
                for i in range(2):
                    cx = rect.left() + 12 + i * 22
                    p.drawEllipse(QPointF(cx, wheel_y), 6, 6)

            puff_count = 3 + int(self.display_load / 20.0)
            for i in range(puff_count):
                progress = ((self.steam_phase * 0.12) + i * 0.16) % 1.0
                drift = math.sin((self.steam_phase * 0.4) + i * 0.5) * 3.0
                x = chimney.center().x() + 4 + (progress * 36.0) + drift
                y = (base_y - 10) - (progress * 30.0) - (i * 1.6)
                alpha = max(45, int(185 * (1.0 - progress * 0.55)))
                size = 5.0 + (progress * 6.0) + (self.display_load / 85.0)
                p.setBrush(QColor(148, 163, 184, alpha))
                p.setPen(Qt.NoPen)
                p.drawEllipse(QPointF(x, y), size, size)
        finally:
            p.end()


class CircleGauge(QWidget):
    def __init__(self, label: str):
        super().__init__()
        self.setMinimumSize(112, 112)
        self.label = label
        self.value = 0.0
        self.target_value = 0.0
        self.spin = 0.0
        self.anim = QTimer(self)
        self.anim.timeout.connect(self.advance)
        self.anim.start(60)

    def set_value(self, value: float):
        self.target_value = clamp(value, 0.0, 100.0)
        self.value = self.target_value

    def advance(self):
        self.spin = (self.spin + 4.0) % 360.0
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing)
            w = self.width()
            h = self.height()
            size = min(w, h) - 10
            rect = QRectF((w - size) / 2, (h - size) / 2, size, size)

            p.setPen(QPen(QColor("#1f2937"), 8))
            p.drawArc(rect, 0, 360 * 16)

            pct = min(self.value / 100.0, 0.95)
            span = int(360 * pct * 16)
            start = int((90 - self.spin) * 16)

            if self.value < 50:
                ring = QColor("#22c55e")
            elif self.value < 80:
                ring = QColor("#f59e0b")
            else:
                ring = QColor("#ef4444")

            p.setPen(QPen(ring, 8, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(rect, start, -span)

            p.setPen(QColor("#e5e7eb"))
            p.setFont(QFont("Bahnschrift", 10, QFont.Medium))
            p.drawText(rect, Qt.AlignCenter, f"{self.label}\n{int(self.value)}%")
        finally:
            p.end()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1360, 860)

        self.state = {}
        self.history = deque(maxlen=HISTORY_MAX_POINTS)
        self.gpu_hist = deque(maxlen=HISTORY_MAX_POINTS)
        self.live_fast_hist = deque(maxlen=LIVE_PEAK_HISTORY_MAX_POINTS)
        self.last_process_rows = []

        root = QWidget()
        root.setObjectName("appRoot")
        self.setCentralWidget(root)
        main = QVBoxLayout(root)

        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("mainScroll")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        main.addWidget(self.scroll_area, 1)

        self.content_root = QWidget()
        self.scroll_area.setWidget(self.content_root)
        content = QVBoxLayout(self.content_root)
        content.setSpacing(10)
        content.setContentsMargins(12, 10, 12, 12)

        header = QHBoxLayout()
        logo = QLabel("⎈")
        logo.setObjectName("appLogo")
        title = QLabel("Pulseboard")
        title.setObjectName("appTitle")
        subtitle = QLabel("A native, futuristic control board for your laptop.")
        subtitle.setObjectName("appSub")

        title_row = QHBoxLayout()
        title_row.setSpacing(0)
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.addWidget(logo)
        title_row.addWidget(title)

        header_left = QVBoxLayout()
        header_left.setSpacing(2)
        header_left.setContentsMargins(0, 0, 0, 0)
        header_left.addLayout(title_row)
        header_left.addWidget(subtitle)

        header.addLayout(header_left)
        header.addStretch()

        self.clock = QLabel(now_str())
        self.clock.setObjectName("clock")
        header.addWidget(self.clock)
        content.addLayout(header)

        controls = QHBoxLayout()
        controls.setSpacing(10)

        self.refresh_spin = QDoubleSpinBox()
        self.refresh_spin.setDecimals(2)
        self.refresh_spin.setSingleStep(0.25)
        self.refresh_spin.setRange(0.25, 10.0)
        self.refresh_spin.setValue(REFRESH_SECONDS_DEFAULT)

        self.auto_check = QCheckBox("Auto refresh")
        self.auto_check.setChecked(True)

        self.status = QLabel("Ready")
        self.status.setObjectName("status")

        controls.addWidget(QLabel("Refresh (sec)"))
        controls.addWidget(self.refresh_spin)
        controls.addWidget(self.auto_check)
        controls.addStretch()
        controls.addWidget(self.status)
        content.addLayout(controls)

        splitter = QSplitter(Qt.Horizontal)
        content.addWidget(splitter, 1)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setSpacing(10)
        left_layout.setContentsMargins(0, 0, 0, 0)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setSpacing(10)
        right_layout.setContentsMargins(0, 0, 0, 0)
        splitter.addWidget(right)

        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(0, 7)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([1120, 380])
        right.setMaximumWidth(470)

        sig_box = QGroupBox("Performance Signature")
        sig_layout = QGridLayout(sig_box)
        sig_layout.setHorizontalSpacing(10)
        sig_layout.setVerticalSpacing(10)

        self.sig_score = MetricCard("Efficiency Score")
        self.sig_mode = MetricCard("Mode")
        self.sig_baseline = MetricCard("24h Baseline")
        self.sig_delta = MetricCard("Historical Delta")
        self.sig_r_forecast = MetricCard("R Forecast")
        self.sig_r_signal = MetricCard("Analytics Engine")
        for card in (
            self.sig_score,
            self.sig_mode,
            self.sig_baseline,
            self.sig_delta,
            self.sig_r_forecast,
            self.sig_r_signal,
        ):
            card.set_roomy(True)
            card.setMinimumHeight(112)
        sig_layout.addWidget(self.sig_score, 0, 0)
        sig_layout.addWidget(self.sig_mode, 0, 1)
        sig_layout.addWidget(self.sig_baseline, 1, 0)
        sig_layout.addWidget(self.sig_delta, 1, 1)
        sig_layout.addWidget(self.sig_r_forecast, 2, 0)
        sig_layout.addWidget(self.sig_r_signal, 2, 1)

        self.event_label = QLabel("Recent events (last 20 min): none")
        self.event_label.setObjectName("eventLabel")
        self.event_label.setWordWrap(True)
        sig_layout.addWidget(self.event_label, 3, 0, 1, 2)
        left_layout.addWidget(sig_box)
        sig_box.setMaximumHeight(408)

        fingerprint_box = QGroupBox("Session Fingerprint")
        fingerprint_layout = QVBoxLayout(fingerprint_box)
        fingerprint_layout.setContentsMargins(14, 14, 14, 14)
        fingerprint_layout.setSpacing(12)

        self.fp_hero = QFrame()
        self.fp_hero.setObjectName("fingerprintHero")
        hero_layout = QGridLayout(self.fp_hero)
        hero_layout.setContentsMargins(22, 18, 22, 18)
        hero_layout.setHorizontalSpacing(12)
        hero_layout.setVerticalSpacing(4)

        self.fp_eyebrow = QLabel("Session Match")
        self.fp_eyebrow.setObjectName("fingerprintEyebrow")
        self.fp_workload_value = QLabel("Unknown")
        self.fp_workload_value.setObjectName("fingerprintWorkload")
        self.fp_baseline_text = QLabel("Closest baseline pending")
        self.fp_baseline_text.setObjectName("fingerprintBaseline")

        badge_col = QVBoxLayout()
        badge_col.setSpacing(8)
        badge_col.setContentsMargins(0, 0, 0, 0)
        self.fp_conf_badge = QLabel("Confidence --")
        self.fp_conf_badge.setObjectName("fingerprintBadge")
        self.fp_state_badge = QLabel("Preview")
        self.fp_state_badge.setObjectName("fingerprintStateBadge")
        self.fp_state_badge.setProperty("severity", "preview")
        badge_col.addWidget(self.fp_conf_badge, 0, Qt.AlignRight)
        badge_col.addWidget(self.fp_state_badge, 0, Qt.AlignRight)
        badge_col.addStretch(1)

        hero_layout.addWidget(self.fp_eyebrow, 0, 0)
        hero_layout.addLayout(badge_col, 0, 1, 3, 1)
        hero_layout.addWidget(self.fp_workload_value, 1, 0)
        hero_layout.addWidget(self.fp_baseline_text, 2, 0)
        hero_layout.setColumnStretch(0, 1)
        fingerprint_layout.addWidget(self.fp_hero)

        stats_row = QHBoxLayout()
        stats_row.setSpacing(12)
        self.fp_confidence = FingerprintTile("Confidence")
        self.fp_anomaly = FingerprintTile("Anomaly Score")
        self.fp_similarity = FingerprintTile("Baseline Similarity")
        for card in (self.fp_confidence, self.fp_anomaly, self.fp_similarity):
            card.setMinimumHeight(112)
            stats_row.addWidget(card)
        fingerprint_layout.addLayout(stats_row)

        self.fp_summary_box = QFrame()
        self.fp_summary_box.setObjectName("fingerprintSummaryBox")
        summary_layout = QVBoxLayout(self.fp_summary_box)
        summary_layout.setContentsMargins(18, 14, 18, 14)
        summary_layout.setSpacing(6)
        self.fp_summary_title = QLabel("Readout")
        self.fp_summary_title.setObjectName("fingerprintSummaryTitle")
        self.fp_summary = QLabel("Fingerprinting will appear after the first analytics pass.")
        self.fp_summary.setObjectName("fingerprintSummary")
        self.fp_summary.setWordWrap(True)
        summary_layout.addWidget(self.fp_summary_title)
        summary_layout.addWidget(self.fp_summary)
        fingerprint_layout.addWidget(self.fp_summary_box)
        left_layout.addWidget(fingerprint_box)
        fingerprint_box.setMaximumHeight(344)

        replay_box = QGroupBox("Replay Timeline")
        replay_layout = QVBoxLayout(replay_box)
        replay_layout.setSpacing(6)
        self.replay_feed = QLabel("Replay feed is warming up.")
        self.replay_feed.setObjectName("replayFeed")
        self.replay_feed.setWordWrap(True)
        replay_layout.addWidget(self.replay_feed)
        left_layout.addWidget(replay_box)
        replay_box.setMaximumHeight(150)

        ambient_box = QGroupBox("Live Usage")
        ambient_layout = QVBoxLayout(ambient_box)
        ambient_layout.setSpacing(10)
        ambient_layout.setContentsMargins(10, 10, 10, 12)

        self.ambient_summary = QLabel("Live usage at a glance.")
        self.ambient_summary.setObjectName("ambientSummary")
        self.ambient_summary.setWordWrap(True)
        self.ambient_summary.setVisible(False)
        ambient_layout.addWidget(self.ambient_summary)

        gauge_row = QHBoxLayout()
        gauge_row.setSpacing(26)
        self.gauge_cpu = CircleGauge("CPU")
        self.gauge_gpu = CircleGauge("GPU")
        self.gauge_ram = CircleGauge("RAM")
        gauge_row.addWidget(self.gauge_cpu)
        gauge_row.addWidget(self.gauge_gpu)
        gauge_row.addWidget(self.gauge_ram)
        ambient_layout.addLayout(gauge_row)

        ambient_layout.addSpacing(56)

        self.train = TrainWidget()
        ambient_layout.addWidget(self.train)
        left_layout.insertWidget(0, ambient_box)
        ambient_box.setMaximumHeight(392)

        actions_box = QGroupBox("Actions")
        actions_layout = QVBoxLayout(actions_box)
        actions_layout.setSpacing(6)

        self.proc_table = QTableWidget(0, 3)
        self.proc_table.setHorizontalHeaderLabels(["PID", "Name", "Mem (MB)"])
        self.proc_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.proc_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.proc_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.proc_table.setFocusPolicy(Qt.StrongFocus)
        self.proc_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.proc_table.setMinimumHeight(280)
        self.proc_table.setMaximumHeight(420)
        actions_layout.addWidget(self.proc_table)

        btn_row = QHBoxLayout()
        self.btn_refresh = HoverButton("Refresh List")
        self.btn_kill = HoverButton("End Selected")
        self.btn_quick = HoverButton("Close Common Apps")
        btn_row.addWidget(self.btn_refresh)
        btn_row.addWidget(self.btn_kill)
        btn_row.addWidget(self.btn_quick)
        actions_layout.addLayout(btn_row)

        self.btn_temp = HoverButton("Clear User Temp")
        actions_layout.addWidget(self.btn_temp)
        right_layout.addWidget(actions_box)

        self.gpu_box = QGroupBox("GPU Activity")
        gpu_layout = QVBoxLayout(self.gpu_box)
        gpu_layout.setSpacing(6)

        self.gpu_label = QLabel("No GPU activity detected.")
        self.gpu_label.setWordWrap(True)
        self.gpu_label.setObjectName("gpuLabel")
        gpu_layout.addWidget(self.gpu_label)

        self.gpu_box.setVisible(False)
        right_layout.addWidget(self.gpu_box)
        self.gpu_box.setMaximumHeight(92)

        insight_box = QGroupBox("Insight Engine")
        insight_layout = QVBoxLayout(insight_box)
        insight_layout.setSpacing(6)

        self.insight_title = QLabel("Efficiency -- | Stability --")
        self.insight_title.setObjectName("insightTitle")

        self.insight_body = QLabel("")
        self.insight_body.setWordWrap(True)
        self.insight_body.setObjectName("insightBody")

        insight_layout.addWidget(self.insight_title)
        insight_layout.addWidget(self.insight_body)
        right_layout.addWidget(insight_box)

        info_box = QGroupBox("Environment")
        info_layout = QVBoxLayout(info_box)
        info_layout.setSpacing(6)

        self.env_label = QLabel("")
        self.env_label.setObjectName("envLabel")
        info_layout.addWidget(self.env_label)
        right_layout.addWidget(info_box)

        advisor_box = QGroupBox("Advisor")
        advisor_layout = QVBoxLayout(advisor_box)
        advisor_layout.setSpacing(6)

        self.advisor_label = QLabel("")
        self.advisor_label.setWordWrap(True)
        self.advisor_label.setObjectName("advisorLabel")
        advisor_layout.addWidget(self.advisor_label)

        self.btn_cleanup = HoverButton("One-Click Cleanup")
        self.btn_cleanup.setObjectName("advisorButton")
        advisor_layout.addWidget(self.btn_cleanup)
        right_layout.addWidget(advisor_box)
        left_layout.addStretch(1)
        right_layout.addStretch(1)

        self.btn_refresh.clicked.connect(self.refresh_process_list)
        self.btn_kill.clicked.connect(self.kill_selected)
        self.btn_quick.clicked.connect(self.close_common)
        self.btn_temp.clicked.connect(self.clear_temp)
        self.btn_cleanup.clicked.connect(self.one_click_cleanup)

        self.live_timer = QTimer(self)
        self.live_timer.timeout.connect(self.update_live_fast)
        self.live_timer.start(LIVE_REFRESH_MS)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.request_refresh)
        self.timer.start(max(150, int(self.refresh_spin.value() * 1000)))

        self.process_timer = QTimer(self)
        self.process_timer.timeout.connect(self.refresh_process_list)
        self.process_timer.start(15000)

        self.refresh_spin.valueChanged.connect(self.reset_timer)
        self.auto_check.stateChanged.connect(self.toggle_auto)

        self.scrolling_until = 0.0
        self.worker_busy = False
        self.last_mode = None
        self.last_grade = None
        self.db_store = None
        self.db_status = "History DB offline"
        self.db_baseline = None
        self.r_analytics = None
        self.session_fingerprint = None
        self.replay_events = []
        self._sample_counter = 0
        self.latest_gpu_total = 0.0
        self.latest_gpu_top = []
        self.latest_live_fast = None
        self.display_cpu = None
        self.display_gpu = None
        self.display_ram = None
        self.action_thread = None
        self.action_worker = None
        self.action_busy = False
        self._action_handler = None

        if DesktopTelemetryStore is not None:
            try:
                self.db_store = DesktopTelemetryStore.from_env()
                if self.db_store is not None:
                    self.db_store.ensure_ready()
                    self.db_status = f"History DB live | Session {self.db_store.session_id[:8]}"
                    self.db_baseline = self.db_store.fetch_recent_baseline(hours=24)
                    self.r_analytics = self.db_store.fetch_r_analytics()
                    self.session_fingerprint = self.db_store.fetch_workload_fingerprint()
                    self.replay_events = self.db_store.fetch_replay_events(limit=12)
                    self.db_store.run_r_analytics_async()
            except Exception as e:
                self.db_store = None
                self.db_status = f"History DB unavailable: {e}"

        # IMPORTANT FIX: persistent thread + persistent worker (no per-refresh thread creation)
        self.worker_thread = QThread(self)
        self.worker = DataWorker()
        self.worker.moveToThread(self.worker_thread)
        self.worker.finished.connect(self.on_data_ready, Qt.QueuedConnection)
        self.worker_thread.start()

        self.refresh_process_list()
        self.request_refresh()
        self.setup_animations()

        self.scroll_area.verticalScrollBar().valueChanged.connect(self.mark_scrolling)
        self.scroll_area.horizontalScrollBar().valueChanged.connect(self.mark_scrolling)

    def closeEvent(self, event):
        # Clean shutdown of worker thread
        try:
            if hasattr(self, "worker") and self.worker:
                self.worker.cancel()
            if hasattr(self, "action_thread") and self.action_thread:
                self.action_thread.quit()
                self.action_thread.wait(1000)
            if hasattr(self, "worker_thread") and self.worker_thread:
                self.worker_thread.quit()
                self.worker_thread.wait(1500)
            if hasattr(self, "db_store") and self.db_store:
                self.db_store.close()
        except Exception:
            pass
        super().closeEvent(event)

    def setup_animations(self):
        effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(effect)
        self.fade_anim = QPropertyAnimation(effect, b"opacity", self)
        self.fade_anim.setDuration(350)
        self.fade_anim.setStartValue(0.0)
        self.fade_anim.setEndValue(1.0)
        self.fade_anim.setEasingCurve(QEasingCurve.OutCubic)
        self.fade_anim.start()

    def mark_scrolling(self):
        self.scrolling_until = time.time() + 0.25

    def set_action_controls_enabled(self, enabled: bool):
        for btn in (self.btn_refresh, self.btn_kill, self.btn_quick, self.btn_temp, self.btn_cleanup):
            btn.setEnabled(enabled)

    def run_action_async(self, status_text: str, fn, on_done):
        if self.action_busy:
            self.status.setText("Action already running...")
            return

        self.action_busy = True
        self._action_handler = on_done
        self.set_action_controls_enabled(False)
        self.status.setText(status_text)

        self.action_thread = QThread(self)
        self.action_worker = ActionWorker(fn)
        self.action_worker.moveToThread(self.action_thread)
        self.action_thread.started.connect(self.action_worker.run)
        self.action_worker.finished.connect(self.on_action_finished, Qt.QueuedConnection)
        self.action_thread.start()

    def on_action_finished(self, result):
        handler = self._action_handler
        self._action_handler = None
        self.action_busy = False
        self.set_action_controls_enabled(True)

        if self.action_thread is not None:
            self.action_thread.quit()
            self.action_thread.wait(800)
            self.action_thread.deleteLater()
            self.action_thread = None
        self.action_worker = None

        if isinstance(result, Exception):
            self.status.setText(f"Action failed: {result}")
            return

        if handler is not None:
            handler(result)

    def update_live_fast(self):
        gpu_val = clamp(self.latest_gpu_total or 0.0, 0.0, 100.0)
        live = get_live_fast(gpu_total=gpu_val)
        self.latest_live_fast = live
        self.live_fast_hist.append(
            {
                "time": live["time"],
                "cpu": float(live.get("cpu", 0.0)),
                "ram": float(live.get("ram", 0.0)),
                "gpu": gpu_val,
            }
        )
        cpu_display = self.display_cpu if self.display_cpu is not None else live.get("cpu", 0.0)
        gpu_display = self.display_gpu if self.display_gpu is not None else gpu_val
        ram_display = self.display_ram if self.display_ram is not None else live.get("ram", 0.0)
        self.clock.setText(now_str())
        self.display_cpu = smooth_toward(cpu_display, live.get("cpu", 0.0), CPU_INPUT_SMOOTH_ALPHA)
        self.display_gpu = smooth_toward(gpu_display, gpu_val, GPU_INPUT_SMOOTH_ALPHA)
        self.display_ram = smooth_toward(ram_display, live.get("ram", 0.0), RAM_INPUT_SMOOTH_ALPHA)

        self.gauge_cpu.set_value(self.display_cpu)
        self.gauge_gpu.set_value(self.display_gpu)
        self.gauge_ram.set_value(self.display_ram)
        self.train.set_load((self.display_cpu * 0.5) + (self.display_gpu * 0.3) + (self.display_ram * 0.2))
        self.train.set_metrics(self.display_cpu, self.display_gpu, self.display_ram)

    def reset_timer(self):
        self.timer.stop()
        self.timer.start(max(150, int(self.refresh_spin.value() * 1000)))

    def toggle_auto(self):
        if self.auto_check.isChecked():
            self.live_timer.start(LIVE_REFRESH_MS)
            self.reset_timer()
        else:
            self.live_timer.stop()
            self.timer.stop()

    def request_refresh(self):
        if self.worker_busy:
            return
        self.worker_busy = True
        # Persistent worker: trigger sampling via signal
        self.worker.request.emit()

    def build_preview_fingerprint(self, live):
        process_names = [(name or "").lower() for _, name, _ in self.last_process_rows[:10]]
        cpu = float(live.get("cpu", 0.0))
        ram = float(live.get("ram", 0.0))
        gpu = float(self.latest_gpu_total or 0.0)

        if any(x in " ".join(process_names) for x in ("roblox", "steam", "epic", "riot", "valorant")) or gpu >= 35:
            label = "gaming"
            confidence = 72 if gpu >= 35 else 64
            summary = "Early read: session looks gaming-like because GPU load or game processes are already visible."
        elif any(x in " ".join(process_names) for x in ("code.exe", "python", "powershell", "rstudio")):
            label = "coding"
            confidence = 66
            summary = "Early read: session looks coding-like because development tools are leading the process mix."
        elif any(x in " ".join(process_names) for x in ("chrome.exe", "msedge.exe", "firefox.exe")) and gpu < 18:
            label = "browsing"
            confidence = 62
            summary = "Early read: session looks browsing-heavy because browser processes dominate without much GPU pressure."
        elif cpu < 15 and ram < 55 and gpu < 8:
            label = "idle"
            confidence = 74
            summary = "Early read: session still looks idle or very light."
        elif cpu >= 55 or ram >= 80 or gpu >= 25:
            label = "mixed_heavy"
            confidence = 60
            summary = "Early read: session looks mixed-heavy because multiple resources are elevated together."
        else:
            label = "unknown"
            confidence = 45
            summary = "Analytics Engine is still building a stable workload fingerprint from the first samples."

        return {
            "label": label,
            "confidence": confidence,
            "summary": summary,
        }

    def on_data_ready(self, payload):
        live = payload.get("live", {})
        gpu_total = payload.get("gpu_total")
        gpu_top = payload.get("gpu_top", [])
        self.latest_gpu_total = 0.0 if gpu_total is None else float(gpu_total)
        self.latest_gpu_top = gpu_top

        self.worker_busy = False
        self.history.append(live)
        if gpu_total is not None:
            self.gpu_hist.append({"time": live["time"], "gpu": gpu_total})
        self._sample_counter += 1
        if self.db_store is not None:
            try:
                process_rows = self.last_process_rows[:12]
                self.db_store.log_sample(live, gpu_total, gpu_top, process_rows)
                if self._sample_counter <= 14 or self.session_fingerprint is None or not self.replay_events or self._sample_counter % 3 == 0:
                    self.db_store.run_r_analytics_async()
                if self._sample_counter <= 20 or self.session_fingerprint is None or not self.replay_events or self._sample_counter % 2 == 0:
                    self.r_analytics = self.db_store.fetch_r_analytics()
                    self.session_fingerprint = self.db_store.fetch_workload_fingerprint()
                    self.replay_events = self.db_store.fetch_replay_events(limit=12)
                if self._sample_counter % 8 == 0:
                    self.db_baseline = self.db_store.fetch_recent_baseline(hours=24)
                self.db_status = f"History DB live | Session {self.db_store.session_id[:8]} | Samples {self.db_store.sample_count}"
            except Exception as e:
                self.db_status = f"History DB paused: {e}"

        # Always update clock + core visuals (IMPORTANT FIX: no “looks frozen” while scrolling)
        self.clock.setText(now_str())

        if gpu_total is None:
            gpu_total = 0.0
            gpu_top = []
        gpu_val = clamp(gpu_total, 0.0, 100.0)

        self.display_cpu = smooth_toward(self.display_cpu, live.get("cpu", 0.0), CPU_INPUT_SMOOTH_ALPHA)
        self.display_gpu = smooth_toward(self.display_gpu, gpu_val, GPU_INPUT_SMOOTH_ALPHA)
        self.display_ram = smooth_toward(self.display_ram, live.get("ram", 0.0), RAM_INPUT_SMOOTH_ALPHA)

        self.gauge_cpu.set_value(self.display_cpu)
        self.gauge_gpu.set_value(self.display_gpu)
        self.gauge_ram.set_value(self.display_ram)
        self.train.set_load((self.display_cpu * 0.5) + (self.display_gpu * 0.3) + (self.display_ram * 0.2))
        self.train.set_metrics(self.display_cpu, self.display_gpu, self.display_ram)

        score, score_label = efficiency_score(self.history, live)

        r5 = recent_window(self.history, minutes=5)
        cpu_avg = safe_mean([x["cpu"] for x in r5]) if r5 else 0.0
        ram_avg = safe_mean([x["ram"] for x in r5]) if r5 else 0.0
        mode = usage_mode(cpu_avg, ram_avg, 0.0)

        self.sig_score.set_value(score_label, "last 5 min")
        self.sig_mode.set_value(mode, "last 5 min")
        if self.db_baseline is not None and self.db_baseline.samples > 0:
            self.sig_baseline.set_value(
                f"CPU {self.db_baseline.avg_cpu:.0f}% / RAM {self.db_baseline.avg_ram:.0f}%",
                f"{self.db_baseline.samples} samples across {self.db_baseline.sessions} session(s)",
            )
            delta_cpu = live.get("cpu", 0.0) - self.db_baseline.avg_cpu
            delta_ram = live.get("ram", 0.0) - self.db_baseline.avg_ram
            self.sig_delta.set_value(
                f"CPU {delta_cpu:+.0f}% | RAM {delta_ram:+.0f}%",
                "vs 24h baseline",
            )
        else:
            self.sig_baseline.set_value("Warming up", "need more saved history")
            self.sig_delta.set_value("N/A", "waiting for baseline")

        if self.r_analytics is not None:
            if self.r_analytics.forecast_battery_hours is not None:
                self.sig_r_forecast.set_value(
                    f"{self.r_analytics.forecast_battery_hours:.1f} hr left",
                    f"R battery forecast | drain {self.r_analytics.avg_drain_per_hour:.1f}%/hr",
                )
            else:
                self.sig_r_forecast.set_value(
                    f"RAM peak {self.r_analytics.forecast_ram_peak or 0.0:.0f}%",
                    "R forecast waiting on battery slope",
                )
            anomaly_sub = self.r_analytics.anomaly_text or "R analytics aligned with your norm"
            self.sig_r_signal.set_value(
                f"Score {self.r_analytics.pulse_score:.0f}/100",
                anomaly_sub,
            )
        else:
            if self._sample_counter < 3:
                self.sig_r_forecast.set_value("Starting", "R engine is collecting the first few samples")
                self.sig_r_signal.set_value("Starting", "Personalized analytics will appear after the first samples")
            else:
                self.sig_r_forecast.set_value("Running", "R engine is computing forecast output")
                self.sig_r_signal.set_value("Running", "R analytics are being generated for this session")

        preview_fp = self.build_preview_fingerprint(live)
        if self.session_fingerprint is not None:
            self.fp_workload_value.setText(str(self.session_fingerprint.workload_label).replace("_", " ").title())
            self.fp_baseline_text.setText(
                f"Closest baseline: {self.session_fingerprint.personal_baseline_label.replace('_', ' ')}"
            )
            self.fp_confidence.set_content(
                f"{self.session_fingerprint.classification_confidence:.0f}%",
                "classification confidence",
            )
            self.fp_anomaly.set_content(
                f"{self.session_fingerprint.anomaly_score:.0f}/100",
                "personal anomaly score",
            )
            self.fp_similarity.set_content(
                f"{self.session_fingerprint.baseline_similarity:.0f}%",
                f"CPU {self.session_fingerprint.avg_cpu_vs_baseline:+.0f} | RAM {self.session_fingerprint.avg_ram_vs_baseline:+.0f}",
            )
            self.fp_conf_badge.setText(f"{self.session_fingerprint.classification_confidence:.0f}% confidence")
            state_text, state_level = fingerprint_badge_state(self.session_fingerprint.anomaly_score)
            self.fp_state_badge.setText(state_text)
            self.fp_state_badge.setProperty("severity", state_level)
            self.fp_state_badge.style().unpolish(self.fp_state_badge)
            self.fp_state_badge.style().polish(self.fp_state_badge)
            self.fp_summary.setText(
                self.session_fingerprint.summary_text or "Fingerprint computed. Summary text is not available yet."
            )
        else:
            self.fp_workload_value.setText(preview_fp["label"].replace("_", " ").title())
            self.fp_baseline_text.setText("Closest baseline: still calibrating")
            self.fp_confidence.set_content(f"{preview_fp['confidence']:.0f}%", "confidence will refine")
            self.fp_anomaly.set_content("--", "waiting for anomaly model")
            self.fp_similarity.set_content("--", "waiting for baseline comparison")
            self.fp_conf_badge.setText(f"{preview_fp['confidence']:.0f}% confidence")
            state_text, state_level = fingerprint_badge_state(preview=True)
            self.fp_state_badge.setText(state_text)
            self.fp_state_badge.setProperty("severity", state_level)
            self.fp_state_badge.style().unpolish(self.fp_state_badge)
            self.fp_state_badge.style().polish(self.fp_state_badge)
            self.fp_summary.setText(preview_fp["summary"])

        r10 = recent_window(self.history, minutes=10)
        fast10 = recent_window(self.live_fast_hist, minutes=10)
        cpu_vals_10 = [x["cpu"] for x in fast10] or [x["cpu"] for x in r10]
        ram_vals_10 = [x["ram"] for x in fast10] or [x["ram"] for x in r10]

        cpu_std = safe_std(cpu_vals_10)
        ram_std = safe_std(ram_vals_10)
        stability = clamp(100.0 - (cpu_std * 1.2 + ram_std * 1.2), 0.0, 100.0)

        score_val = score if score is not None else 0.0
        grade = (
            "A" if (score_val >= 80 and stability >= 75)
            else "B" if score_val >= 70
            else "C" if score_val >= 55
            else "D"
        )

        events = detect_events(self.history, self.gpu_hist, limit=4)
        now_t = live["time"].strftime("%H:%M:%S")

        if self.last_mode != mode:
            events.insert(0, (now_t, f"Mode changed to {mode}"))
            self.last_mode = mode
        if self.last_grade != grade:
            events.insert(0, (now_t, f"Session state shifted to {mode.lower()}"))
            self.last_grade = grade
        if gpu_top:
            top_pid, top_name, top_util = gpu_top[0]
            events.insert(0, (now_t, f"Top GPU app: {top_name} {top_util:.0f}%"))

        if events:
            lines = [f"{t} {e}" for t, e in events[:4]]
            self.event_label.setText("Recent events (last 20 min): " + " | ".join(lines))
        else:
            self.event_label.setText(
                f"Recent events (last 20 min): Calm — CPU {live['cpu']:.0f}%, "
                f"RAM {live['ram']:.0f}%, GPU {gpu_val:.0f}%."
            )

        if self.replay_events:
            replay_lines = []
            for event in self.replay_events[:5]:
                if event.event_time is not None:
                    ts = event.event_time.astimezone().strftime("%I:%M %p").lstrip("0")
                else:
                    ts = "--:--"
                impact = f" | impact {event.impact_score:.0f}" if event.impact_score else ""
                metric_shift = ""
                if event.metric_before is not None and event.metric_after is not None:
                    metric_shift = f" ({event.metric_before:.0f} -> {event.metric_after:.0f})"
                replay_lines.append(f"{ts} | {event.event_description}{metric_shift}{impact}")
            self.replay_feed.setText("\n".join(replay_lines))
        else:
            fallback_items = [f"{t} | {e}" for t, e in events[:4]]
            if not fallback_items:
                fallback_items = ["Collecting enough samples to build replay events..."]
            self.replay_feed.setText("\n".join(fallback_items))

        env_parts = [f"OS: {platform.system()} {platform.release()}", self.db_status]
        if self.db_baseline is not None and self.db_baseline.samples > 0:
            env_parts.append(
                f"24h baseline CPU {self.db_baseline.avg_cpu:.0f}% | RAM {self.db_baseline.avg_ram:.0f}% | GPU {self.db_baseline.avg_gpu:.0f}%"
            )
        if self.session_fingerprint is not None:
            env_parts.append(
                f"Fingerprint {self.session_fingerprint.workload_label} {self.session_fingerprint.classification_confidence:.0f}%"
            )
        if self.r_analytics is not None:
            env_parts.append(f"Analytics Engine live | Score {self.r_analytics.pulse_score:.0f}")
        self.env_label.setText(" | ".join(env_parts))

        lines = [f"Total GPU: {gpu_total:.0f}%"]
        if gpu_top:
            for pid, name, util in gpu_top:
                lines.append(f"{name} (PID {pid}) {util:.0f}%")
        self.gpu_label.setText(" | ".join(lines))
        self.gpu_box.setVisible(True)

        cpu_trend = trend_label(cpu_vals_10)
        ram_trend = trend_label(ram_vals_10)

        peak_cpu = max(cpu_vals_10) if cpu_vals_10 else 0.0
        peak_ram = max(ram_vals_10) if ram_vals_10 else 0.0
        peak_temp = max([x["temp_c"] for x in r10 if x.get("temp_c") is not None], default=None)

        if self.r_analytics is not None:
            self.insight_title.setText(
                f"Efficiency {score_val:.0f} | Stability {stability:.0f} | Analytics {self.r_analytics.pulse_score:.0f}"
            )
        else:
            self.insight_title.setText(
                f"Efficiency {score_val:.0f} | Stability {stability:.0f}"
            )

        insight_lines = [f"Trends: CPU {cpu_trend}, RAM {ram_trend}."]
        if peak_temp is None:
            insight_lines.append(f"Peaks (10 min): CPU {peak_cpu:.0f}%, RAM {peak_ram:.0f}%.")
        else:
            insight_lines.append(
                f"Peaks (10 min): CPU {peak_cpu:.0f}%, RAM {peak_ram:.0f}%, Temp {peak_temp:.0f}C."
            )
        if self.db_baseline is not None and self.db_baseline.samples > 0:
            insight_lines.append(
                f"Historical baseline (24h): CPU {self.db_baseline.avg_cpu:.0f}%, "
                f"RAM {self.db_baseline.avg_ram:.0f}%, GPU {self.db_baseline.avg_gpu:.0f}%."
            )
        if self.session_fingerprint is not None and self.session_fingerprint.summary_text:
            insight_lines.append(self.session_fingerprint.summary_text)
        if self.r_analytics is not None and self.r_analytics.explain_text:
            insight_lines.append(self.r_analytics.explain_text)
        if live.get("plugged") is False:
            insight_lines.append("Battery mode detected. Consider reducing background apps.")
        self.insight_body.setText(" ".join(insight_lines))

        advisor_lines = []
        if live.get("cpu", 0.0) >= 85:
            advisor_lines.append("CPU is very high. Close heavy apps or use One-Click Cleanup.")
        if live.get("ram", 0.0) >= 85:
            advisor_lines.append("RAM is high. End memory-heavy apps from the list.")
        if live.get("temp_c") is not None and live["temp_c"] >= 85:
            advisor_lines.append("Temperature is high. Reduce load or improve airflow.")
        if live.get("plugged") is False and live.get("battery") is not None and live["battery"] <= 20:
            advisor_lines.append("Battery is low. Plug in or reduce background apps.")
        if self.db_baseline is not None and self.db_baseline.samples > 0:
            if live.get("cpu", 0.0) >= self.db_baseline.avg_cpu + 15:
                advisor_lines.append("CPU is well above your 24-hour baseline. This session is heavier than normal.")
            if live.get("ram", 0.0) >= self.db_baseline.avg_ram + 12:
                advisor_lines.append("RAM is above your recent baseline. Memory-heavy work is likely active.")
        if self.r_analytics is not None:
            if self.r_analytics.anomaly_flag and self.r_analytics.anomaly_text:
                advisor_lines.insert(0, self.r_analytics.anomaly_text)
            if self.r_analytics.drivers_text:
                advisor_lines.append(self.r_analytics.drivers_text)
        if self.session_fingerprint is not None and self.session_fingerprint.anomaly_score >= 60:
            advisor_lines.insert(0, self.session_fingerprint.summary_text)

        if not advisor_lines:
            advisor_lines = ["All systems look stable. If you want a quick boost, use One-Click Cleanup."]
        self.advisor_label.setText(" ".join(advisor_lines))

    def refresh_process_list(self):
        rows = list_processes_snapshot(limit=60)
        self.last_process_rows = rows
        self.proc_table.setRowCount(len(rows))
        for i, (pid, name, mem_mb) in enumerate(rows):
            self.proc_table.setItem(i, 0, QTableWidgetItem(str(pid)))
            self.proc_table.setItem(i, 1, QTableWidgetItem(name))
            self.proc_table.setItem(i, 2, QTableWidgetItem(f"{mem_mb:.1f}"))
        if self.db_store is not None:
            self.status.setText(f"Process list refreshed | History live")
        else:
            self.status.setText("Process list refreshed")

    def kill_selected(self):
        sel = self.proc_table.selectionModel()
        rows = sel.selectedRows() if sel else []
        if not rows:
            self.status.setText("No selection")
            return

        pids = set()
        for row in rows:
            it = self.proc_table.item(row.row(), 0)
            if not it:
                continue
            try:
                pids.add(int(it.text()))
            except Exception:
                pass

        if not pids:
            self.status.setText("No valid PID selected")
            return

        ok = QMessageBox.question(
            self,
            "Confirm",
            "End selected processes? Unsaved work may be lost.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ok != QMessageBox.Yes:
            return

        def action():
            killed = 0
            for pid in pids:
                ok2, _ = terminate_pid(pid)
                if ok2:
                    killed += 1
            return killed

        def after(killed):
            self.status.setText(f"Ended {killed} process(es)")
            QTimer.singleShot(120, self.refresh_process_list)

        self.run_action_async("Ending selected process(es)...", action, after)

    def close_common(self):
        ok = QMessageBox.question(
            self,
            "Confirm",
            "Close common background apps (Discord, Teams, Steam, browsers)?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ok != QMessageBox.Yes:
            return
        def after(killed):
            self.status.setText(f"Closed {killed} process(es)")
            QTimer.singleShot(120, self.refresh_process_list)

        self.run_action_async("Closing common apps...", close_common_apps, after)

    def clear_temp(self):
        ok = QMessageBox.question(
            self,
            "Confirm",
            "Clear user temp files? This removes cache files.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ok != QMessageBox.Yes:
            return
        def after(result):
            removed, msg = result
            self.status.setText(f"Temp cleanup: removed {removed} items. {msg}")

        self.run_action_async("Clearing temp files...", clear_user_temp, after)

    def one_click_cleanup(self):
        ok = QMessageBox.question(
            self,
            "Confirm",
            "Close common apps and clear temp files?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ok != QMessageBox.Yes:
            return
        def action():
            killed = close_common_apps()
            removed, msg = clear_user_temp()
            return killed, removed, msg

        def after(result):
            killed, removed, msg = result
            self.status.setText(f"Cleanup: closed {killed} apps, removed {removed} items. {msg}")
            QTimer.singleShot(120, self.refresh_process_list)

        self.run_action_async("Running cleanup...", action, after)


def build_app():
    app = QApplication(sys.argv)

    # IMPORTANT FIX: warm up non-blocking CPU sampling so first read isn't 0/None
    try:
        psutil.cpu_percent(interval=None)
    except Exception:
        pass

    app.setStyleSheet(
        """
        QWidget {
            color: #e5e7eb;
            background-color: #0b1220;
            font-family: 'Bahnschrift', 'Segoe UI', 'Arial', sans-serif;
            font-size: 12px;
        }
        QWidget#appRoot {
            background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 #0b1220, stop:0.5 #0f172a, stop:1 #111827);
        }
        QScrollArea#mainScroll {
            background: transparent;
        }
        QGroupBox {
            border: 1px solid rgba(255,255,255,0.08);
            margin-top: 10px;
            border-radius: 12px;
            background-color: rgba(12, 18, 32, 0.65);
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            subcontrol-position: top left;
            padding: 4px 8px;
            color: #93c5fd;
            font-weight: 600;
        }
        QLabel {
            letter-spacing: 0.2px;
        }
        QFrame#metricCard {
            border: 1px solid rgba(148,163,184,0.18);
            border-radius: 12px;
            padding: 0px;
            background-color: rgba(15, 23, 42, 0.78);
        }
        QFrame#metricCard[cardVariant="feature"] {
            border: 1px solid rgba(96, 165, 250, 0.26);
            background-color: rgba(15, 23, 42, 0.88);
        }
        QFrame#fingerprintHero {
            border: 1px solid rgba(96, 165, 250, 0.24);
            border-radius: 14px;
            background-color: rgba(15, 23, 42, 0.88);
        }
        QFrame#fingerprintTile {
            border: 1px solid rgba(148,163,184,0.16);
            border-radius: 14px;
            background-color: rgba(15, 23, 42, 0.74);
        }
        QFrame#fingerprintSummaryBox {
            border: 1px solid rgba(148,163,184,0.14);
            border-radius: 14px;
            background-color: rgba(15, 23, 42, 0.62);
        }
        QLabel#metricTitle {
            color: #c7d2fe;
            background: transparent;
            border: none;
            padding: 0px;
            margin: 0px;
        }
        QLabel#metricValue {
            color: #f8fafc;
            background: transparent;
            border: none;
            padding: 0px;
            margin: 0px;
        }
        QLabel#metricSub {
            color: #9fb0c9;
            background: transparent;
            border: none;
            padding: 0px;
            margin: 0px;
        }
        QLabel#fingerprintEyebrow {
            color: #8fb8ff;
            font-size: 12px;
            font-weight: 600;
        }
        QLabel#fingerprintWorkload {
            color: #f8fafc;
            font-size: 30px;
            font-weight: 700;
        }
        QLabel#fingerprintBaseline {
            color: #c7d2fe;
            font-size: 13px;
        }
        QLabel#fingerprintBadge {
            color: #dbeafe;
            background-color: rgba(59, 130, 246, 0.16);
            border: 1px solid rgba(96, 165, 250, 0.28);
            border-radius: 10px;
            padding: 5px 10px;
            font-size: 12px;
            font-weight: 600;
        }
        QLabel#fingerprintStateBadge {
            color: #e2e8f0;
            background-color: rgba(148, 163, 184, 0.12);
            border: 1px solid rgba(148, 163, 184, 0.22);
            border-radius: 10px;
            padding: 5px 10px;
            font-size: 12px;
            font-weight: 600;
        }
        QLabel#fingerprintStateBadge[severity="normal"] {
            color: #dcfce7;
            background-color: rgba(34, 197, 94, 0.14);
            border: 1px solid rgba(74, 222, 128, 0.22);
        }
        QLabel#fingerprintStateBadge[severity="watch"] {
            color: #fde68a;
            background-color: rgba(245, 158, 11, 0.14);
            border: 1px solid rgba(251, 191, 36, 0.22);
        }
        QLabel#fingerprintStateBadge[severity="alert"] {
            color: #fecaca;
            background-color: rgba(239, 68, 68, 0.14);
            border: 1px solid rgba(248, 113, 113, 0.22);
        }
        QLabel#fingerprintStateBadge[severity="preview"] {
            color: #cbd5e1;
            background-color: rgba(100, 116, 139, 0.16);
            border: 1px solid rgba(148, 163, 184, 0.20);
        }
        QLabel#fingerprintTileTitle {
            color: #a5b4fc;
            font-size: 12px;
            font-weight: 600;
        }
        QLabel#fingerprintTileValue {
            color: #f8fafc;
            font-size: 22px;
            font-weight: 700;
        }
        QLabel#fingerprintTileDetail {
            color: #9fb0c9;
            font-size: 12px;
        }
        QLabel#appTitle {
            font-size: 28px;
            font-weight: 700;
        }
        QLabel#appLogo {
            color: #38bdf8;
            font-size: 22px;
            font-weight: 700;
        }
        QLabel#appSub {
            color: #9ca3af;
        }
        QLabel#clock {
            color: #a5b4fc;
            font-weight: 600;
        }
        QLabel#status {
            color: #34d399;
        }
        QLabel#eventLabel {
            color: #a5b4fc;
        }
        QLabel#envLabel {
            color: #cbd5f5;
        }
        QLabel#insightTitle {
            color: #e5e7eb;
            font-size: 16px;
            font-weight: 700;
        }
        QLabel#insightBody {
            color: #cbd5f5;
        }
        QLabel#ambientSummary {
            color: #e2e8f0;
        }
        QLabel#advisorLabel {
            color: #e2e8f0;
        }
        QLabel#fingerprintSummaryTitle {
            color: #8fb8ff;
            font-size: 12px;
            font-weight: 600;
        }
        QLabel#fingerprintSummary {
            color: #d8e4ff;
            font-size: 13px;
        }
        QLabel#replayFeed {
            color: #dbe4f5;
            background-color: rgba(15, 23, 42, 0.72);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 10px;
            padding: 10px 12px;
            line-height: 1.35;
        }
        QPushButton#advisorButton {
            background-color: #0f172a;
            border: 1px solid rgba(255,255,255,0.2);
            padding: 6px 10px;
            border-radius: 8px;
            min-height: 34px;
            color: #e5e7eb;
        }
        QPushButton#advisorButton:hover {
            background-color: #162033;
            border-color: #60a5fa;
        }
        QPushButton#advisorButton:disabled {
            background-color: #101827;
            border: 1px solid rgba(148,163,184,0.26);
            color: #94a3b8;
            padding: 6px 10px;
        }
        QPushButton {
            background-color: #111827;
            border: 1px solid rgba(255,255,255,0.12);
            padding: 6px 10px;
            border-radius: 8px;
            min-height: 34px;
            color: #e5e7eb;
        }
        QPushButton:hover {
            background-color: #162033;
            border-color: #60a5fa;
        }
        QPushButton:pressed {
            background-color: #1d2a44;
        }
        QPushButton:disabled {
            background-color: #101827;
            border: 1px solid rgba(148,163,184,0.22);
            color: #94a3b8;
            padding: 6px 10px;
        }
        QHeaderView::section {
            background-color: #111827;
            color: #9ca3af;
            padding: 6px;
            border: none;
        }
        QTableWidget::item:selected {
            background-color: rgba(59, 130, 246, 0.35);
            color: #e5e7eb;
        }
        """
    )

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    build_app()

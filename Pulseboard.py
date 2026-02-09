import os
import sys
import time
import math
import shutil
import psutil
import platform
import subprocess
from collections import deque
from datetime import datetime, timedelta

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
from PySide6.QtGui import QFont, QColor, QPainter, QPen
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
    QSpinBox,
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
)

APP_TITLE = "Pulseboard"
REFRESH_SECONDS_DEFAULT = 2
HISTORY_MAX_POINTS = 300

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


def get_live():
    b = psutil.sensors_battery()

    # IMPORTANT FIX:
    # - avoid blocking 1s sampling (was: Get-Counter SampleInterval 1 or psutil interval=1.0)
    # - keep it non-blocking so UI feels smooth
    cpu = None
    try:
        ps = r"""
        try {
          $c = Get-Counter '\Processor(_Total)\% Processor Time' -MaxSamples 1
          $c.CounterSamples | Select-Object CookedValue | ConvertTo-Json -Compress
        } catch {
          ""
        }
        """
        p = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )
        txt = (p.stdout or "").strip()
        if txt:
            import json

            data = json.loads(txt)
            cpu = (
                float(data["CookedValue"])
                if isinstance(data, dict)
                else float(data[0]["CookedValue"])
            )
    except Exception:
        cpu = None

    if cpu is None:
        # Non-blocking read (requires warm-up call once at startup)
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
        # IMPORTANT FIX: persistent worker thread; request signal triggers sampling
        self.request.connect(self.run, Qt.QueuedConnection)

    def cancel(self):
        self._cancel = True

    def run(self):
        if self._cancel:
            return
        try:
            live = get_live()
            gpu_total, gpu_top = get_gpu_activity()
            if self._cancel:
                return
            self.finished.emit({"live": live, "gpu_total": gpu_total, "gpu_top": gpu_top})
        except Exception:
            self.finished.emit({"live": get_live(), "gpu_total": None, "gpu_top": []})


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
        psutil.wait_procs([p], timeout=2)
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
                    p.wait(timeout=1)
                except Exception:
                    p.kill()
                killed += 1
        except Exception:
            pass
    time.sleep(0.5)
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
        self.title = QLabel(title)
        self.value = QLabel(value)
        self.sub = QLabel(sub)

        self.title.setObjectName("metricTitle")
        self.value.setObjectName("metricValue")
        self.sub.setObjectName("metricSub")

        layout = QVBoxLayout(self)
        layout.addWidget(self.title)
        layout.addWidget(self.value)
        layout.addWidget(self.sub)
        layout.setSpacing(2)

    def set_value(self, v, sub=""):
        self.value.setText(v)
        self.sub.setText(sub)


class TrainWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(60)
        self.steam_phase = 0.0
        self.load = 0.0
        self.cpu = 0.0
        self.gpu = 0.0
        self.ram = 0.0
        self.anim = QTimer(self)
        self.anim.timeout.connect(self.advance)
        self.anim.start(60)

    def set_load(self, load: float):
        self.load = clamp(load, 0.0, 100.0)
        self.update()

    def set_metrics(self, cpu: float, gpu: float, ram: float):
        self.cpu = clamp(cpu, 0.0, 100.0)
        self.gpu = clamp(gpu, 0.0, 100.0)
        self.ram = clamp(ram, 0.0, 100.0)
        self.update()

    def advance(self):
        self.steam_phase += 0.15 + (self.load / 100.0) * 0.35
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

            puff_count = 3 + int(self.load / 20.0)
            for i in range(puff_count):
                t = self.steam_phase + i * 0.7
                drift = math.sin(t * 0.7) * 6
                x = chimney.center().x() + drift + i * 12
                y = base_y - 18 - (t * 6 % 40)
                alpha = max(30, 200 - int((t * 10) % 170))
                size = 5 + (i * 2) + (self.load / 50.0)
                p.setBrush(QColor(148, 163, 184, alpha))
                p.setPen(Qt.NoPen)
                p.drawEllipse(QPointF(x, y), size, size)
        finally:
            p.end()


class CircleGauge(QWidget):
    def __init__(self, label: str):
        super().__init__()
        self.setMinimumSize(110, 110)
        self.label = label
        self.value = 0.0
        self.spin = 0.0
        self.anim = QTimer(self)
        self.anim.timeout.connect(self.advance)
        self.anim.start(60)

    def set_value(self, value: float):
        self.value = clamp(value, 0.0, 100.0)
        self.update()

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
        self.resize(1200, 760)

        self.state = {}
        self.history = deque(maxlen=HISTORY_MAX_POINTS)
        self.gpu_hist = deque(maxlen=HISTORY_MAX_POINTS)

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

        self.refresh_spin = QSpinBox()
        self.refresh_spin.setRange(1, 10)
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
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        sig_box = QGroupBox("Performance Signature")
        sig_layout = QGridLayout(sig_box)
        sig_layout.setHorizontalSpacing(10)
        sig_layout.setVerticalSpacing(10)

        self.sig_score = MetricCard("Efficiency Score")
        self.sig_mode = MetricCard("Mode")
        sig_layout.addWidget(self.sig_score, 0, 0)
        sig_layout.addWidget(self.sig_mode, 0, 1)

        self.event_label = QLabel("Recent events (last 20 min): none")
        self.event_label.setObjectName("eventLabel")
        sig_layout.addWidget(self.event_label, 1, 0, 1, 2)
        left_layout.addWidget(sig_box)

        ambient_box = QGroupBox("Live Usage")
        ambient_layout = QVBoxLayout(ambient_box)
        ambient_layout.setSpacing(8)
        ambient_layout.setContentsMargins(12, 10, 12, 10)

        self.ambient_summary = QLabel("Live usage at a glance.")
        self.ambient_summary.setObjectName("ambientSummary")
        self.ambient_summary.setWordWrap(True)
        self.ambient_summary.setVisible(False)
        ambient_layout.addWidget(self.ambient_summary)

        gauge_row = QHBoxLayout()
        self.gauge_cpu = CircleGauge("CPU")
        self.gauge_gpu = CircleGauge("GPU")
        self.gauge_ram = CircleGauge("RAM")
        gauge_row.addWidget(self.gauge_cpu)
        gauge_row.addWidget(self.gauge_gpu)
        gauge_row.addWidget(self.gauge_ram)
        ambient_layout.addLayout(gauge_row)

        self.train = TrainWidget()
        ambient_layout.addWidget(self.train)
        left_layout.insertWidget(0, ambient_box)

        actions_box = QGroupBox("Actions")
        actions_layout = QVBoxLayout(actions_box)
        actions_layout.setSpacing(8)

        self.proc_table = QTableWidget(0, 3)
        self.proc_table.setHorizontalHeaderLabels(["PID", "Name", "Mem (MB)"])
        self.proc_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.proc_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.proc_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.proc_table.setFocusPolicy(Qt.StrongFocus)
        self.proc_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        actions_layout.addWidget(self.proc_table)

        btn_row = QHBoxLayout()
        self.btn_refresh = QPushButton("Refresh List")
        self.btn_kill = QPushButton("End Selected")
        self.btn_quick = QPushButton("Close Common Apps")
        btn_row.addWidget(self.btn_refresh)
        btn_row.addWidget(self.btn_kill)
        btn_row.addWidget(self.btn_quick)
        actions_layout.addLayout(btn_row)

        self.btn_temp = QPushButton("Clear User Temp")
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

        insight_box = QGroupBox("Insight Engine")
        insight_layout = QVBoxLayout(insight_box)
        insight_layout.setSpacing(6)

        self.insight_title = QLabel("Pulse Grade: --")
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

        self.btn_cleanup = QPushButton("One-Click Cleanup")
        self.btn_cleanup.setObjectName("advisorButton")
        advisor_layout.addWidget(self.btn_cleanup)
        right_layout.addWidget(advisor_box)

        self.btn_refresh.clicked.connect(self.refresh_process_list)
        self.btn_kill.clicked.connect(self.kill_selected)
        self.btn_quick.clicked.connect(self.close_common)
        self.btn_temp.clicked.connect(self.clear_temp)
        self.btn_cleanup.clicked.connect(self.one_click_cleanup)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.request_refresh)
        self.timer.start(self.refresh_spin.value() * 1000)

        self.refresh_spin.valueChanged.connect(self.reset_timer)
        self.auto_check.stateChanged.connect(self.toggle_auto)

        self.scrolling_until = 0.0
        self.worker_busy = False
        self.last_mode = None
        self.last_grade = None

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
            if hasattr(self, "worker_thread") and self.worker_thread:
                self.worker_thread.quit()
                self.worker_thread.wait(1500)
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

    def reset_timer(self):
        self.timer.stop()
        self.timer.start(self.refresh_spin.value() * 1000)

    def toggle_auto(self):
        if self.auto_check.isChecked():
            self.reset_timer()
        else:
            self.timer.stop()

    def request_refresh(self):
        if self.worker_busy:
            return
        self.worker_busy = True
        # Persistent worker: trigger sampling via signal
        self.worker.request.emit()

    def on_data_ready(self, payload):
        live = payload.get("live", {})
        gpu_total = payload.get("gpu_total")
        gpu_top = payload.get("gpu_top", [])

        self.worker_busy = False
        self.history.append(live)
        if gpu_total is not None:
            self.gpu_hist.append({"time": live["time"], "gpu": gpu_total})

        # Always update clock + core visuals (IMPORTANT FIX: no “looks frozen” while scrolling)
        self.clock.setText(now_str())

        if gpu_total is None:
            gpu_total = 0.0
            gpu_top = []
        gpu_val = clamp(gpu_total, 0.0, 100.0)

        self.gauge_cpu.set_value(live.get("cpu", 0.0))
        self.gauge_gpu.set_value(gpu_val)
        self.gauge_ram.set_value(live.get("ram", 0.0))
        self.train.set_load((live.get("cpu", 0.0) * 0.5) + (gpu_val * 0.3) + (live.get("ram", 0.0) * 0.2))
        self.train.set_metrics(live.get("cpu", 0.0), gpu_val, live.get("ram", 0.0))

        # If scrolling, skip the heavier text churn (but keep visuals fluid)
        if time.time() < self.scrolling_until:
            return

        score, score_label = efficiency_score(self.history, live)

        r5 = recent_window(self.history, minutes=5)
        cpu_avg = safe_mean([x["cpu"] for x in r5]) if r5 else 0.0
        ram_avg = safe_mean([x["ram"] for x in r5]) if r5 else 0.0
        mode = usage_mode(cpu_avg, ram_avg, 0.0)

        self.sig_score.set_value(score_label, "last 5 min")
        self.sig_mode.set_value(mode, "last 5 min")

        r10 = recent_window(self.history, minutes=10)
        cpu_vals_10 = [x["cpu"] for x in r10]
        ram_vals_10 = [x["ram"] for x in r10]

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
            events.insert(0, (now_t, f"Pulse Grade now {grade}"))
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

        self.env_label.setText(f"OS: {platform.system()} {platform.release()}")

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

        self.insight_title.setText(
            f"Pulse Grade: {grade} | Efficiency {score_val:.0f} | Stability {stability:.0f}"
        )

        insight_lines = [f"Trends: CPU {cpu_trend}, RAM {ram_trend}."]
        if peak_temp is None:
            insight_lines.append(f"Peaks (10 min): CPU {peak_cpu:.0f}%, RAM {peak_ram:.0f}%.")
        else:
            insight_lines.append(
                f"Peaks (10 min): CPU {peak_cpu:.0f}%, RAM {peak_ram:.0f}%, Temp {peak_temp:.0f}C."
            )
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

        if not advisor_lines:
            advisor_lines = ["All systems look stable. If you want a quick boost, use One-Click Cleanup."]
        self.advisor_label.setText(" ".join(advisor_lines))

    def refresh_process_list(self):
        rows = list_processes_snapshot(limit=60)
        self.proc_table.setRowCount(len(rows))
        for i, (pid, name, mem_mb) in enumerate(rows):
            self.proc_table.setItem(i, 0, QTableWidgetItem(str(pid)))
            self.proc_table.setItem(i, 1, QTableWidgetItem(name))
            self.proc_table.setItem(i, 2, QTableWidgetItem(f"{mem_mb:.1f}"))
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

        killed = 0
        for pid in pids:
            ok2, _ = terminate_pid(pid)
            if ok2:
                killed += 1

        self.status.setText(f"Ended {killed} process(es)")
        self.refresh_process_list()

    def close_common(self):
        ok = QMessageBox.question(
            self,
            "Confirm",
            "Close common background apps (Discord, Teams, Steam, browsers)?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ok != QMessageBox.Yes:
            return
        killed = close_common_apps()
        self.status.setText(f"Closed {killed} process(es)")
        self.refresh_process_list()

    def clear_temp(self):
        ok = QMessageBox.question(
            self,
            "Confirm",
            "Clear user temp files? This removes cache files.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ok != QMessageBox.Yes:
            return
        removed, msg = clear_user_temp()
        self.status.setText(f"Temp cleanup: removed {removed} items. {msg}")

    def one_click_cleanup(self):
        ok = QMessageBox.question(
            self,
            "Confirm",
            "Close common apps and clear temp files?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ok != QMessageBox.Yes:
            return
        killed = close_common_apps()
        removed, msg = clear_user_temp()
        self.status.setText(f"Cleanup: closed {killed} apps, removed {removed} items. {msg}")
        self.refresh_process_list()


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
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 12px;
            padding: 8px;
            background-color: rgba(15, 23, 42, 0.7);
        }
        QLabel#metricTitle {
            color: #9ca3af;
            font-size: 12px;
        }
        QLabel#metricValue {
            color: #e5e7eb;
            font-size: 20px;
            font-weight: 700;
        }
        QLabel#metricSub {
            color: #6b7280;
            font-size: 11px;
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
        QPushButton#advisorButton {
            background-color: #0f172a;
            border: 1px solid rgba(255,255,255,0.2);
            padding: 6px 10px;
            border-radius: 8px;
        }
        QPushButton#advisorButton:hover {
            border-color: #60a5fa;
        }
        QPushButton {
            background-color: #111827;
            border: 1px solid rgba(255,255,255,0.12);
            padding: 6px 10px;
            border-radius: 8px;
        }
        QPushButton:hover {
            border-color: #60a5fa;
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

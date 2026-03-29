import os
import platform
import shutil
import socket
import subprocess
from pathlib import Path
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

try:
    import psycopg
except ImportError:
    psycopg = None


SCHEMA_SQL = """
create table if not exists collector_sessions (
    session_id uuid primary key,
    started_at timestamptz not null default now(),
    host_name text not null,
    platform_name text not null,
    platform_release text not null,
    workload_label text
);

create table if not exists system_metrics (
    id bigserial primary key,
    session_id uuid not null references collector_sessions(session_id) on delete cascade,
    captured_at timestamptz not null,
    cpu_usage numeric(6, 2),
    ram_usage numeric(6, 2),
    gpu_usage numeric(8, 2),
    battery_percent numeric(6, 2),
    plugged_in boolean,
    top_gpu_process text
);

create index if not exists idx_system_metrics_session_time
    on system_metrics (session_id, captured_at desc);

create table if not exists gpu_process_metrics (
    id bigserial primary key,
    session_id uuid not null references collector_sessions(session_id) on delete cascade,
    captured_at timestamptz not null,
    pid integer,
    process_name text,
    gpu_usage numeric(8, 2)
);

create index if not exists idx_gpu_process_metrics_time
    on gpu_process_metrics (captured_at desc);

create table if not exists process_memory_metrics (
    id bigserial primary key,
    session_id uuid not null references collector_sessions(session_id) on delete cascade,
    captured_at timestamptz not null,
    pid integer,
    process_name text,
    memory_mb numeric(12, 2)
);

create index if not exists idx_process_memory_metrics_time
    on process_memory_metrics (captured_at desc);

create table if not exists analytics_session_scores (
    session_id uuid primary key references collector_sessions(session_id) on delete cascade,
    computed_at timestamptz not null default now(),
    efficiency_score numeric(8, 2),
    stability_score numeric(8, 2),
    battery_score numeric(8, 2),
    pulse_score numeric(8, 2),
    pulse_grade text,
    avg_drain_per_hour numeric(8, 2),
    ram_minutes_above_80 numeric(8, 2),
    forecast_battery_hours numeric(8, 2),
    forecast_ram_peak numeric(8, 2),
    anomaly_flag boolean default false,
    anomaly_text text,
    explain_text text,
    drivers_text text
);

create table if not exists analytics_process_impact (
    process_name text primary key,
    computed_at timestamptz not null default now(),
    impact_score numeric(10, 2),
    stress_rate numeric(8, 4),
    avg_memory_mb numeric(12, 2),
    avg_proc_gpu numeric(8, 2)
);

create table if not exists analytics_session_fingerprint (
    session_id uuid primary key references collector_sessions(session_id) on delete cascade,
    computed_at timestamptz not null default now(),
    workload_label text not null,
    classification_confidence numeric(8, 2),
    anomaly_score numeric(8, 2),
    baseline_similarity numeric(8, 2),
    personal_baseline_label text,
    avg_cpu_vs_baseline numeric(8, 2),
    avg_ram_vs_baseline numeric(8, 2),
    avg_gpu_vs_baseline numeric(8, 2),
    battery_drain_vs_baseline numeric(8, 2),
    summary_text text
);

create table if not exists analytics_session_replay_events (
    id bigserial primary key,
    session_id uuid not null references collector_sessions(session_id) on delete cascade,
    event_time timestamptz not null,
    event_type text not null,
    event_description text not null,
    metric_name text,
    metric_before numeric(10, 2),
    metric_after numeric(10, 2),
    impact_score numeric(10, 2)
);

create index if not exists idx_analytics_replay_session_time
    on analytics_session_replay_events (session_id, event_time desc);

create table if not exists analytics_workload_baselines (
    workload_label text not null,
    baseline_version integer not null default 1,
    avg_cpu numeric(8, 2),
    avg_ram numeric(8, 2),
    avg_gpu numeric(8, 2),
    avg_battery_drain numeric(8, 2),
    avg_pulse_score numeric(8, 2),
    avg_session_duration numeric(10, 2),
    common_processes text,
    updated_at timestamptz not null default now(),
    primary key (workload_label, baseline_version)
);
"""


@dataclass
class HistoricalSnapshot:
    avg_cpu: float = 0.0
    avg_ram: float = 0.0
    avg_gpu: float = 0.0
    samples: int = 0
    sessions: int = 0


@dataclass
class RAnalyticsSnapshot:
    pulse_score: float = 0.0
    pulse_grade: str = "N/A"
    efficiency_score: float = 0.0
    stability_score: float = 0.0
    battery_score: float = 0.0
    avg_drain_per_hour: float = 0.0
    ram_minutes_above_80: float = 0.0
    forecast_battery_hours: float | None = None
    forecast_ram_peak: float | None = None
    anomaly_flag: bool = False
    anomaly_text: str = ""
    explain_text: str = ""
    drivers_text: str = ""


@dataclass
class WorkloadFingerprintSnapshot:
    workload_label: str = "unknown"
    classification_confidence: float = 0.0
    anomaly_score: float = 0.0
    baseline_similarity: float = 0.0
    personal_baseline_label: str = "unknown"
    avg_cpu_vs_baseline: float = 0.0
    avg_ram_vs_baseline: float = 0.0
    avg_gpu_vs_baseline: float = 0.0
    battery_drain_vs_baseline: float = 0.0
    summary_text: str = ""


@dataclass
class ReplayEvent:
    event_time: Optional[datetime]
    event_type: str
    event_description: str
    metric_name: str
    metric_before: Optional[float]
    metric_after: Optional[float]
    impact_score: float


class DesktopTelemetryStore:
    def __init__(self, dsn: str, workload_label: str | None = None):
        self.dsn = dsn
        self.workload_label = workload_label or os.getenv("PULSEBOARD_WORKLOAD_LABEL") or "desktop"
        self.session_id = str(uuid.uuid4())
        self.host_name = socket.gethostname()
        self.conn = None
        self.sample_count = 0
        self._rscript_path = None

    @classmethod
    def from_env(cls):
        dsn = os.getenv("PULSEBOARD_DB_DSN", "").strip()
        if not dsn or psycopg is None:
            return None
        return cls(dsn=dsn)

    def utc_now(self):
        return datetime.now(timezone.utc)

    def connect(self):
        if self.conn is None:
            self.conn = psycopg.connect(self.dsn, autocommit=True)
        return self.conn

    def _find_rscript(self):
        if self._rscript_path:
            return self._rscript_path

        env_rscript = os.getenv("PULSEBOARD_RSCRIPT", "").strip()
        if env_rscript and Path(env_rscript).exists():
            self._rscript_path = env_rscript
            return self._rscript_path

        candidate = shutil.which("Rscript") if "shutil" in globals() else None
        if candidate:
            self._rscript_path = candidate
            return self._rscript_path

        base_dir = Path(__file__).resolve().parent
        r_root = Path("C:/Program Files/R")
        if r_root.exists():
            versions = sorted([p for p in r_root.iterdir() if p.is_dir()], reverse=True)
            for version in versions:
                exe = version / "bin" / "x64" / "Rscript.exe"
                if exe.exists():
                    self._rscript_path = str(exe)
                    return self._rscript_path
        return None

    def _build_r_env(self, rscript_path: str):
        env = os.environ.copy()
        if not env.get("R_LIBS_USER"):
            user_profile = Path.home()
            version_match = None
            try:
                version_match = Path(rscript_path).resolve().parts
            except Exception:
                version_match = ()
            major_minor = "4.5"
            for part in version_match:
                if part.startswith("R-"):
                    version = part.replace("R-", "")
                    tokens = version.split(".")
                    if len(tokens) >= 2:
                        major_minor = f"{tokens[0]}.{tokens[1]}"
                    break
            env["R_LIBS_USER"] = str(user_profile / "Documents" / "R" / "win-library" / major_minor)
        return env

    def ensure_ready(self):
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            cur.execute(
                """
                insert into collector_sessions (
                    session_id, started_at, host_name, platform_name, platform_release, workload_label
                )
                values (%s, %s, %s, %s, %s, %s)
                on conflict (session_id) do nothing
                """,
                (
                    self.session_id,
                    self.utc_now(),
                    self.host_name,
                    platform.system(),
                    platform.release(),
                    self.workload_label,
                ),
            )

    def log_sample(self, live: dict, gpu_total, gpu_top: list, process_rows: list):
        conn = self.connect()
        captured_at = live.get("time") or self.utc_now()
        battery = live.get("battery")
        plugged = live.get("plugged")
        top_gpu_process = gpu_top[0][1] if gpu_top else None

        with conn.cursor() as cur:
            cur.execute(
                """
                insert into system_metrics (
                    session_id, captured_at, cpu_usage, ram_usage, gpu_usage,
                    battery_percent, plugged_in, top_gpu_process
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    self.session_id,
                    captured_at,
                    float(live.get("cpu", 0.0)),
                    float(live.get("ram", 0.0)),
                    None if gpu_total is None else float(gpu_total),
                    None if battery is None else float(battery),
                    None if plugged is None else bool(plugged),
                    top_gpu_process,
                ),
            )

            for pid, name, util in gpu_top[:5]:
                cur.execute(
                    """
                    insert into gpu_process_metrics (
                        session_id, captured_at, pid, process_name, gpu_usage
                    ) values (%s, %s, %s, %s, %s)
                    """,
                    (self.session_id, captured_at, int(pid), name, float(util)),
                )

            for pid, name, mem_mb in process_rows[:12]:
                cur.execute(
                    """
                    insert into process_memory_metrics (
                        session_id, captured_at, pid, process_name, memory_mb
                    ) values (%s, %s, %s, %s, %s)
                    """,
                    (self.session_id, captured_at, int(pid), name, float(mem_mb)),
                )

        self.sample_count += 1

    def fetch_recent_baseline(self, hours: int = 24) -> HistoricalSnapshot:
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute(
                """
                select
                    coalesce(avg(cpu_usage), 0),
                    coalesce(avg(ram_usage), 0),
                    coalesce(avg(gpu_usage), 0),
                    count(*),
                    count(distinct session_id)
                from system_metrics
                where captured_at >= now() - (%s::int * interval '1 hour')
                  and session_id <> %s
                """,
                (hours, self.session_id),
            )
            row = cur.fetchone() or (0, 0, 0, 0, 0)
        return HistoricalSnapshot(
            avg_cpu=float(row[0] or 0.0),
            avg_ram=float(row[1] or 0.0),
            avg_gpu=float(row[2] or 0.0),
            samples=int(row[3] or 0),
            sessions=int(row[4] or 0),
        )

    def run_r_analytics_async(self):
        rscript = self._find_rscript()
        if not rscript:
            return False

        script_path = Path(__file__).resolve().parent / "r_engine" / "session_analytics.R"
        if not script_path.exists():
            return False

        try:
            env = self._build_r_env(rscript)
            subprocess.Popen(
                [rscript, str(script_path), self.dsn, self.session_id],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                shell=False,
                env=env,
            )
            return True
        except Exception:
            return False

    def fetch_r_analytics(self) -> RAnalyticsSnapshot | None:
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute(
                """
                select
                    pulse_score,
                    pulse_grade,
                    efficiency_score,
                    stability_score,
                    battery_score,
                    avg_drain_per_hour,
                    ram_minutes_above_80,
                    forecast_battery_hours,
                    forecast_ram_peak,
                    coalesce(anomaly_flag, false),
                    coalesce(anomaly_text, ''),
                    coalesce(explain_text, ''),
                    coalesce(drivers_text, '')
                from analytics_session_scores
                where session_id = %s
                """,
                (self.session_id,),
            )
            row = cur.fetchone()

        if not row:
            return None

        return RAnalyticsSnapshot(
            pulse_score=float(row[0] or 0.0),
            pulse_grade=str(row[1] or "N/A"),
            efficiency_score=float(row[2] or 0.0),
            stability_score=float(row[3] or 0.0),
            battery_score=float(row[4] or 0.0),
            avg_drain_per_hour=float(row[5] or 0.0),
            ram_minutes_above_80=float(row[6] or 0.0),
            forecast_battery_hours=None if row[7] is None else float(row[7]),
            forecast_ram_peak=None if row[8] is None else float(row[8]),
            anomaly_flag=bool(row[9]),
            anomaly_text=str(row[10] or ""),
            explain_text=str(row[11] or ""),
            drivers_text=str(row[12] or ""),
        )

    def fetch_workload_fingerprint(self) -> WorkloadFingerprintSnapshot | None:
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute(
                """
                select
                    workload_label,
                    classification_confidence,
                    anomaly_score,
                    baseline_similarity,
                    personal_baseline_label,
                    avg_cpu_vs_baseline,
                    avg_ram_vs_baseline,
                    avg_gpu_vs_baseline,
                    battery_drain_vs_baseline,
                    coalesce(summary_text, '')
                from analytics_session_fingerprint
                where session_id = %s
                """,
                (self.session_id,),
            )
            row = cur.fetchone()

        if not row:
            return None

        return WorkloadFingerprintSnapshot(
            workload_label=str(row[0] or "unknown"),
            classification_confidence=float(row[1] or 0.0),
            anomaly_score=float(row[2] or 0.0),
            baseline_similarity=float(row[3] or 0.0),
            personal_baseline_label=str(row[4] or "unknown"),
            avg_cpu_vs_baseline=float(row[5] or 0.0),
            avg_ram_vs_baseline=float(row[6] or 0.0),
            avg_gpu_vs_baseline=float(row[7] or 0.0),
            battery_drain_vs_baseline=float(row[8] or 0.0),
            summary_text=str(row[9] or ""),
        )

    def fetch_replay_events(self, limit: int = 10) -> list[ReplayEvent]:
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute(
                """
                select
                    event_time,
                    event_type,
                    event_description,
                    coalesce(metric_name, ''),
                    metric_before,
                    metric_after,
                    coalesce(impact_score, 0)
                from analytics_session_replay_events
                where session_id = %s
                order by event_time desc
                limit %s
                """,
                (self.session_id, int(limit)),
            )
            rows = cur.fetchall() or []

        return [
            ReplayEvent(
                event_time=row[0],
                event_type=str(row[1] or ""),
                event_description=str(row[2] or ""),
                metric_name=str(row[3] or ""),
                metric_before=None if row[4] is None else float(row[4]),
                metric_after=None if row[5] is None else float(row[5]),
                impact_score=float(row[6] or 0.0),
            )
            for row in rows
        ]

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None

import argparse
import json
import os
import platform
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import psutil

try:
    import psycopg
except ImportError:
    psycopg = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def get_live() -> dict[str, Any]:
    battery = psutil.sensors_battery()

    cpu = None
    try:
        script = r"""
        try {
          $c = Get-Counter '\Processor(_Total)\% Processor Time' -MaxSamples 1
          $c.CounterSamples | Select-Object CookedValue | ConvertTo-Json -Compress
        } catch {
          ""
        }
        """
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )
        output = (result.stdout or "").strip()
        if output:
            payload = json.loads(output)
            cpu = (
                float(payload["CookedValue"])
                if isinstance(payload, dict)
                else float(payload[0]["CookedValue"])
            )
    except Exception:
        cpu = None

    if cpu is None:
        cpu = psutil.cpu_percent(interval=None)

    sample = {
        "captured_at": utc_now(),
        "cpu_usage": float(cpu),
        "ram_usage": float(psutil.virtual_memory().percent),
        "battery_percent": None,
        "plugged_in": None,
    }

    if battery:
        sample["battery_percent"] = float(battery.percent)
        sample["plugged_in"] = bool(battery.power_plugged)

    return sample


def get_gpu_activity() -> tuple[float | None, list[dict[str, Any]]]:
    script = r"""
    try {
      $c = Get-Counter '\GPU Engine(*)\Utilization Percentage' -ErrorAction Stop
      $c.CounterSamples | Select-Object InstanceName, CookedValue | ConvertTo-Json -Compress
    } catch {
      ""
    }
    """
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )
    except Exception:
        return None, []

    output = (result.stdout or "").strip()
    if not output:
        return None, []

    try:
        payload = json.loads(output)
    except Exception:
        return None, []

    rows = payload if isinstance(payload, list) else [payload]

    total = 0.0
    per_pid: dict[int, float] = {}
    for row in rows:
        try:
            instance = row.get("InstanceName", "")
            usage = float(row.get("CookedValue", 0.0))
        except Exception:
            continue

        total += usage
        parts = instance.split("_")
        pid = None
        if len(parts) >= 2 and parts[0] == "pid":
            try:
                pid = int(parts[1])
            except Exception:
                pid = None

        if pid is not None:
            per_pid[pid] = per_pid.get(pid, 0.0) + usage

    leaders = []
    for pid, usage in sorted(per_pid.items(), key=lambda item: item[1], reverse=True)[:5]:
        try:
            name = psutil.Process(pid).name()
        except Exception:
            name = f"PID {pid}"
        leaders.append({"pid": pid, "process_name": name, "gpu_usage": float(usage)})

    return total, leaders


def get_process_snapshot(limit: int = 20) -> list[dict[str, Any]]:
    rows = []
    for proc in psutil.process_iter(attrs=["pid", "name"]):
        try:
            with proc.oneshot():
                rows.append(
                    {
                        "pid": int(proc.pid),
                        "process_name": proc.info.get("name") or "unknown",
                        "memory_mb": float(proc.memory_info().rss / (1024 * 1024)),
                    }
                )
        except Exception:
            continue

    rows.sort(key=lambda row: row["memory_mb"], reverse=True)
    return rows[:limit]


def collect_sample(process_limit: int) -> dict[str, Any]:
    live = get_live()
    gpu_usage, gpu_processes = get_gpu_activity()
    processes = get_process_snapshot(limit=process_limit)

    return {
        "captured_at": live["captured_at"],
        "system": {
            **live,
            "gpu_usage": float(gpu_usage) if gpu_usage is not None else None,
            "top_gpu_process": gpu_processes[0]["process_name"] if gpu_processes else None,
        },
        "gpu_processes": gpu_processes,
        "processes": processes,
    }


@dataclass
class CollectorConfig:
    interval: int
    once: bool
    process_limit: int
    workload_label: str | None
    dsn: str | None


def open_connection(dsn: str):
    if psycopg is None:
        raise RuntimeError("psycopg is not installed. Add psycopg[binary] to the environment.")
    return psycopg.connect(dsn)


def register_session(conn, session_id: str, workload_label: str | None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into collector_sessions (
                session_id,
                started_at,
                host_name,
                platform_name,
                platform_release,
                workload_label
            )
            values (%s, %s, %s, %s, %s, %s)
            on conflict (session_id) do nothing
            """,
            (
                session_id,
                utc_now(),
                socket.gethostname(),
                platform.system(),
                platform.release(),
                workload_label,
            ),
        )
    conn.commit()


def write_sample(conn, session_id: str, sample: dict[str, Any]) -> None:
    system = sample["system"]
    captured_at = sample["captured_at"]

    with conn.cursor() as cur:
        cur.execute(
            """
            insert into system_metrics (
                session_id,
                captured_at,
                cpu_usage,
                ram_usage,
                gpu_usage,
                battery_percent,
                plugged_in,
                top_gpu_process
            )
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                session_id,
                captured_at,
                system["cpu_usage"],
                system["ram_usage"],
                system["gpu_usage"],
                system["battery_percent"],
                system["plugged_in"],
                system["top_gpu_process"],
            ),
        )

        for row in sample["gpu_processes"]:
            cur.execute(
                """
                insert into gpu_process_metrics (
                    session_id,
                    captured_at,
                    pid,
                    process_name,
                    gpu_usage
                )
                values (%s, %s, %s, %s, %s)
                """,
                (
                    session_id,
                    captured_at,
                    row["pid"],
                    row["process_name"],
                    row["gpu_usage"],
                ),
            )

        for row in sample["processes"]:
            cur.execute(
                """
                insert into process_memory_metrics (
                    session_id,
                    captured_at,
                    pid,
                    process_name,
                    memory_mb
                )
                values (%s, %s, %s, %s, %s)
                """,
                (
                    session_id,
                    captured_at,
                    row["pid"],
                    row["process_name"],
                    row["memory_mb"],
                ),
            )

    conn.commit()


def print_sample(sample: dict[str, Any]) -> None:
    serializable = {
        "captured_at": sample["captured_at"].isoformat(),
        "system": {
            **sample["system"],
            "captured_at": sample["system"]["captured_at"].isoformat(),
        },
        "gpu_processes": sample["gpu_processes"],
        "processes": sample["processes"][:5],
    }
    print(json.dumps(serializable, indent=2))


def run_collector(config: CollectorConfig) -> None:
    try:
        psutil.cpu_percent(interval=None)
    except Exception:
        pass

    session_id = str(uuid.uuid4())
    connection = None

    if config.dsn:
        connection = open_connection(config.dsn)
        register_session(connection, session_id, config.workload_label)
        print(f"Connected to PostgreSQL. Session {session_id} started.")
    else:
        print("PULSEBOARD_DB_DSN not set. Running in console-only mode.")

    while True:
        sample = collect_sample(config.process_limit)

        if connection is not None:
            write_sample(connection, session_id, sample)
            system = sample["system"]
            print(
                f"{sample['captured_at'].isoformat()} | "
                f"CPU {system['cpu_usage']:.1f}% | "
                f"RAM {system['ram_usage']:.1f}% | "
                f"GPU {0.0 if system['gpu_usage'] is None else system['gpu_usage']:.1f}%"
            )
        else:
            print_sample(sample)

        if config.once:
            break

        time.sleep(config.interval)

    if connection is not None:
        connection.close()


def parse_args() -> CollectorConfig:
    parser = argparse.ArgumentParser(description="Collect PulseBoard telemetry into PostgreSQL.")
    parser.add_argument("--interval", type=int, default=int(os.getenv("PULSEBOARD_REFRESH_SECONDS", "2")))
    parser.add_argument("--once", action="store_true", help="Collect a single sample and exit.")
    parser.add_argument("--process-limit", type=int, default=15)
    parser.add_argument("--workload-label", type=str, default=None)
    args = parser.parse_args()

    return CollectorConfig(
        interval=max(1, args.interval),
        once=bool(args.once),
        process_limit=max(1, args.process_limit),
        workload_label=args.workload_label,
        dsn=os.getenv("PULSEBOARD_DB_DSN"),
    )


if __name__ == "__main__":
    run_collector(parse_args())

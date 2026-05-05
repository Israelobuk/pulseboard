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

create index if not exists idx_system_metrics_captured_at
    on system_metrics (captured_at desc);

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

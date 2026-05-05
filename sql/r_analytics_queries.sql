-- Session ranking
with session_points as (
  select
    s.session_id,
    coalesce(s.workload_label, 'unlabeled') as workload_label,
    m.captured_at,
    m.cpu_usage,
    m.ram_usage,
    coalesce(m.gpu_usage, 0) as gpu_usage,
    m.battery_percent,
    m.plugged_in,
    lag(m.battery_percent) over (partition by s.session_id order by m.captured_at) as prev_battery,
    lag(m.captured_at) over (partition by s.session_id order by m.captured_at) as prev_time
  from collector_sessions s
  join system_metrics m on m.session_id = s.session_id
  where s.started_at >= now() - interval '30 day'
),
drains as (
  select *,
    case
      when plugged_in is false
       and prev_battery is not null
       and prev_time is not null
       and extract(epoch from captured_at - prev_time) > 0
      then greatest(prev_battery - battery_percent, 0)
           / greatest(extract(epoch from captured_at - prev_time) / 3600.0, 1.0 / 60.0)
      else null
    end as drain_per_hour
  from session_points
)
select
  session_id,
  workload_label,
  avg(cpu_usage) as avg_cpu,
  avg(ram_usage) as avg_ram,
  percentile_cont(0.95) within group (order by ram_usage) as p95_ram,
  avg(gpu_usage) as avg_gpu,
  avg(drain_per_hour) as avg_drain_per_hour,
  stddev_samp(cpu_usage) + stddev_samp(ram_usage) as instability
from drains
group by session_id, workload_label
order by avg_cpu + p95_ram + coalesce(avg_drain_per_hour, 0) desc;

-- Anomaly detection against workload-specific personal baseline
with rollups as (
  select
    s.session_id,
    coalesce(s.workload_label, 'unlabeled') as workload_label,
    avg(m.cpu_usage) as avg_cpu,
    avg(m.ram_usage) as avg_ram,
    percentile_cont(0.95) within group (order by m.ram_usage) as p95_ram,
    avg(coalesce(m.gpu_usage, 0)) as avg_gpu
  from collector_sessions s
  join system_metrics m on m.session_id = s.session_id
  where s.started_at >= now() - interval '30 day'
  group by s.session_id, coalesce(s.workload_label, 'unlabeled')
),
baseline as (
  select
    workload_label,
    avg(avg_cpu) as baseline_cpu,
    avg(avg_ram) as baseline_ram,
    avg(p95_ram) as baseline_p95_ram,
    avg(avg_gpu) as baseline_gpu
  from rollups
  group by workload_label
)
select
  r.session_id,
  r.workload_label,
  r.avg_cpu - b.baseline_cpu as cpu_delta_vs_norm,
  r.avg_ram - b.baseline_ram as ram_delta_vs_norm,
  r.p95_ram - b.baseline_p95_ram as ram_peak_delta_vs_norm,
  r.avg_gpu - b.baseline_gpu as gpu_delta_vs_norm
from rollups r
join baseline b using (workload_label)
where r.avg_ram > b.baseline_ram + 10
   or r.p95_ram > b.baseline_p95_ram + 12
   or r.avg_cpu > b.baseline_cpu + 12;

-- Battery drain trends
with battery_points as (
  select
    s.session_id,
    coalesce(s.workload_label, 'unlabeled') as workload_label,
    m.captured_at,
    m.battery_percent,
    m.plugged_in,
    coalesce(m.gpu_usage, 0) as gpu_usage,
    lag(m.battery_percent) over (partition by s.session_id order by m.captured_at) as prev_battery,
    lag(m.captured_at) over (partition by s.session_id order by m.captured_at) as prev_time
  from collector_sessions s
  join system_metrics m on m.session_id = s.session_id
  where s.started_at >= now() - interval '30 day'
)
select
  workload_label,
  avg(
    case
      when plugged_in is false
       and prev_battery is not null
       and prev_time is not null
       and extract(epoch from captured_at - prev_time) > 0
      then greatest(prev_battery - battery_percent, 0)
           / greatest(extract(epoch from captured_at - prev_time) / 3600.0, 1.0 / 60.0)
      else null
    end
  ) as avg_drain_per_hour,
  avg(case when gpu_usage >= 10 then 1 else 0 end) as dgpu_proxy_ratio
from battery_points
group by workload_label
order by avg_drain_per_hour desc nulls last;

-- Process impact analysis
with stress_windows as (
  select
    session_id,
    captured_at,
    case when ram_usage >= 85 then 1 else 0 end as ram_spike,
    case when coalesce(gpu_usage, 0) >= 40 then 1 else 0 end as gpu_spike,
    case when cpu_usage >= 75 or ram_usage >= 80 or coalesce(gpu_usage, 0) >= 35 then 1 else 0 end as stress_window
  from system_metrics
),
proc_events as (
  select session_id, captured_at, lower(process_name) as process_name, max(memory_mb) as memory_mb, 0::numeric as proc_gpu_usage
  from process_memory_metrics
  group by session_id, captured_at, lower(process_name)
  union all
  select session_id, captured_at, lower(process_name) as process_name, 0::numeric as memory_mb, max(gpu_usage) as proc_gpu_usage
  from gpu_process_metrics
  group by session_id, captured_at, lower(process_name)
),
proc_agg as (
  select
    session_id,
    captured_at,
    process_name,
    max(memory_mb) as memory_mb,
    max(proc_gpu_usage) as proc_gpu_usage
  from proc_events
  group by session_id, captured_at, process_name
)
select
  p.process_name,
  count(*) as observations,
  avg(p.memory_mb) as avg_memory_mb,
  avg(p.proc_gpu_usage) as avg_proc_gpu,
  avg(w.ram_spike) as ram_spike_rate,
  avg(w.gpu_spike) as gpu_spike_rate,
  avg(w.stress_window) as stress_rate,
  (
    coalesce(avg(p.memory_mb), 0) / 160.0 +
    coalesce(avg(p.proc_gpu_usage), 0) * 0.9 +
    avg(w.stress_window) * 28 +
    avg(w.ram_spike) * 18 +
    avg(w.gpu_spike) * 18
  ) as impact_score
from proc_agg p
join stress_windows w
  on w.session_id = p.session_id
 and w.captured_at = p.captured_at
where p.process_name is not null
group by p.process_name
having count(*) >= 4
order by impact_score desc, observations desc;

-- Latest fingerprint for the active session
select
  session_id,
  computed_at,
  workload_label,
  classification_confidence,
  anomaly_score,
  baseline_similarity,
  personal_baseline_label,
  avg_cpu_vs_baseline,
  avg_ram_vs_baseline,
  avg_gpu_vs_baseline,
  battery_drain_vs_baseline,
  summary_text
from analytics_session_fingerprint
where session_id = $1::uuid;

-- Replay timeline for the active session
select
  event_time,
  event_type,
  event_description,
  metric_name,
  metric_before,
  metric_after,
  impact_score
from analytics_session_replay_events
where session_id = $1::uuid
order by event_time desc
limit 20;

-- Baseline lookup by nearest workload label
select
  workload_label,
  avg_cpu,
  avg_ram,
  avg_gpu,
  avg_battery_drain,
  avg_pulse_score,
  avg_session_duration,
  common_processes,
  updated_at
from analytics_workload_baselines
where baseline_version = 1
order by updated_at desc;

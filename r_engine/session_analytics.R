suppressPackageStartupMessages({
  library(DBI)
  library(RPostgres)
  library(dplyr)
  library(data.table)
  library(lubridate)
})

args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 2) {
  stop("Usage: Rscript session_analytics.R <postgres_dsn> <session_id>")
}

dsn <- args[[1]]
session_id <- args[[2]]
script_path_arg <- sub("^--file=", "", commandArgs(trailingOnly = FALSE)[grep("^--file=", commandArgs(trailingOnly = FALSE))][1])
script_dir <- if (!is.na(script_path_arg) && nzchar(script_path_arg)) dirname(normalizePath(script_path_arg)) else getwd()
source(file.path(script_dir, "workload_helpers.R"))

parse_pg_dsn <- function(dsn) {
  pattern <- "^postgres(?:ql)?://([^:]+)(?::([^@]*))?@([^:/]+)(?::([0-9]+))?/([^?]+).*$"
  parts <- regmatches(dsn, regexec(pattern, dsn))[[1]]
  if (length(parts) == 0) {
    stop("Could not parse PostgreSQL DSN")
  }
  list(
    user = parts[2],
    password = if (nzchar(parts[3])) parts[3] else "",
    host = parts[4],
    port = if (nzchar(parts[5])) as.integer(parts[5]) else 5432L,
    dbname = parts[6]
  )
}

cfg <- parse_pg_dsn(dsn)
con <- dbConnect(
  RPostgres::Postgres(),
  host = cfg$host,
  port = cfg$port,
  dbname = cfg$dbname,
  user = cfg$user,
  password = cfg$password
)
on.exit(dbDisconnect(con), add = TRUE)

session_meta <- dbGetQuery(
  con,
  "select coalesce(workload_label, 'unlabeled') as workload_label from collector_sessions where session_id = $1::uuid",
  params = list(session_id)
)
if (nrow(session_meta) == 0) {
  quit(save = "no", status = 0)
}
workload_label <- session_meta$workload_label[[1]]

metrics <- dbGetQuery(
  con,
  "
  select
    captured_at,
    cpu_usage,
    ram_usage,
    coalesce(gpu_usage, 0) as gpu_usage,
    battery_percent,
    plugged_in
  from system_metrics
  where session_id = $1::uuid
  order by captured_at
  ",
  params = list(session_id)
)

if (nrow(metrics) < 3) {
  quit(save = "no", status = 0)
}

metrics$captured_at <- as.POSIXct(metrics$captured_at, tz = "UTC")

process_rows <- dbGetQuery(
  con,
  "
  select captured_at, lower(process_name) as process_name, memory_mb
  from process_memory_metrics
  where session_id = $1::uuid
  order by captured_at
  ",
  params = list(session_id)
)
if (nrow(process_rows) > 0) {
  process_rows$captured_at <- as.POSIXct(process_rows$captured_at, tz = "UTC")
}

drain_per_hour <- c()
for (i in 2:nrow(metrics)) {
  if (!isTRUE(metrics$plugged_in[[i]]) &&
      !is.na(metrics$battery_percent[[i]]) &&
      !is.na(metrics$battery_percent[[i - 1]])) {
    delta_battery <- max(metrics$battery_percent[[i - 1]] - metrics$battery_percent[[i]], 0)
    delta_hours <- as.numeric(difftime(metrics$captured_at[[i]], metrics$captured_at[[i - 1]], units = "hours"))
    if (!is.na(delta_hours) && delta_hours > 0) {
      drain_per_hour <- c(drain_per_hour, delta_battery / max(delta_hours, 1 / 60))
    }
  }
}

avg_drain <- if (length(drain_per_hour) > 0) mean(drain_per_hour, na.rm = TRUE) else 0
avg_cpu <- mean(metrics$cpu_usage, na.rm = TRUE)
avg_ram <- mean(metrics$ram_usage, na.rm = TRUE)
avg_gpu <- mean(metrics$gpu_usage, na.rm = TRUE)
p95_ram <- as.numeric(stats::quantile(metrics$ram_usage, probs = 0.95, na.rm = TRUE, names = FALSE))
cpu_std <- if (nrow(metrics) > 1) stats::sd(metrics$cpu_usage, na.rm = TRUE) else 0
ram_std <- if (nrow(metrics) > 1) stats::sd(metrics$ram_usage, na.rm = TRUE) else 0

delta_mins <- c(0, diff(as.numeric(metrics$captured_at)) / 60)
ram_minutes_above_80 <- sum(delta_mins[metrics$ram_usage >= 80], na.rm = TRUE)

battery_forecast_hours <- NA_real_
battery_rows <- metrics[!is.na(metrics$battery_percent) & metrics$plugged_in == FALSE, ]
if (nrow(battery_rows) >= 4) {
  mins <- as.numeric(difftime(battery_rows$captured_at, min(battery_rows$captured_at), units = "mins"))
  fit <- stats::lm(battery_percent ~ mins, data = data.frame(battery_percent = battery_rows$battery_percent, mins = mins))
  slope_per_min <- unname(stats::coef(fit)[["mins"]])
  if (!is.na(slope_per_min) && slope_per_min < -0.01) {
    battery_forecast_hours <- tail(battery_rows$battery_percent, 1) / abs(slope_per_min) / 60
  }
}

forecast_ram_peak <- avg_ram
recent_rows <- tail(metrics, min(10, nrow(metrics)))
if (nrow(recent_rows) >= 4) {
  mins <- seq_len(nrow(recent_rows))
  fit_ram <- stats::lm(ram_usage ~ mins, data = data.frame(ram_usage = recent_rows$ram_usage, mins = mins))
  next_points <- predict(fit_ram, newdata = data.frame(mins = seq(max(mins), max(mins) + 6)))
  forecast_ram_peak <- max(c(recent_rows$ram_usage, next_points), na.rm = TRUE)
}

baseline <- dbGetQuery(
  con,
  "
  with rollups as (
    select
      s.session_id,
      avg(m.cpu_usage) as avg_cpu,
      avg(m.ram_usage) as avg_ram,
      avg(coalesce(m.gpu_usage, 0)) as avg_gpu,
      percentile_cont(0.95) within group (order by m.ram_usage) as p95_ram
    from collector_sessions s
    join system_metrics m on m.session_id = s.session_id
    where coalesce(s.workload_label, 'unlabeled') = $1::text
      and s.session_id <> $2::uuid
      and s.started_at >= now() - interval '30 day'
    group by s.session_id
  ),
  battery_rollups as (
    select
      session_id,
      avg(drain_per_hour) as avg_drain_per_hour
    from (
      select
        s.session_id,
        case
          when m.plugged_in is false
           and lag(m.battery_percent) over (partition by s.session_id order by m.captured_at) is not null
           and lag(m.captured_at) over (partition by s.session_id order by m.captured_at) is not null
           and extract(epoch from m.captured_at - lag(m.captured_at) over (partition by s.session_id order by m.captured_at)) > 0
          then greatest(lag(m.battery_percent) over (partition by s.session_id order by m.captured_at) - m.battery_percent, 0)
               / greatest(extract(epoch from m.captured_at - lag(m.captured_at) over (partition by s.session_id order by m.captured_at)) / 3600.0, 1.0 / 60.0)
          else null
        end as drain_per_hour
      from collector_sessions s
      join system_metrics m on m.session_id = s.session_id
      where coalesce(s.workload_label, 'unlabeled') = $1::text
        and s.session_id <> $2::uuid
        and s.started_at >= now() - interval '30 day'
    ) x
    group by session_id
  )
  select
    coalesce(avg(r.avg_cpu), 0) as baseline_cpu,
    coalesce(avg(r.avg_ram), 0) as baseline_ram,
    coalesce(avg(r.avg_gpu), 0) as baseline_gpu,
    coalesce(avg(r.p95_ram), 0) as baseline_p95_ram,
    coalesce(avg(b.avg_drain_per_hour), 0) as baseline_drain_per_hour
  from rollups r
  left join battery_rollups b using (session_id)
  ",
  params = list(workload_label, session_id)
)

baseline_cpu <- baseline$baseline_cpu[[1]]
baseline_ram <- baseline$baseline_ram[[1]]
baseline_gpu <- baseline$baseline_gpu[[1]]
baseline_p95_ram <- baseline$baseline_p95_ram[[1]]
baseline_drain <- baseline$baseline_drain_per_hour[[1]]

ram_pressure_penalty <- max(p95_ram - 72, 0) * 0.95
cpu_load_penalty <- max(avg_cpu - max(baseline_cpu, 28), 0) * 0.45
battery_penalty <- if (mean(metrics$plugged_in == FALSE, na.rm = TRUE) > 0.15) max(avg_drain - max(baseline_drain, 3.8), 0) * 1.8 else 0
dgpu_penalty <- avg_gpu * mean(metrics$plugged_in == FALSE, na.rm = TRUE) * 0.42 + mean(metrics$gpu_usage >= 10, na.rm = TRUE) * 6.0
instability_penalty <- max((cpu_std + ram_std) - 18, 0) * 0.9

efficiency_score <- max(0, min(100, 100 - ram_pressure_penalty - cpu_load_penalty - battery_penalty - dgpu_penalty))
stability_score <- max(0, min(100, 100 - instability_penalty - max(max(metrics$ram_usage, na.rm = TRUE) - 88, 0) * 0.7))
battery_score <- if (mean(metrics$plugged_in == FALSE, na.rm = TRUE) > 0.15) max(0, min(100, 100 - avg_drain * 4.6 - avg_gpu * 0.22)) else max(0, min(100, 92 - avg_gpu * 0.08))
pulse_score <- round((efficiency_score * 0.45) + (stability_score * 0.30) + (battery_score * 0.25), 1)
pulse_grade <- if (pulse_score >= 85) "A" else if (pulse_score >= 72) "B" else if (pulse_score >= 58) "C" else "D"

drivers <- c()
if (ram_minutes_above_80 >= 8) drivers <- c(drivers, sprintf("RAM stayed above 80%% for %.1f minutes.", ram_minutes_above_80))
if (avg_drain > baseline_drain + 2.5) drivers <- c(drivers, sprintf("Battery drain is %.1f%%/hr versus your usual %.1f%%/hr.", avg_drain, baseline_drain))
if (avg_ram > baseline_ram + 8) drivers <- c(drivers, sprintf("Average RAM is %.0f%%, around %.0f points above your %s norm.", avg_ram, avg_ram - baseline_ram, workload_label))
if (avg_cpu > baseline_cpu + 10) drivers <- c(drivers, sprintf("CPU load is %.0f points above your %s baseline.", avg_cpu - baseline_cpu, workload_label))
if (avg_gpu > baseline_gpu + 8 && mean(metrics$plugged_in == FALSE, na.rm = TRUE) > 0.20) drivers <- c(drivers, "dGPU-style GPU load is active while on battery.")
if (length(drivers) == 0) drivers <- c("Session is operating close to your normal workload profile.")

anomaly_flag <- FALSE
anomaly_text <- "Session is within your recent norm."
if (avg_drain > baseline_drain + 3.5 && avg_drain > 0) {
  anomaly_flag <- TRUE
  anomaly_text <- sprintf("Battery drain is %.0f%% higher than your usual %s sessions.", 100 * (avg_drain - baseline_drain) / max(baseline_drain, 1), workload_label)
} else if (avg_ram > baseline_ram + 10) {
  anomaly_flag <- TRUE
  anomaly_text <- sprintf("This session is using %.0f%% more RAM than your usual %s sessions.", avg_ram - baseline_ram, workload_label)
} else if (p95_ram > baseline_p95_ram + 10) {
  anomaly_flag <- TRUE
  anomaly_text <- sprintf("RAM peaks are materially above your %s baseline.", workload_label)
}

explain_text <- sprintf(
  "Analytics Engine scored this session at %.0f/100. Efficiency %.0f, stability %.0f, battery %.0f. Forecast RAM peak %.0f%%.",
  pulse_score, efficiency_score, stability_score, battery_score, forecast_ram_peak
)
drivers_text <- paste(drivers, collapse = " ")

dbExecute(
  con,
  "
  insert into analytics_session_scores (
    session_id, computed_at, efficiency_score, stability_score, battery_score, pulse_score,
    pulse_grade, avg_drain_per_hour, ram_minutes_above_80, forecast_battery_hours,
    forecast_ram_peak, anomaly_flag, anomaly_text, explain_text, drivers_text
  )
  values (
    $1::uuid, now(), $2, $3, $4, $5,
    $6, $7, $8, $9,
    $10, $11, $12, $13, $14
  )
  on conflict (session_id) do update set
    computed_at = excluded.computed_at,
    efficiency_score = excluded.efficiency_score,
    stability_score = excluded.stability_score,
    battery_score = excluded.battery_score,
    pulse_score = excluded.pulse_score,
    pulse_grade = excluded.pulse_grade,
    avg_drain_per_hour = excluded.avg_drain_per_hour,
    ram_minutes_above_80 = excluded.ram_minutes_above_80,
    forecast_battery_hours = excluded.forecast_battery_hours,
    forecast_ram_peak = excluded.forecast_ram_peak,
    anomaly_flag = excluded.anomaly_flag,
    anomaly_text = excluded.anomaly_text,
    explain_text = excluded.explain_text,
    drivers_text = excluded.drivers_text
  ",
  params = list(
    session_id, efficiency_score, stability_score, battery_score, pulse_score,
    pulse_grade, avg_drain, ram_minutes_above_80, battery_forecast_hours,
    forecast_ram_peak, anomaly_flag, anomaly_text, explain_text, drivers_text
  )
)

historical_rollups <- dbGetQuery(
  con,
  "
  with metric_rollups as (
    select
      m.session_id,
      avg(m.cpu_usage) as avg_cpu,
      avg(m.ram_usage) as avg_ram,
      avg(coalesce(m.gpu_usage, 0)) as avg_gpu,
      percentile_cont(0.95) within group (order by m.ram_usage) as p95_ram,
      extract(epoch from max(m.captured_at) - min(m.captured_at)) / 60.0 as duration_minutes,
      avg(case when m.plugged_in is false then 1 else 0 end) as unplugged_ratio
    from system_metrics m
    join collector_sessions s on s.session_id = m.session_id
    where s.started_at >= now() - interval '60 day'
    group by m.session_id
  ),
  battery_rollups as (
    select
      session_id,
      avg(drain_per_hour) as avg_drain_per_hour
    from (
      select
        session_id,
        case
          when plugged_in is false
           and lag(battery_percent) over (partition by session_id order by captured_at) is not null
           and lag(captured_at) over (partition by session_id order by captured_at) is not null
           and extract(epoch from captured_at - lag(captured_at) over (partition by session_id order by captured_at)) > 0
          then greatest(lag(battery_percent) over (partition by session_id order by captured_at) - battery_percent, 0)
               / greatest(extract(epoch from captured_at - lag(captured_at) over (partition by session_id order by captured_at)) / 3600.0, 1.0 / 60.0)
          else null
        end as drain_per_hour
      from system_metrics
    ) x
    group by session_id
  ),
  proc_counts as (
    select
      session_id,
      lower(process_name) as process_name,
      count(*) as observations,
      max(memory_mb) as max_memory_mb
    from process_memory_metrics
    group by session_id, lower(process_name)
  ),
  proc_ranked as (
    select *,
      row_number() over (partition by session_id order by observations desc, max_memory_mb desc, process_name asc) as rn
    from proc_counts
  ),
  proc_rollups as (
    select
      session_id,
      string_agg(process_name, ', ' order by observations desc, max_memory_mb desc, process_name asc) as common_processes
    from proc_ranked
    where rn <= 8
    group by session_id
  )
  select
    s.session_id,
    coalesce(s.workload_label, 'unknown') as declared_label,
    mr.avg_cpu,
    mr.avg_ram,
    mr.avg_gpu,
    mr.p95_ram,
    coalesce(br.avg_drain_per_hour, 0) as avg_battery_drain,
    coalesce(mr.duration_minutes, 0) as duration_minutes,
    coalesce(mr.unplugged_ratio, 0) as unplugged_ratio,
    coalesce(proc_rollups.common_processes, '') as common_processes,
    ascs.pulse_score
  from collector_sessions s
  join metric_rollups mr on mr.session_id = s.session_id
  left join battery_rollups br on br.session_id = s.session_id
  left join proc_rollups on proc_rollups.session_id = s.session_id
  left join analytics_session_scores ascs on ascs.session_id = s.session_id
  where s.started_at >= now() - interval '60 day'
  "
)

historical_agg <- data.frame()
if (nrow(historical_rollups) > 0) {
  rollup_rows <- lapply(seq_len(nrow(historical_rollups)), function(i) {
    row <- historical_rollups[i, ]
    synthetic_metrics <- data.frame(
      captured_at = as.POSIXct(c("2000-01-01 00:00:00", "2000-01-01 00:05:00"), tz = "UTC"),
      cpu_usage = c(row$avg_cpu, row$avg_cpu),
      ram_usage = c(row$avg_ram, row$avg_ram),
      gpu_usage = c(row$avg_gpu, row$avg_gpu),
      battery_percent = c(NA_real_, NA_real_),
      plugged_in = c(row$unplugged_ratio < 0.5, row$unplugged_ratio < 0.5)
    )
    rollup_session(
      session_id = as.character(row$session_id),
      metrics = synthetic_metrics,
      process_names = strsplit(as.character(row$common_processes), ",")[[1]],
      declared_label = as.character(row$declared_label),
      pulse_score = row$pulse_score
    )
  })
  historical_agg <- bind_rows(rollup_rows)
}

current_rollup <- rollup_session(
  session_id = session_id,
  metrics = metrics,
  process_names = process_rows$process_name,
  declared_label = workload_label,
  pulse_score = NA_real_
)

baseline_inputs <- historical_agg %>%
  filter(session_id != !!session_id) %>%
  group_by(predicted_label) %>%
  summarise(
    avg_cpu = mean(avg_cpu, na.rm = TRUE),
    avg_ram = mean(avg_ram, na.rm = TRUE),
    avg_gpu = mean(avg_gpu, na.rm = TRUE),
    avg_battery_drain = mean(avg_battery_drain, na.rm = TRUE),
    avg_pulse_score = mean(avg_pulse_score, na.rm = TRUE),
    avg_session_duration = mean(avg_session_duration, na.rm = TRUE),
    common_processes = paste(unique(unlist(strsplit(paste(common_processes, collapse = ", "), ","))), collapse = ", "),
    .groups = "drop"
  ) %>%
  rename(workload_label = predicted_label)

if (nrow(baseline_inputs) == 0) {
  baseline_inputs <- data.frame(
    workload_label = current_rollup$predicted_label,
    avg_cpu = current_rollup$avg_cpu,
    avg_ram = current_rollup$avg_ram,
    avg_gpu = current_rollup$avg_gpu,
    avg_battery_drain = current_rollup$avg_battery_drain,
    avg_pulse_score = current_rollup$avg_pulse_score,
    avg_session_duration = current_rollup$avg_session_duration,
    common_processes = current_rollup$common_processes,
    stringsAsFactors = FALSE
  )
}

dbExecute(con, "delete from analytics_workload_baselines where baseline_version = 1")
for (i in seq_len(nrow(baseline_inputs))) {
  row <- baseline_inputs[i, ]
  dbExecute(
    con,
    "
    insert into analytics_workload_baselines (
      workload_label, baseline_version, avg_cpu, avg_ram, avg_gpu,
      avg_battery_drain, avg_pulse_score, avg_session_duration, common_processes, updated_at
    ) values ($1, 1, $2, $3, $4, $5, $6, $7, $8, now())
    on conflict (workload_label, baseline_version) do update set
      avg_cpu = excluded.avg_cpu,
      avg_ram = excluded.avg_ram,
      avg_gpu = excluded.avg_gpu,
      avg_battery_drain = excluded.avg_battery_drain,
      avg_pulse_score = excluded.avg_pulse_score,
      avg_session_duration = excluded.avg_session_duration,
      common_processes = excluded.common_processes,
      updated_at = excluded.updated_at
    ",
    params = list(row$workload_label, row$avg_cpu, row$avg_ram, row$avg_gpu, row$avg_battery_drain, row$avg_pulse_score, row$avg_session_duration, row$common_processes)
  )
}

baseline_row <- pick_personal_baseline(current_rollup, baseline_inputs)
if (is.null(baseline_row)) {
  baseline_row <- baseline_inputs[1, , drop = FALSE]
  baseline_row$distance <- 0
}

baseline_similarity <- clamp_num(100 - baseline_row$distance[[1]] * 26, 12, 100)
anomaly_score <- clamp_num(baseline_row$distance[[1]] * 28 + ifelse(current_rollup$avg_gpu > baseline_row$avg_gpu[[1]] + 12 && current_rollup$unplugged_ratio > 0.2, 12, 0), 0, 100)

classification <- classify_rollup(current_rollup$avg_cpu, current_rollup$avg_ram, current_rollup$avg_gpu, current_rollup$avg_battery_drain, current_rollup$p95_ram, current_rollup$unplugged_ratio, normalize_processes(process_rows$process_name))
anomaly_text <- if (anomaly_score >= 60) {
  if ((current_rollup$avg_battery_drain - baseline_row$avg_battery_drain[[1]]) > 2.5) {
    sprintf("This session appears abnormal due to battery drain running %.1f%%/hr above your %s baseline.", current_rollup$avg_battery_drain - baseline_row$avg_battery_drain[[1]], baseline_row$workload_label[[1]])
  } else if ((current_rollup$avg_ram - baseline_row$avg_ram[[1]]) > 8) {
    sprintf("This session is using %.0f%% more RAM than your usual %s sessions.", current_rollup$avg_ram - baseline_row$avg_ram[[1]], baseline_row$workload_label[[1]])
  } else if ((current_rollup$avg_gpu - baseline_row$avg_gpu[[1]]) > 10 && current_rollup$unplugged_ratio > 0.15) {
    "This session appears abnormal due to sustained GPU activity during battery use."
  } else {
    sprintf("This session is materially different from your normal %s workload.", baseline_row$workload_label[[1]])
  }
} else {
  "Session is within your recent norm."
}

summary_text <- build_summary_text(current_rollup, baseline_row, classification$label, anomaly_score, anomaly_text)

dbExecute(
  con,
  "
  insert into analytics_session_fingerprint (
    session_id, computed_at, workload_label, classification_confidence, anomaly_score,
    baseline_similarity, personal_baseline_label, avg_cpu_vs_baseline, avg_ram_vs_baseline,
    avg_gpu_vs_baseline, battery_drain_vs_baseline, summary_text
  ) values (
    $1::uuid, now(), $2, $3, $4, $5, $6, $7, $8, $9, $10, $11
  )
  on conflict (session_id) do update set
    computed_at = excluded.computed_at,
    workload_label = excluded.workload_label,
    classification_confidence = excluded.classification_confidence,
    anomaly_score = excluded.anomaly_score,
    baseline_similarity = excluded.baseline_similarity,
    personal_baseline_label = excluded.personal_baseline_label,
    avg_cpu_vs_baseline = excluded.avg_cpu_vs_baseline,
    avg_ram_vs_baseline = excluded.avg_ram_vs_baseline,
    avg_gpu_vs_baseline = excluded.avg_gpu_vs_baseline,
    battery_drain_vs_baseline = excluded.battery_drain_vs_baseline,
    summary_text = excluded.summary_text
  ",
  params = list(
    session_id,
    classification$label,
    classification$confidence,
    anomaly_score,
    baseline_similarity,
    baseline_row$workload_label[[1]],
    current_rollup$avg_cpu - baseline_row$avg_cpu[[1]],
    current_rollup$avg_ram - baseline_row$avg_ram[[1]],
    current_rollup$avg_gpu - baseline_row$avg_gpu[[1]],
    current_rollup$avg_battery_drain - baseline_row$avg_battery_drain[[1]],
    summary_text
  )
)

replay_events <- build_replay_events(metrics, process_rows, baseline_row)
dbExecute(con, "delete from analytics_session_replay_events where session_id = $1::uuid", params = list(session_id))
if (nrow(replay_events) > 0) {
  for (i in seq_len(nrow(replay_events))) {
    row <- replay_events[i, ]
    dbExecute(
      con,
      "
      insert into analytics_session_replay_events (
        session_id, event_time, event_type, event_description, metric_name,
        metric_before, metric_after, impact_score
      ) values ($1::uuid, $2, $3, $4, $5, $6, $7, $8)
      ",
      params = list(session_id, row$event_time, row$event_type, row$event_description, row$metric_name, row$metric_before, row$metric_after, row$impact_score)
    )
  }
}

impact_rows <- dbGetQuery(
  con,
  "
  with stress_windows as (
    select
      session_id,
      captured_at,
      case when ram_usage >= 85 then 1 else 0 end as ram_spike,
      case when coalesce(gpu_usage, 0) >= 40 then 1 else 0 end as gpu_spike,
      case when cpu_usage >= 75 or ram_usage >= 80 or coalesce(gpu_usage, 0) >= 35 then 1 else 0 end as stress_window
    from system_metrics
    where captured_at >= now() - interval '14 day'
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
  order by impact_score desc
  limit 25
  "
)

dbExecute(con, "delete from analytics_process_impact")
if (nrow(impact_rows) > 0) {
  for (i in seq_len(nrow(impact_rows))) {
    row <- impact_rows[i, ]
    dbExecute(
      con,
      "
      insert into analytics_process_impact (
        process_name, computed_at, impact_score, stress_rate, avg_memory_mb, avg_proc_gpu
      ) values ($1, now(), $2, $3, $4, $5)
      on conflict (process_name) do update set
        computed_at = excluded.computed_at,
        impact_score = excluded.impact_score,
        stress_rate = excluded.stress_rate,
        avg_memory_mb = excluded.avg_memory_mb,
        avg_proc_gpu = excluded.avg_proc_gpu
      ",
      params = list(
        row$process_name,
        row$impact_score,
        row$stress_rate,
        row$avg_memory_mb,
        row$avg_proc_gpu
      )
    )
  }
}

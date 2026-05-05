clamp_num <- function(x, lo = 0, hi = 100) {
  x <- as.numeric(x)
  if (is.na(x)) {
    return(lo)
  }
  max(lo, min(hi, x))
}

safe_mean <- function(x) {
  x <- x[!is.na(x)]
  if (length(x) == 0) {
    return(0)
  }
  mean(x)
}

safe_sd <- function(x) {
  x <- x[!is.na(x)]
  if (length(x) <= 1) {
    return(0)
  }
  stats::sd(x)
}

safe_quantile <- function(x, prob) {
  x <- x[!is.na(x)]
  if (length(x) == 0) {
    return(0)
  }
  as.numeric(stats::quantile(x, probs = prob, names = FALSE))
}

normalize_processes <- function(processes) {
  if (length(processes) == 0 || all(is.na(processes))) {
    return(character())
  }
  values <- unique(trimws(tolower(processes)))
  values[nzchar(values)]
}

compute_drain_series <- function(metrics) {
  if (nrow(metrics) < 2) {
    return(numeric())
  }
  drains <- c()
  for (i in 2:nrow(metrics)) {
    if (!isTRUE(metrics$plugged_in[[i]]) &&
        !is.na(metrics$battery_percent[[i]]) &&
        !is.na(metrics$battery_percent[[i - 1]])) {
      delta_battery <- max(metrics$battery_percent[[i - 1]] - metrics$battery_percent[[i]], 0)
      delta_hours <- as.numeric(difftime(metrics$captured_at[[i]], metrics$captured_at[[i - 1]], units = "hours"))
      if (!is.na(delta_hours) && delta_hours > 0) {
        drains <- c(drains, delta_battery / max(delta_hours, 1 / 60))
      }
    }
  }
  drains
}

process_flags <- function(processes) {
  list(
    coding = sum(grepl("code|pycharm|idea64|devenv|rstudio|powershell|python|git|node", processes)),
    gaming = sum(grepl("roblox|steam|epic|riot|game|launcher|battle\\.net|valorant|fortnite|league", processes)),
    browsing = sum(grepl("chrome|msedge|firefox|opera|brave", processes)),
    streaming = sum(grepl("obs|vlc|spotify|netflix|youtube|discord|twitch", processes)),
    heavy_memory = sum(grepl("chrome|code|discord|roblox|obs|msedge|chatgpt", processes))
  )
}

classify_rollup <- function(avg_cpu, avg_ram, avg_gpu, avg_drain, p95_ram, unplugged_ratio, processes) {
  flags <- process_flags(processes)
  scores <- c(
    coding = 0,
    gaming = 0,
    browsing = 0,
    streaming = 0,
    idle = 0,
    mixed_heavy = 0,
    unknown = 0.5
  )

  scores["coding"] <- scores["coding"] +
    flags$coding * 2.8 +
    ifelse(avg_gpu < 18, 1.6, 0) +
    ifelse(avg_cpu >= 18 && avg_cpu <= 65, 1.2, 0) +
    ifelse(avg_ram >= 45 && avg_ram <= 88, 1.5, 0)

  scores["gaming"] <- scores["gaming"] +
    flags$gaming * 3.3 +
    ifelse(avg_gpu >= 35, 3.8, 0) +
    ifelse(avg_drain >= 8, 1.2, 0) +
    ifelse(unplugged_ratio >= 0.25 && avg_gpu >= 25, 1.4, 0)

  scores["browsing"] <- scores["browsing"] +
    flags$browsing * 2.4 +
    ifelse(avg_cpu <= 35, 1.1, 0) +
    ifelse(avg_gpu <= 12, 1.1, 0) +
    ifelse(avg_ram <= 78, 0.8, 0)

  scores["streaming"] <- scores["streaming"] +
    flags$streaming * 2.5 +
    ifelse(avg_gpu >= 10 && avg_gpu <= 30, 1.2, 0) +
    ifelse(avg_cpu <= 40, 0.8, 0)

  scores["idle"] <- scores["idle"] +
    ifelse(avg_cpu < 15, 3.5, 0) +
    ifelse(avg_gpu < 6, 2.0, 0) +
    ifelse(avg_ram < 52, 1.6, 0) +
    ifelse(length(processes) <= 4, 0.8, 0)

  scores["mixed_heavy"] <- scores["mixed_heavy"] +
    ifelse(avg_cpu >= 55, 2.1, 0) +
    ifelse(avg_ram >= 80, 2.4, 0) +
    ifelse(avg_gpu >= 28, 2.1, 0) +
    ifelse(flags$heavy_memory >= 3, 1.2, 0) +
    ifelse(p95_ram >= 88, 1.1, 0)

  top_label <- names(which.max(scores))[1]
  sorted_scores <- sort(scores, decreasing = TRUE)
  top_score <- sorted_scores[[1]]
  second_score <- if (length(sorted_scores) > 1) sorted_scores[[2]] else 0

  if (top_score < 2.2) {
    top_label <- "unknown"
  }

  confidence <- clamp_num(58 + (top_score - second_score) * 9, 35, 99)
  reasons <- c()
  if (flags$coding > 0 && top_label == "coding") reasons <- c(reasons, "developer tooling dominated the session mix")
  if (flags$gaming > 0 && top_label == "gaming") reasons <- c(reasons, "game-related processes and sustained GPU load were present")
  if (flags$browsing >= 2 && top_label == "browsing") reasons <- c(reasons, "browser processes dominated the active process mix")
  if (flags$streaming > 0 && top_label == "streaming") reasons <- c(reasons, "media and communication processes matched a streaming pattern")
  if (top_label == "mixed_heavy") reasons <- c(reasons, "CPU, RAM, and GPU stayed elevated together")
  if (top_label == "idle") reasons <- c(reasons, "system activity stayed low across all major resources")
  if (length(reasons) == 0) reasons <- c("session features did not strongly match a single historical cluster")

  list(label = top_label, confidence = confidence, reasons = reasons, scores = scores)
}

compute_local_efficiency <- function(cpu, ram, gpu, plugged_in) {
  unplugged_penalty <- ifelse(isFALSE(plugged_in), 6, 0)
  clamp_num(100 - cpu * 0.34 - ram * 0.43 - gpu * 0.16 - unplugged_penalty, 0, 100)
}

rollup_session <- function(session_id, metrics, process_names, declared_label = "unknown", pulse_score = NA_real_) {
  processes <- normalize_processes(process_names)
  drains <- compute_drain_series(metrics)
  avg_drain <- if (length(drains) > 0) mean(drains, na.rm = TRUE) else 0
  duration_minutes <- if (nrow(metrics) > 1) {
    as.numeric(difftime(max(metrics$captured_at), min(metrics$captured_at), units = "mins"))
  } else {
    0
  }
  unplugged_ratio <- safe_mean(ifelse(is.na(metrics$plugged_in), 0, ifelse(metrics$plugged_in, 0, 1)))
  avg_cpu <- safe_mean(metrics$cpu_usage)
  avg_ram <- safe_mean(metrics$ram_usage)
  avg_gpu <- safe_mean(metrics$gpu_usage)
  p95_ram <- safe_quantile(metrics$ram_usage, 0.95)
  classification <- classify_rollup(avg_cpu, avg_ram, avg_gpu, avg_drain, p95_ram, unplugged_ratio, processes)

  if (is.na(pulse_score)) {
    pulse_score <- clamp_num(100 - avg_cpu * 0.28 - avg_ram * 0.34 - avg_gpu * 0.18 - avg_drain * 1.6, 0, 100)
  }

  data.frame(
    session_id = session_id,
    declared_label = declared_label,
    predicted_label = classification$label,
    classification_confidence = classification$confidence,
    classification_reasons = paste(classification$reasons, collapse = "; "),
    avg_cpu = avg_cpu,
    avg_ram = avg_ram,
    avg_gpu = avg_gpu,
    avg_battery_drain = avg_drain,
    avg_pulse_score = pulse_score,
    avg_session_duration = duration_minutes,
    unplugged_ratio = unplugged_ratio,
    p95_ram = p95_ram,
    common_processes = paste(processes, collapse = ", "),
    stringsAsFactors = FALSE
  )
}

compute_distance <- function(current_rollup, baseline_row) {
  baseline_cpu <- as.numeric(baseline_row$avg_cpu[[1]])
  baseline_ram <- as.numeric(baseline_row$avg_ram[[1]])
  baseline_gpu <- as.numeric(baseline_row$avg_gpu[[1]])
  baseline_drain <- as.numeric(baseline_row$avg_battery_drain[[1]])
  baseline_duration <- as.numeric(baseline_row$avg_session_duration[[1]])
  cpu_z <- abs(current_rollup$avg_cpu - baseline_cpu) / max(12, baseline_cpu * 0.35, 1)
  ram_z <- abs(current_rollup$avg_ram - baseline_ram) / max(8, baseline_ram * 0.18, 1)
  gpu_z <- abs(current_rollup$avg_gpu - baseline_gpu) / max(10, baseline_gpu * 0.35 + 4, 1)
  drain_z <- abs(current_rollup$avg_battery_drain - baseline_drain) / max(2.5, baseline_drain * 0.30 + 1, 1)
  duration_z <- abs(current_rollup$avg_session_duration - baseline_duration) / max(20, baseline_duration * 0.4 + 5, 1)
  mean(c(cpu_z, ram_z, gpu_z, drain_z, duration_z))
}

pick_personal_baseline <- function(current_rollup, baselines) {
  if (nrow(baselines) == 0) {
    return(NULL)
  }
  distances <- vapply(
    seq_len(nrow(baselines)),
    function(i) compute_distance(current_rollup, baselines[i, , drop = FALSE]),
    numeric(1)
  )
  idx <- which.min(distances)
  baseline <- baselines[idx, , drop = FALSE]
  baseline$distance <- distances[[idx]]
  baseline
}

build_summary_text <- function(current_rollup, baseline_row, classification_label, anomaly_score, anomaly_text) {
  lead_line <- sprintf("This session most closely matches your typical %s workload.", baseline_row$workload_label[[1]])
  if (classification_label != baseline_row$workload_label[[1]]) {
    lead_line <- sprintf("This session currently fingerprints as %s, but its nearest personal baseline is %s.", classification_label, baseline_row$workload_label[[1]])
  }

  delta_bits <- c()
  delta_ram <- current_rollup$avg_ram - baseline_row$avg_ram[[1]]
  delta_gpu <- current_rollup$avg_gpu - baseline_row$avg_gpu[[1]]
  delta_drain <- current_rollup$avg_battery_drain - baseline_row$avg_battery_drain[[1]]
  if (abs(delta_ram) >= 5) delta_bits <- c(delta_bits, sprintf("RAM averaged %.0f points %s baseline", abs(delta_ram), ifelse(delta_ram >= 0, "above", "below")))
  if (abs(delta_gpu) >= 6) delta_bits <- c(delta_bits, sprintf("GPU averaged %.0f points %s baseline", abs(delta_gpu), ifelse(delta_gpu >= 0, "above", "below")))
  if (current_rollup$avg_battery_drain > 0 && abs(delta_drain) >= 1.5) delta_bits <- c(delta_bits, sprintf("battery drain ran %.1f%%/hr %s baseline", abs(delta_drain), ifelse(delta_drain >= 0, "above", "below")))
  if (length(delta_bits) == 0) delta_bits <- c("resource behavior stayed close to your personal norm")

  risk_line <- if (anomaly_score >= 65) {
    anomaly_text
  } else if (anomaly_score >= 40) {
    "The session is moderately off-pattern, but still within your broader operating range."
  } else {
    "The session is tracking as normal for your machine history."
  }

  paste(lead_line, paste(delta_bits, collapse = ", "), risk_line)
}

build_replay_events <- function(metrics, process_rows, baseline_row) {
  events <- list()
  if (nrow(metrics) < 2) {
    return(data.frame())
  }

  add_event <- function(event_time, event_type, description, metric_name, before, after, impact) {
    events[[length(events) + 1]] <<- data.frame(
      event_time = as.POSIXct(event_time, tz = "UTC"),
      event_type = event_type,
      event_description = description,
      metric_name = metric_name,
      metric_before = as.numeric(before),
      metric_after = as.numeric(after),
      impact_score = round(as.numeric(impact), 2),
      stringsAsFactors = FALSE
    )
  }

  cpu_threshold <- max(70, baseline_row$avg_cpu[[1]] + 18)
  ram_threshold <- max(80, baseline_row$avg_ram[[1]] + 8)
  gpu_threshold <- max(18, baseline_row$avg_gpu[[1]] + 12)
  drain_threshold <- max(6, baseline_row$avg_battery_drain[[1]] + 3)

  prev_abnormal <- FALSE
  prev_drain <- 0
  for (i in 2:nrow(metrics)) {
    prev <- metrics[i - 1, ]
    cur <- metrics[i, ]

    if (prev$cpu_usage < cpu_threshold && cur$cpu_usage >= cpu_threshold) {
      add_event(cur$captured_at, "cpu_surge", sprintf("CPU surged from %.0f%% to %.0f%%.", prev$cpu_usage, cur$cpu_usage), "cpu_usage", prev$cpu_usage, cur$cpu_usage, cur$cpu_usage - prev$cpu_usage)
    }
    if ((cur$ram_usage - prev$ram_usage) >= 8 || (prev$ram_usage < ram_threshold && cur$ram_usage >= ram_threshold)) {
      add_event(cur$captured_at, "ram_spike", sprintf("RAM rose from %.0f%% to %.0f%%.", prev$ram_usage, cur$ram_usage), "ram_usage", prev$ram_usage, cur$ram_usage, max(cur$ram_usage - prev$ram_usage, cur$ram_usage - baseline_row$avg_ram[[1]]))
    }
    if (prev$gpu_usage < gpu_threshold && cur$gpu_usage >= gpu_threshold) {
      desc <- if (isFALSE(cur$plugged_in)) sprintf("dGPU-style load activated while unplugged, moving from %.0f%% to %.0f%% GPU.", prev$gpu_usage, cur$gpu_usage) else sprintf("GPU activity jumped from %.0f%% to %.0f%%.", prev$gpu_usage, cur$gpu_usage)
      add_event(cur$captured_at, "gpu_activation", desc, "gpu_usage", prev$gpu_usage, cur$gpu_usage, cur$gpu_usage - prev$gpu_usage)
    }

    cur_eff <- compute_local_efficiency(cur$cpu_usage, cur$ram_usage, cur$gpu_usage, cur$plugged_in)
    prev_eff <- compute_local_efficiency(prev$cpu_usage, prev$ram_usage, prev$gpu_usage, prev$plugged_in)
    if ((prev_eff - cur_eff) >= 8) {
      add_event(cur$captured_at, "efficiency_drop", sprintf("Efficiency proxy fell %.0f points as load intensified.", prev_eff - cur_eff), "efficiency_proxy", prev_eff, cur_eff, prev_eff - cur_eff)
    }

    sample_drain <- 0
    if (!isTRUE(cur$plugged_in) && !is.na(cur$battery_percent) && !is.na(prev$battery_percent)) {
      delta_battery <- max(prev$battery_percent - cur$battery_percent, 0)
      delta_hours <- as.numeric(difftime(cur$captured_at, prev$captured_at, units = "hours"))
      if (!is.na(delta_hours) && delta_hours > 0) {
        sample_drain <- delta_battery / max(delta_hours, 1 / 60)
      }
    }
    if (sample_drain >= drain_threshold && prev_drain < drain_threshold) {
      add_event(cur$captured_at, "battery_inflection", sprintf("Battery drain accelerated to %.1f%%/hr.", sample_drain), "battery_drain", prev_drain, sample_drain, sample_drain)
    }
    prev_drain <- sample_drain

    anomaly_proxy <- mean(c(
      abs(cur$cpu_usage - baseline_row$avg_cpu[[1]]) / max(12, baseline_row$avg_cpu[[1]] * 0.3, 1),
      abs(cur$ram_usage - baseline_row$avg_ram[[1]]) / max(8, baseline_row$avg_ram[[1]] * 0.18, 1),
      abs(cur$gpu_usage - baseline_row$avg_gpu[[1]]) / max(10, baseline_row$avg_gpu[[1]] * 0.35 + 4, 1)
    ))
    cur_abnormal <- anomaly_proxy >= 1.8
    if (!prev_abnormal && cur_abnormal) {
      add_event(cur$captured_at, "abnormal_transition", "Session moved from normal behavior into an abnormal operating state.", "anomaly_proxy", anomaly_proxy - 0.3, anomaly_proxy, anomaly_proxy * 12)
    }
    prev_abnormal <- cur_abnormal
  }

  if (nrow(process_rows) > 0) {
    proc_dt <- as.data.table(process_rows)
    proc_dt[, captured_at := as.POSIXct(captured_at, tz = "UTC")]
    proc_dt[, process_name := tolower(process_name)]
    proc_dt <- proc_dt[memory_mb >= 140]
    if (nrow(proc_dt) > 0) {
      snapshots <- split(proc_dt, proc_dt$captured_at)
      prev_names <- character()
      for (snap_time in names(snapshots)) {
        snap <- snapshots[[snap_time]][order(-memory_mb)]
        cur_names <- unique(snap$process_name)
        launched <- setdiff(cur_names, prev_names)
        if (length(launched) > 0) {
          browser_launches <- launched[grepl("chrome|msedge|firefox|opera|brave", launched)]
          if (length(browser_launches) >= 1) {
            add_event(as.POSIXct(snap_time, tz = "UTC"), "process_launch", "Browser cluster launched into the active memory set.", "memory_mb", NA, max(snap$memory_mb, na.rm = TRUE), 18)
          }
          top_new <- launched[1]
          top_row <- snap[process_name == top_new][1]
          if (nrow(top_row) > 0 && !grepl("chrome|msedge|firefox|opera|brave", top_new)) {
            add_event(as.POSIXct(snap_time, tz = "UTC"), "process_launch", sprintf("%s entered the active process mix.", top_new), "memory_mb", NA, top_row$memory_mb[[1]], min(25, top_row$memory_mb[[1]] / 18))
          }
        }
        prev_names <- cur_names
      }
    }
  }

  if (length(events) == 0) {
    return(data.frame())
  }

  dplyr::bind_rows(events) %>%
    dplyr::arrange(event_time, dplyr::desc(impact_score)) %>%
    dplyr::distinct(event_time, event_type, event_description, .keep_all = TRUE) %>%
    dplyr::slice_tail(n = 16)
}

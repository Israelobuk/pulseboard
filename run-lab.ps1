param(
    [int]$CollectorInterval = 2,
    [string]$WorkloadLabel = "",
    [int]$ShinyPort = 8787
)

$ErrorActionPreference = "Stop"

$collectorScript = Join-Path $PSScriptRoot "run-collector.ps1"
$shinyScript = Join-Path $PSScriptRoot "run-shiny.ps1"

if (-not (Test-Path $collectorScript)) {
    Write-Error "Collector launcher not found: $collectorScript"
}

if (-not (Test-Path $shinyScript)) {
    Write-Error "Shiny launcher not found: $shinyScript"
}

$collectorArgs = @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-File", $collectorScript,
    "-Interval", $CollectorInterval
)

if ($WorkloadLabel) {
    $collectorArgs += @("-WorkloadLabel", $WorkloadLabel)
}

$shinyArgs = @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-File", $shinyScript,
    "-Port", $ShinyPort
)

Write-Host "Starting collector in a new terminal window..."
Start-Process powershell -ArgumentList $collectorArgs | Out-Null

Write-Host "Starting Shiny dashboard in a new terminal window..."
Start-Process powershell -ArgumentList $shinyArgs | Out-Null

Write-Host "PulseBoard DS Lab launchers started."

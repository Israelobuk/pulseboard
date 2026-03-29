param(
    [int]$Interval = 2,
    [string]$WorkloadLabel = "",
    [int]$ProcessLimit = 15,
    [switch]$Once
)

$ErrorActionPreference = "Stop"

$pythonExe = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonExe) {
    Write-Error "python was not found on PATH."
}

$collectorPath = Join-Path $PSScriptRoot "collector.py"
if (-not (Test-Path $collectorPath)) {
    Write-Error "collector.py not found: $collectorPath"
}

$args = @($collectorPath, "--interval", $Interval, "--process-limit", $ProcessLimit)

if ($WorkloadLabel) {
    $args += @("--workload-label", $WorkloadLabel)
}

if ($Once) {
    $args += "--once"
}

& $pythonExe.Source @args
exit $LASTEXITCODE

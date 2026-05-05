param()

$ErrorActionPreference = "Stop"

$preferredVenvs = @(
    (Join-Path $PSScriptRoot ".venv312\\Scripts\\python.exe"),
    (Join-Path $PSScriptRoot ".venv\\Scripts\\python.exe")
)

$pythonPath = $null
foreach ($candidate in $preferredVenvs) {
    if (Test-Path $candidate) {
        $pythonPath = $candidate
        break
    }
}

if (-not $pythonPath) {
    $pythonExe = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonExe) {
        Write-Error "python was not found on PATH. Create a venv with: python -m venv .venv"
    }
    $pythonPath = $pythonExe.Source
}

if (-not $env:PULSEBOARD_DB_DSN) {
    Write-Warning "PULSEBOARD_DB_DSN is not set. The app will run desktop-only without PostgreSQL history."
}

# Avoid broken Python prefix/stdlib resolution from stale env vars.
$env:PYTHONHOME = $null
$env:PYTHONPATH = $null

& $pythonPath (Join-Path $PSScriptRoot "Pulseboard.py")
exit $LASTEXITCODE

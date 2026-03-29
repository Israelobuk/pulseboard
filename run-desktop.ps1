param()

$ErrorActionPreference = "Stop"

$pythonExe = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonExe) {
    Write-Error "python was not found on PATH."
}

if (-not $env:PULSEBOARD_DB_DSN) {
    Write-Warning "PULSEBOARD_DB_DSN is not set. The app will run desktop-only without PostgreSQL history."
}

& $pythonExe.Source (Join-Path $PSScriptRoot "Pulseboard.py")
exit $LASTEXITCODE

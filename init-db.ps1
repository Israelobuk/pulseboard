param(
    [string]$Dsn = $env:PULSEBOARD_DB_DSN,
    [string]$Database = $env:PGDATABASE
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command psql -ErrorAction SilentlyContinue)) {
    Write-Error "psql was not found on PATH. Install PostgreSQL client tools or add psql to PATH."
}

$schemaPath = Join-Path $PSScriptRoot "sql\schema.sql"

if (-not (Test-Path $schemaPath)) {
    Write-Error "Schema file not found: $schemaPath"
}

if ($Dsn) {
    Write-Host "Initializing PulseBoard schema using PULSEBOARD_DB_DSN..."
    & psql $Dsn -f $schemaPath
    exit $LASTEXITCODE
}

if ($Database) {
    Write-Host "Initializing PulseBoard schema in database '$Database'..."
    & psql -d $Database -f $schemaPath
    exit $LASTEXITCODE
}

Write-Error "Set PULSEBOARD_DB_DSN or PGDATABASE before running init-db.ps1."

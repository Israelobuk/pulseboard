param()

$ErrorActionPreference = "Stop"

$rscriptExe = Get-Command Rscript -ErrorAction SilentlyContinue
if (-not $rscriptExe) {
    Write-Error "Rscript was not found on PATH. Install R or add Rscript to PATH."
}

if (-not $env:R_LIBS_USER) {
    $env:R_LIBS_USER = Join-Path $HOME "Documents\R\win-library\4.5"
}

New-Item -ItemType Directory -Force -Path $env:R_LIBS_USER | Out-Null

$expr = @"
dir.create(Sys.getenv('R_LIBS_USER'), recursive=TRUE, showWarnings=FALSE)
.libPaths(c(Sys.getenv('R_LIBS_USER'), .libPaths()))
install.packages(
  c('shiny','DBI','RPostgres','dplyr','data.table','tidyr','lubridate','ggplot2','plotly','DT','scales','glue','htmltools'),
  lib=Sys.getenv('R_LIBS_USER'),
  repos='https://cloud.r-project.org'
)
"@

& $rscriptExe.Source -e $expr
exit $LASTEXITCODE

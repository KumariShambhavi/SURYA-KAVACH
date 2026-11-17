# SURYA-KAVACH one-command launcher (Windows / PowerShell)
# Converts raw PRADAN zips (if needed), ingests, then serves the dashboard.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

Write-Host "SURYA-KAVACH starting..." -ForegroundColor Cyan

# 1. Convert raw PRADAN zips -> CSVs if none converted yet
$rawSolexs = Join-Path "data\raw\solexs" "*.csv"
if (-not (Test-Path $rawSolexs)) {
    Write-Host "Converting raw PRADAN zips (data/raw is empty)..." -ForegroundColor Yellow
    python -m solarflare.cli convert
}

# 2. Ingest (build catalogue + train forecaster) if the DB has no run yet
if (-not (Test-Path "data\flare_ops.db")) {
    Write-Host "Running first ingest..." -ForegroundColor Yellow
    python -m solarflare.cli ingest --force
}

# 3. Serve
$port = if ($env:PORT) { $env:PORT } else { 8000 }
Write-Host "Serving dashboard at http://127.0.0.1:$port" -ForegroundColor Green
python -m solarflare.cli serve --host 127.0.0.1 --port $port

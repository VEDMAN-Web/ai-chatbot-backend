# ==============================================================================
# SparkBeta Tech AI Chatbot - Master QA Automated Test Runner (PowerShell)
# Target: Staging Environment (Tajlandia.com, VarsoviaEstate.com, Thai.estate)
# ==============================================================================

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

Write-Host "==============================================================================" -ForegroundColor Cyan
Write-Host "SPARKBETA TECH AI CHATBOT - AUTOMATED QA TEST RUNNER (POWERSHELL)" -ForegroundColor Cyan
Write-Host "==============================================================================" -ForegroundColor Cyan

# 1. Environment Guard: Non-negotiable Rule 1 (Staging Only)
$env:ENVIRONMENT = "staging"
Write-Host "[1/3] Environment check: ENVIRONMENT=$($env:ENVIRONMENT)" -ForegroundColor Green

# 2. Check Backend Server Health
Write-Host "[2/3] Checking backend health on http://127.0.0.1:8000..." -ForegroundColor Yellow
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -Method Get -TimeoutSec 5 -ErrorAction Stop
    Write-Host "      Backend health verified: $($health | ConvertTo-Json -Compress)" -ForegroundColor Green
} catch {
    Write-Host "      WARNING: Local backend on http://127.0.0.1:8000 is not responding." -ForegroundColor Red
    Write-Host "      Please ensure the backend is running via: python -m uvicorn app.main:app --host 127.0.0.1 --port 8000" -ForegroundColor Yellow
}

# 3. Execute Parallel Test Runner
Write-Host "[3/3] Launching parallel test execution..." -ForegroundColor Yellow
python scripts/run_qa_parallel.py @args

$exitCode = $LASTEXITCODE
if ($exitCode -eq 0) {
    Write-Host "`n>>> ALL AUTOMATED QA TEST SUITES PASSED (EXIT CODE 0) <<<" -ForegroundColor Green
} else {
    Write-Host "`n>>> AUTOMATED QA TEST SUITE FAILED (EXIT CODE $exitCode) <<<" -ForegroundColor Red
}

exit $exitCode


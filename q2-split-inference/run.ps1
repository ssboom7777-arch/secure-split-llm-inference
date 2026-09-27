$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Weights = Join-Path $Root "assets\model.safetensors"
$Tokenizer = Join-Path $Root "assets\tokenizer.json"
$Runtime = Join-Path $Root "runtime"
New-Item -ItemType Directory -Force -Path $Runtime | Out-Null

if (-not (Test-Path -LiteralPath $Python)) {
    & (Join-Path $Root "setup.ps1")
    if ($LASTEXITCODE -ne 0) { throw "Runtime setup failed" }
}
if (-not (Test-Path -LiteralPath $Weights) -or -not (Test-Path -LiteralPath $Tokenizer)) {
    & (Join-Path $Root "download-model.ps1")
    if ($LASTEXITCODE -ne 0) { throw "Model download failed" }
}
if (-not (Test-Path -LiteralPath $Tokenizer)) { throw "Missing tokenizer: $Tokenizer" }

# Make one-click startup idempotent: if both halves are already healthy, keep
# the existing processes and return successfully.
try { $cloudReady = (Invoke-RestMethod -Uri "http://127.0.0.1:8101/health" -TimeoutSec 2).status -eq "ok" } catch { $cloudReady = $false }
try { $enterpriseReady = (Invoke-RestMethod -Uri "http://127.0.0.1:8100/health" -TimeoutSec 2).status -eq "ok" } catch { $enterpriseReady = $false }
if ($cloudReady -and $enterpriseReady) {
    Write-Host "Split inference is already running: http://127.0.0.1:8100"
    exit 0
}
if ($cloudReady -or $enterpriseReady) {
    throw "Only one split service is healthy. Run stop.ps1, then run run.ps1 again."
}

$CloudLog = Join-Path $Runtime "cloud.log"
$EnterpriseLog = Join-Path $Runtime "enterprise.log"
$Cloud = Start-Process -FilePath $Python -ArgumentList @(
    (Join-Path $Root "cloud_server.py"), "--weights", $Weights
) -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput $CloudLog -RedirectStandardError (Join-Path $Runtime "cloud.err.log") -PassThru

$deadline = (Get-Date).AddMinutes(2)
do {
    Start-Sleep -Milliseconds 500
    try { $ready = (Invoke-RestMethod -Uri "http://127.0.0.1:8101/health" -TimeoutSec 2).status -eq "ok" } catch { $ready = $false }
    if ($Cloud.HasExited) { throw "Cloud service exited. See $CloudLog" }
} until ($ready -or (Get-Date) -gt $deadline)
if (-not $ready) { throw "Cloud service did not become ready" }

$Enterprise = Start-Process -FilePath $Python -ArgumentList @(
    (Join-Path $Root "enterprise_server.py"), "--weights", $Weights, "--tokenizer", $Tokenizer
) -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput $EnterpriseLog -RedirectStandardError (Join-Path $Runtime "enterprise.err.log") -PassThru

$deadline = (Get-Date).AddMinutes(1)
do {
    Start-Sleep -Milliseconds 300
    try { $ready = (Invoke-RestMethod -Uri "http://127.0.0.1:8100/health" -TimeoutSec 2).status -eq "ok" } catch { $ready = $false }
    if ($Enterprise.HasExited) { throw "Enterprise service exited. See $EnterpriseLog" }
} until ($ready -or (Get-Date) -gt $deadline)
if (-not $ready) { throw "Enterprise service did not become ready" }

@{ cloud = $Cloud.Id; enterprise = $Enterprise.Id } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Runtime "pids.json") -Encoding UTF8
Write-Host "Split inference is ready: http://127.0.0.1:8100"
Write-Host "See README.md for curl and PowerShell request examples."

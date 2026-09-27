$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
try { $ready = (Invoke-RestMethod -Uri "http://127.0.0.1:8100/health" -TimeoutSec 2).status -eq "ok" } catch { $ready = $false }
if (-not $ready) { & (Join-Path $Root "run.ps1") }
& $Python (Join-Path $Root "verify_http_e2e.py") `
  --weights (Join-Path $Root "assets\model.safetensors") `
  --tokenizer (Join-Path $Root "assets\tokenizer.json") `
  --output (Join-Path $Root "results\http-e2e-correctness.json")
if ($LASTEXITCODE -ne 0) { throw "HTTP end-to-end verification failed" }

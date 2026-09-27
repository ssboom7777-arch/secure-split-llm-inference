$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
& (Join-Path $Root ".venv\Scripts\python.exe") (Join-Path $Root "verify_correctness.py") `
  --weights (Join-Path $Root "assets\model.safetensors") `
  --tokenizer (Join-Path $Root "assets\tokenizer.json") `
  --output (Join-Path $Root "results\correctness.json")
if ($LASTEXITCODE -ne 0) { throw "Correctness verification failed" }

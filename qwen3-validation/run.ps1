param([int]$MaxNewTokens = 4, [int]$Jobs = 8)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root "..\q2-split-inference\.venv\Scripts\python.exe"
& $Python (Join-Path $Root "run_experiments.py") --max-new-tokens $MaxNewTokens --jobs $Jobs
if ($LASTEXITCODE -ne 0) { throw "Qwen3 validation failed" }

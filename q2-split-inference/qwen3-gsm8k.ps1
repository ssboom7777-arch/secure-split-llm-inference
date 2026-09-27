param([int]$Samples = 50, [int]$MaxTokens = 128)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Workspace = Resolve-Path (Join-Path $Root "..\..")
$Python = Join-Path $Root ".venv\Scripts\python.exe"
& (Join-Path $Workspace "nanoagent\local-demo\start-model.ps1")
if ($LASTEXITCODE -ne 0) { throw "Qwen3 server failed to start" }
& $Python (Join-Path $Root "evaluate_chat_api_gsm8k.py") `
  --dataset (Join-Path $Root "data\gsm8k-test.jsonl") --samples $Samples --max-tokens $MaxTokens `
  --output (Join-Path $Root "results\gsm8k-qwen3-1.7b.json")
if ($LASTEXITCODE -ne 0) { throw "Qwen3 GSM8K evaluation failed" }

param([int]$Samples = 50, [int]$NoiseSamples = 10, [int]$MaxNewTokens = 128)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root "..\q2-split-inference\.venv\Scripts\python.exe"
& $Python (Join-Path $Root "gsm8k_split_noise.py") --samples $Samples --max-new-tokens $MaxNewTokens --noise 0 --output (Join-Path $Root "results\qwen3-1.7b-gsm8k-split-noise.json")
if ($LASTEXITCODE -ne 0) { throw "Qwen3 split GSM8K baseline failed" }
& $Python (Join-Path $Root "gsm8k_split_noise.py") --samples $NoiseSamples --max-new-tokens $MaxNewTokens --noise 0.5 1 --output (Join-Path $Root "results\qwen3-1.7b-gsm8k-noise-subset.json")
if ($LASTEXITCODE -ne 0) { throw "Qwen3 split GSM8K evaluation failed" }

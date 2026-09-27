param(
    [int]$Samples = 50,
    [int]$MaxNewTokens = 64,
    [int]$Offset = 0
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Weights = Join-Path $Root "..\q1-poc\assets\model.safetensors"
$Tokenizer = Join-Path $Root "assets\tokenizer.json"

if (-not (Test-Path -LiteralPath $Python)) {
    & (Join-Path $Root "setup.ps1")
    if ($LASTEXITCODE -ne 0) { throw "Runtime setup failed" }
}
if (-not (Test-Path -LiteralPath $Weights)) { throw "Missing MiniMind weights. Run q1-poc\run.ps1 first." }

& $Python (Join-Path $Root "evaluate_gsm8k.py") `
    --weights $Weights --tokenizer $Tokenizer `
    --dataset (Join-Path $Root "data\gsm8k-test.jsonl") `
    --samples $Samples --offset $Offset --max-new-tokens $MaxNewTokens `
    --output (Join-Path $Root "results\gsm8k-comparison.json")
if ($LASTEXITCODE -ne 0) { throw "GSM8K evaluation failed" }

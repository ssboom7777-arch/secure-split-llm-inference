param([int]$MaxNewTokens = 32)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Q2 = Resolve-Path (Join-Path $Root "..\q2-split-inference")
$Python = Join-Path $Q2 ".venv\Scripts\python.exe"
$Weights = Join-Path $Root "..\q2-split-inference\assets\model.safetensors"
$Tokenizer = Join-Path $Root "..\q2-split-inference\assets\tokenizer.json"
$AttackResults = Join-Path $Root "results\results.json"
$Output = Join-Path $Root "results\noise-utility-tradeoff.json"
$Chart = Join-Path $Root "results\noise-utility-tradeoff.svg"

if (-not (Test-Path -LiteralPath $Python)) { & (Join-Path $Q2 "setup.ps1") }
if (-not (Test-Path -LiteralPath $Weights) -or -not (Test-Path -LiteralPath $AttackResults)) {
    & (Join-Path $Root "run.ps1")
}
& $Python (Join-Path $Root "evaluate_noise_generation.py") `
    --weights $Weights --tokenizer $Tokenizer --q2-dir $Q2 `
    --attack-results $AttackResults --max-new-tokens $MaxNewTokens `
    --output $Output --chart $Chart
if ($LASTEXITCODE -ne 0) { throw "Noise utility experiment failed" }

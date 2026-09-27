$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Q2 = Resolve-Path (Join-Path $Root "..\q2-split-inference")
$Python = Join-Path $Q2 ".venv\Scripts\python.exe"
& $Python (Join-Path $Root "fixed_2e8c_sim.py") `
    --schedule (Join-Path $Root "results\sustained-schedule.json") `
    --serial-result (Join-Path $Root "results\sustained-serial.json") `
    --tokenizer (Join-Path $Root "..\q2-split-inference\assets\tokenizer.json") `
    --output (Join-Path $Root "results\fixed-2e8c-comparison.json")
if ($LASTEXITCODE -ne 0) { throw "Fixed 2-enterprise/8-cloud simulation failed" }

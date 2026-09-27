$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Q2 = Resolve-Path (Join-Path $Root "..\q2-split-inference")
$Python = Join-Path $Q2 ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { & (Join-Path $Q2 "setup.ps1") }
if (-not (Test-Path -LiteralPath (Join-Path $Q2 "assets\model.safetensors"))) { & (Join-Path $Q2 "download-model.ps1") }
$Weights = Resolve-Path (Join-Path $Root "..\q2-split-inference\assets\model.safetensors")
$Results = Join-Path $Root "results"
New-Item -ItemType Directory -Force -Path $Results | Out-Null

& $Python (Join-Path $Root "benchmark_local.py") --weights $Weights --q2-dir $Q2 --output (Join-Path $Results "local-benchmark.json")
if ($LASTEXITCODE -ne 0) { throw "Local benchmark failed" }

& $Python (Join-Path $Root "calibrate.py") `
    --benchmark (Join-Path $Results "local-benchmark.json") `
    --model (Join-Path $Root "profiles\models\minimind3.json") `
    --q3-result (Join-Path $Root "..\q3-scheduler-poc\results\optimized.json") `
    --output (Join-Path $Results "local-validation.json")
if ($LASTEXITCODE -ne 0) { throw "Calibration failed" }

& $Python (Join-Path $Root "generate_calibration.py") `
    --local-validation (Join-Path $Results "local-validation.json") `
    --model (Join-Path $Root "profiles\models\minimind3.json") `
    --open-loop (Join-Path $Results "open-loop-validation\qps-0.10.json") `
    --open-loop (Join-Path $Results "open-loop-validation\qps-0.13.json") `
    --template-workload (Join-Path $Root "profiles\workloads\minimind-open-loop-slo.json") `
    --hardware-output (Join-Path $Root "profiles\hardware\local-calibrated-cpu.json") `
    --workload-output (Join-Path $Root "profiles\workloads\minimind-open-loop-slo.json") `
    --report-output (Join-Path $Results "calibration-parameters.json")
if ($LASTEXITCODE -ne 0) { throw "End-to-end calibration profile generation failed" }

& $Python (Join-Path $Root "estimate.py") `
    --model (Join-Path $Root "profiles\models\minimind3.json") `
    --hardware (Join-Path $Root "profiles\hardware\local-calibrated-cpu.json") `
    --workload (Join-Path $Root "profiles\workloads\minimind-open-loop-slo.json") `
    --output (Join-Path $Results "minimind-open-loop-corrected.json")
if ($LASTEXITCODE -ne 0) { throw "Corrected local capacity estimate failed" }

& $Python (Join-Path $Root "estimate.py") `
    --model (Join-Path $Root "profiles\models\qwen3-32b.json") `
    --hardware (Join-Path $Root "profiles\hardware\h20x8.json") `
    --workload (Join-Path $Root "profiles\workloads\4k-slo.json") `
    --output (Join-Path $Results "qwen3-32b-h20x8.json")
if ($LASTEXITCODE -ne 0) { throw "Target estimate failed" }

& $Python (Join-Path $Root "sweep.py") `
    --model (Join-Path $Root "profiles\models\qwen3-32b.json") `
    --hardware (Join-Path $Root "profiles\hardware\h20x8.json") `
    --workload (Join-Path $Root "profiles\workloads\4k-slo.json") `
    --output (Join-Path $Results "qwen3-32b-h20x8-sweep.json")
if ($LASTEXITCODE -ne 0) { throw "Target sweep failed" }

param(
    [int]$DurationSeconds = 300,
    [int]$Users = 5,
    [double]$MeanUserIntervalSeconds = 8.0,
    [int]$Seed = 20260925,
    [double]$BatchWindowMs = 3.0,
    [switch]$OptimizedOnly
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Q2 = Resolve-Path (Join-Path $Root "..\q2-split-inference")
$Python = Join-Path $Q2 ".venv\Scripts\python.exe"
$Weights = Resolve-Path (Join-Path $Q2 "assets\model.safetensors")
$Tokenizer = Resolve-Path (Join-Path $Q2 "assets\tokenizer.json")
$Runtime = Join-Path $Root "runtime"
$Results = Join-Path $Root "results"
$Schedule = Join-Path $Results "sustained-schedule.json"
New-Item -ItemType Directory -Force -Path $Runtime, $Results | Out-Null

if (-not $OptimizedOnly) {
    & $Python (Join-Path $Root "sustained_load_test.py") --make-schedule --schedule $Schedule `
        --duration $DurationSeconds --users $Users --mean-user-interval $MeanUserIntervalSeconds --seed $Seed
    if ($LASTEXITCODE -ne 0) { throw "Schedule generation failed" }
} elseif (-not (Test-Path -LiteralPath $Schedule)) {
    throw "OptimizedOnly requires existing schedule: $Schedule"
}

function Wait-Health([string]$Url, [System.Diagnostics.Process]$Process) {
    $deadline = (Get-Date).AddMinutes(3)
    do {
        Start-Sleep -Milliseconds 300
        try { $ready = (Invoke-RestMethod -Uri $Url -TimeoutSec 2).status -eq "ok" } catch { $ready = $false }
        if ($Process.HasExited) { throw "Service exited before becoming healthy" }
    } until ($ready -or (Get-Date) -gt $deadline)
    if (-not $ready) { throw "Timed out waiting for $Url" }
}

function Run-Case([string]$Label, [bool]$Serialize, [int]$MaxBatch, [string]$Policy, [double]$Window) {
    Write-Host "Starting $Label five-user sustained run..."
    $cloud = $null
    $enterprise = $null
    try {
        $cloud = Start-Process -FilePath $Python -ArgumentList @(
            (Join-Path $Q2 "cloud_server.py"), "--weights", $Weights, "--port", "8301",
            "--max-batch-size", $MaxBatch, "--batch-window-ms", $Window, "--scheduling-policy", $Policy
        ) -WorkingDirectory $Q2 -WindowStyle Hidden -RedirectStandardOutput (Join-Path $Runtime "$Label-cloud.log") -RedirectStandardError (Join-Path $Runtime "$Label-cloud.err.log") -PassThru
        Wait-Health "http://127.0.0.1:8301/health" $cloud
        $enterpriseArgs = @(
            (Join-Path $Q2 "enterprise_server.py"), "--weights", $Weights, "--tokenizer", $Tokenizer,
            "--cloud-url", "http://127.0.0.1:8301", "--port", "8300",
            "--network-one-way-ms", "2.5", "--bandwidth-gbps", "10"
        )
        if ($Serialize) {
            $enterpriseArgs += "--serialize-requests"
        } else {
            $enterpriseArgs += @("--prefill-chunk-size", "128", "--prefill-chunk-threshold", "256")
        }
        $enterprise = Start-Process -FilePath $Python -ArgumentList $enterpriseArgs -WorkingDirectory $Q2 -WindowStyle Hidden -RedirectStandardOutput (Join-Path $Runtime "$Label-enterprise.log") -RedirectStandardError (Join-Path $Runtime "$Label-enterprise.err.log") -PassThru
        Wait-Health "http://127.0.0.1:8300/health" $enterprise
        & $Python (Join-Path $Root "sustained_load_test.py") --schedule $Schedule `
            --duration $DurationSeconds --enterprise-url "http://127.0.0.1:8300" `
            --cloud-url "http://127.0.0.1:8301" --label $Label --output (Join-Path $Results "$Label.json")
        if ($LASTEXITCODE -ne 0) { throw "$Label sustained test failed" }
    }
    finally {
        if ($enterprise -and -not $enterprise.HasExited) { Stop-Process -Id $enterprise.Id -Force }
        if ($cloud -and -not $cloud.HasExited) { Stop-Process -Id $cloud.Id -Force }
        Start-Sleep -Milliseconds 500
    }
}

if (-not $OptimizedOnly) { Run-Case "sustained-serial" $true 1 "fifo" 0 }
Run-Case "sustained-optimized" $false $Users "slo-aware" $BatchWindowMs
& $Python (Join-Path $Root "summarize_sustained.py") `
    (Join-Path $Results "sustained-serial.json") `
    (Join-Path $Results "sustained-optimized.json") `
    (Join-Path $Results "sustained-comparison.json")
if ($LASTEXITCODE -ne 0) { throw "Sustained summary failed" }
Write-Host "Sustained results written to $Results"

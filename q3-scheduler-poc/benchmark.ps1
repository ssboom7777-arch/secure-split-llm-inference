param(
    [int]$Requests = 8,
    [int]$Concurrency = 8,
    [int]$MaxNewTokens = 8,
    [double]$BatchWindowMs = 3.0,
    [int]$PrefillChunkSize = 16
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
New-Item -ItemType Directory -Force -Path $Runtime, $Results | Out-Null

if (-not (Test-Path -LiteralPath $Python)) { throw "Run q2-split-inference\setup.ps1 first." }

function Wait-Health([string]$Url, [System.Diagnostics.Process]$Process) {
    $deadline = (Get-Date).AddMinutes(3)
    do {
        Start-Sleep -Milliseconds 300
        try { $ready = (Invoke-RestMethod -Uri $Url -TimeoutSec 2).status -eq "ok" } catch { $ready = $false }
        if ($Process.HasExited) { throw "Service exited before becoming healthy." }
    } until ($ready -or (Get-Date) -gt $deadline)
    if (-not $ready) { throw "Timed out waiting for $Url" }
}

function Run-Case([string]$Label, [int]$MaxBatchSize, [double]$WindowMs, [string]$Policy, [bool]$Serialize, [int]$ChunkSize) {
    Write-Host "Running $Label (batch=$MaxBatchSize, window=${WindowMs}ms, policy=$Policy, serial=$Serialize, chunk=$ChunkSize)..."
    $cloud = $null
    $enterprise = $null
    try {
        $cloud = Start-Process -FilePath $Python -ArgumentList @(
            (Join-Path $Q2 "cloud_server.py"), "--weights", $Weights,
            "--port", "8201", "--max-batch-size", $MaxBatchSize, "--batch-window-ms", $WindowMs,
            "--scheduling-policy", $Policy
        ) -WorkingDirectory $Q2 -WindowStyle Hidden -RedirectStandardOutput (Join-Path $Runtime "$Label-cloud.log") -RedirectStandardError (Join-Path $Runtime "$Label-cloud.err.log") -PassThru
        Wait-Health "http://127.0.0.1:8201/health" $cloud

        $enterpriseArgs = @(
            (Join-Path $Q2 "enterprise_server.py"), "--weights", $Weights, "--tokenizer", $Tokenizer,
            "--cloud-url", "http://127.0.0.1:8201", "--port", "8200",
            "--network-one-way-ms", "2.5", "--bandwidth-gbps", "10",
            "--prefill-chunk-size", $ChunkSize
        )
        if ($Serialize) { $enterpriseArgs += "--serialize-requests" }
        $enterprise = Start-Process -FilePath $Python -ArgumentList $enterpriseArgs -WorkingDirectory $Q2 -WindowStyle Hidden -RedirectStandardOutput (Join-Path $Runtime "$Label-enterprise.log") -RedirectStandardError (Join-Path $Runtime "$Label-enterprise.err.log") -PassThru
        Wait-Health "http://127.0.0.1:8200/health" $enterprise

        & $Python (Join-Path $Root "load_test.py") `
            --enterprise-url "http://127.0.0.1:8200" --cloud-url "http://127.0.0.1:8201" `
            --requests $Requests --concurrency $Concurrency --max-new-tokens $MaxNewTokens `
            --label $Label --output (Join-Path $Results "$Label.json")
        if ($LASTEXITCODE -ne 0) { throw "$Label load test failed." }
    }
    finally {
        if ($enterprise -and -not $enterprise.HasExited) { Stop-Process -Id $enterprise.Id -Force }
        if ($cloud -and -not $cloud.HasExited) { Stop-Process -Id $cloud.Id -Force }
        Start-Sleep -Milliseconds 500
    }
}

Run-Case "serial" 1 0 "fifo" $true 0
Run-Case "pipeline" 1 0 "fifo" $false 0
Run-Case "stage-aware" 1 0 "decode-first" $false 0
Run-Case "chunked-prefill" 1 0 "decode-first" $false $PrefillChunkSize
Run-Case "full-optimized" $Concurrency $BatchWindowMs "decode-first" $false 0
& $Python (Join-Path $Root "summarize.py") `
    --input (Join-Path $Results "serial.json") `
    --input (Join-Path $Results "pipeline.json") `
    --input (Join-Path $Results "stage-aware.json") `
    --input (Join-Path $Results "chunked-prefill.json") `
    --input (Join-Path $Results "full-optimized.json") `
    --output (Join-Path $Results "comparison.json")
if ($LASTEXITCODE -ne 0) { throw "Summary generation failed." }
Write-Host "Results written to $Results"

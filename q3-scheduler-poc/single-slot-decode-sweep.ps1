$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Q2 = Resolve-Path (Join-Path $Root "..\q2-split-inference")
$Python = Join-Path $Q2 ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { & (Join-Path $Q2 "setup.ps1") }
if (-not (Test-Path -LiteralPath (Join-Path $Q2 "assets\model.safetensors"))) { & (Join-Path $Q2 "download-model.ps1") }
$Weights = Resolve-Path (Join-Path $Q2 "assets\model.safetensors")
$Tokenizer = Resolve-Path (Join-Path $Q2 "assets\tokenizer.json")
$Runtime = Join-Path $Root "runtime\single-slot-decode-sweep"
$Results = Join-Path $Root "results"
New-Item -ItemType Directory -Force -Path $Runtime, $Results | Out-Null

function Wait-Health([string]$Url, [System.Diagnostics.Process]$Process) {
    $deadline = (Get-Date).AddMinutes(3)
    do {
        Start-Sleep -Milliseconds 300
        try { $ready = (Invoke-RestMethod -Uri $Url -TimeoutSec 2).status -eq "ok" } catch { $ready = $false }
        if ($Process.HasExited) { throw "Service exited before healthy: $Url" }
    } until ($ready -or (Get-Date) -gt $deadline)
    if (-not $ready) { throw "Timed out: $Url" }
}

function Run-Case([int]$Concurrency) {
    $label = "decode-priority-c$Concurrency"
    $processes = @()
    try {
        $linkUrl = "http://127.0.0.1:8699"
        $link = Start-Process -FilePath $Python -ArgumentList @(
            (Join-Path $Root "shared_link_server.py"), "--port", "8699", "--bandwidth-gbps", "10", "--propagation-ms", "2.5"
        ) -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $Runtime "$label-link.log") -RedirectStandardError (Join-Path $Runtime "$label-link.err.log") -PassThru
        $processes += $link; Wait-Health "$linkUrl/health" $link
        $cloudUrl = "http://127.0.0.1:8701"
        $cloud = Start-Process -FilePath $Python -ArgumentList @(
            (Join-Path $Q2 "cloud_server.py"), "--weights", $Weights, "--port", "8701", "--max-batch-size", "1",
            "--batch-window-ms", "0", "--scheduling-policy", "decode-first", "--torch-threads", "1"
        ) -WorkingDirectory $Q2 -WindowStyle Hidden -RedirectStandardOutput (Join-Path $Runtime "$label-cloud.log") -RedirectStandardError (Join-Path $Runtime "$label-cloud.err.log") -PassThru
        $processes += $cloud; Wait-Health "$cloudUrl/health" $cloud
        $enterpriseUrl = "http://127.0.0.1:8700"
        $enterprise = Start-Process -FilePath $Python -ArgumentList @(
            (Join-Path $Q2 "enterprise_server.py"), "--weights", $Weights, "--tokenizer", $Tokenizer,
            "--cloud-url", $cloudUrl, "--port", "8700", "--network-one-way-ms", "2.5",
            "--bandwidth-gbps", "10", "--shared-link-url", $linkUrl, "--torch-threads", "1"
        ) -WorkingDirectory $Q2 -WindowStyle Hidden -RedirectStandardOutput (Join-Path $Runtime "$label-enterprise.log") -RedirectStandardError (Join-Path $Runtime "$label-enterprise.err.log") -PassThru
        $processes += $enterprise; Wait-Health "$enterpriseUrl/health" $enterprise
        Invoke-RestMethod -Uri "$cloudUrl/metrics/reset" -Method Post -ContentType "application/json" -Body "{}" | Out-Null
        & $Python (Join-Path $Root "load_test.py") --enterprise-url $enterpriseUrl --cloud-url $cloudUrl `
            --requests 32 --concurrency $Concurrency --max-new-tokens 64 --label $label `
            --output (Join-Path $Results "$label.json")
        if ($LASTEXITCODE -ne 0) { throw "$label failed" }
    }
    finally {
        foreach ($process in $processes) { if ($process -and -not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue } }
        Start-Sleep -Milliseconds 500
    }
}

Run-Case 2
Run-Case 3
Run-Case 4
& $Python (Join-Path $Root "summarize_decode_sweep.py") `
    --baseline (Join-Path $Results "single-slot-D-pipeline.json") `
    --input (Join-Path $Results "decode-priority-c2.json") `
    --input (Join-Path $Results "decode-priority-c3.json") `
    --input (Join-Path $Results "decode-priority-c4.json") `
    --output (Join-Path $Results "single-slot-decode-sweep.json")
if ($LASTEXITCODE -ne 0) { throw "Decode sweep summary failed" }

param(
    [int]$Requests = 32,
    [int]$MaxNewTokens = 64
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Q2 = Resolve-Path (Join-Path $Root "..\q2-split-inference")
$Python = Join-Path $Q2 ".venv\Scripts\python.exe"
$Weights = Resolve-Path (Join-Path $Q2 "assets\model.safetensors")
$Tokenizer = Resolve-Path (Join-Path $Q2 "assets\tokenizer.json")
$Runtime = Join-Path $Root "runtime\single-slot-bd"
$Results = Join-Path $Root "results"
New-Item -ItemType Directory -Force -Path $Runtime, $Results | Out-Null

function Wait-Health([string]$Url, [System.Diagnostics.Process]$Process) {
    $deadline = (Get-Date).AddMinutes(3)
    do {
        Start-Sleep -Milliseconds 300
        try { $ready = (Invoke-RestMethod -Uri $Url -TimeoutSec 2).status -eq "ok" } catch { $ready = $false }
        if ($Process.HasExited) { throw "Service exited before healthy: $Url" }
    } until ($ready -or (Get-Date) -gt $deadline)
    if (-not $ready) { throw "Timed out waiting for $Url" }
}

function Run-Case([string]$Label, [bool]$Serialize) {
    $processes = @()
    try {
        $linkUrl = "http://127.0.0.1:8599"
        $link = Start-Process -FilePath $Python -ArgumentList @(
            (Join-Path $Root "shared_link_server.py"), "--port", "8599", "--bandwidth-gbps", "10", "--propagation-ms", "2.5"
        ) -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $Runtime "$Label-link.log") -RedirectStandardError (Join-Path $Runtime "$Label-link.err.log") -PassThru
        $processes += $link
        Wait-Health "$linkUrl/health" $link

        $cloudUrl = "http://127.0.0.1:8601"
        $cloud = Start-Process -FilePath $Python -ArgumentList @(
            (Join-Path $Q2 "cloud_server.py"), "--weights", $Weights, "--port", "8601",
            "--max-batch-size", "1", "--batch-window-ms", "0", "--scheduling-policy", "fifo", "--torch-threads", "1"
        ) -WorkingDirectory $Q2 -WindowStyle Hidden -RedirectStandardOutput (Join-Path $Runtime "$Label-cloud.log") -RedirectStandardError (Join-Path $Runtime "$Label-cloud.err.log") -PassThru
        $processes += $cloud
        Wait-Health "$cloudUrl/health" $cloud

        $enterpriseUrl = "http://127.0.0.1:8600"
        $enterpriseArgs = @(
            (Join-Path $Q2 "enterprise_server.py"), "--weights", $Weights, "--tokenizer", $Tokenizer,
            "--cloud-url", $cloudUrl, "--port", "8600", "--network-one-way-ms", "2.5",
            "--bandwidth-gbps", "10", "--shared-link-url", $linkUrl, "--torch-threads", "1"
        )
        if ($Serialize) { $enterpriseArgs += "--serialize-requests" }
        $enterprise = Start-Process -FilePath $Python -ArgumentList $enterpriseArgs -WorkingDirectory $Q2 -WindowStyle Hidden -RedirectStandardOutput (Join-Path $Runtime "$Label-enterprise.log") -RedirectStandardError (Join-Path $Runtime "$Label-enterprise.err.log") -PassThru
        $processes += $enterprise
        Wait-Health "$enterpriseUrl/health" $enterprise

        Invoke-RestMethod -Uri "$cloudUrl/metrics/reset" -Method Post -ContentType "application/json" -Body "{}" | Out-Null
        Invoke-RestMethod -Uri "$linkUrl/metrics/reset" -Method Post -ContentType "application/json" -Body "{}" | Out-Null
        $raw = Join-Path $Results "$Label-raw.json"
        & $Python (Join-Path $Root "load_test.py") --enterprise-url $enterpriseUrl --cloud-url $cloudUrl `
            --requests $Requests --concurrency $Requests --max-new-tokens $MaxNewTokens --label $Label --output $raw
        if ($LASTEXITCODE -ne 0) { throw "$Label load failed" }
        $doc = Get-Content -LiteralPath $raw -Raw -Encoding UTF8 | ConvertFrom-Json
        $doc | Add-Member -NotePropertyName shared_link -NotePropertyValue (Invoke-RestMethod -Uri "$linkUrl/metrics")
        $doc | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath (Join-Path $Results "$Label.json") -Encoding UTF8
    }
    finally {
        foreach ($process in $processes) {
            if ($process -and -not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
        }
        Start-Sleep -Milliseconds 500
    }
}

Run-Case "single-slot-B-serial" $true
Run-Case "single-slot-D-pipeline" $false
& $Python (Join-Path $Root "summarize_single_slot_bd.py") `
    (Join-Path $Results "single-slot-B-serial.json") `
    (Join-Path $Results "single-slot-D-pipeline.json") `
    (Join-Path $Results "single-slot-BD-comparison.json")
if ($LASTEXITCODE -ne 0) { throw "B/D summary failed" }

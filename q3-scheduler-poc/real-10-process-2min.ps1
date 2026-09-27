param(
    [int]$DurationSeconds = 120,
    [int]$Users = 5,
    [double]$MeanUserIntervalSeconds = 8.0,
    [int]$Seed = 20260925
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Q2 = Resolve-Path (Join-Path $Root "..\q2-split-inference")
$Python = Join-Path $Q2 ".venv\Scripts\python.exe"
$Weights = Resolve-Path (Join-Path $Q2 "assets\model.safetensors")
$Tokenizer = Resolve-Path (Join-Path $Q2 "assets\tokenizer.json")
$Runtime = Join-Path $Root "runtime\real-10-process"
$Results = Join-Path $Root "results"
$Schedule = Join-Path $Results "real-10-process-schedule.json"
$LoadResult = Join-Path $Results "real-10-process-load.json"
$SummaryResult = Join-Path $Results "real-10-process-summary.json"
New-Item -ItemType Directory -Force -Path $Runtime, $Results | Out-Null

& $Python (Join-Path $Root "sustained_load_test.py") --make-schedule --schedule $Schedule `
    --duration $DurationSeconds --users $Users --mean-user-interval $MeanUserIntervalSeconds --seed $Seed
if ($LASTEXITCODE -ne 0) { throw "Schedule generation failed" }

function Wait-Health([string]$Url, [System.Diagnostics.Process]$Process) {
    $deadline = (Get-Date).AddMinutes(4)
    do {
        Start-Sleep -Milliseconds 500
        try { $ready = (Invoke-RestMethod -Uri $Url -TimeoutSec 2).status -eq "ok" } catch { $ready = $false }
        if ($Process.HasExited) { throw "Service $Url exited; inspect $Runtime" }
    } until ($ready -or (Get-Date) -gt $deadline)
    if (-not $ready) { throw "Timed out waiting for $Url" }
}

$processes = @()
$samples = @()
try {
    $linkUrl = "http://127.0.0.1:8399"
    $link = Start-Process -FilePath $Python -ArgumentList @(
        (Join-Path $Root "shared_link_server.py"), "--port", "8399",
        "--bandwidth-gbps", "10", "--propagation-ms", "2.5"
    ) -WorkingDirectory $Root -WindowStyle Hidden `
      -RedirectStandardOutput (Join-Path $Runtime "shared-link.log") `
      -RedirectStandardError (Join-Path $Runtime "shared-link.err.log") -PassThru
    $processes += $link
    Wait-Health "$linkUrl/health" $link
    Write-Host "Ready: shared 10 Gbps duplex link (PID $($link.Id))"

    $cloudUrls = @()
    for ($i = 0; $i -lt 8; $i++) {
        $port = 8401 + $i
        $label = "cloud-$i"
        $process = Start-Process -FilePath $Python -ArgumentList @(
            (Join-Path $Q2 "cloud_server.py"), "--weights", $Weights, "--port", $port,
            "--max-batch-size", "1", "--batch-window-ms", "0", "--scheduling-policy", "fifo",
            "--torch-threads", "1"
        ) -WorkingDirectory $Q2 -WindowStyle Hidden `
          -RedirectStandardOutput (Join-Path $Runtime "$label.log") `
          -RedirectStandardError (Join-Path $Runtime "$label.err.log") -PassThru
        $processes += $process
        $url = "http://127.0.0.1:$port"
        $cloudUrls += $url
        Wait-Health "$url/health" $process
        Write-Host "Ready: $label (PID $($process.Id))"
    }

    $cloudList = $cloudUrls -join ","
    $enterpriseUrls = @()
    for ($i = 0; $i -lt 2; $i++) {
        $port = 8500 + $i
        $label = "enterprise-$i"
        $process = Start-Process -FilePath $Python -ArgumentList @(
            (Join-Path $Q2 "enterprise_server.py"), "--weights", $Weights, "--tokenizer", $Tokenizer,
            "--cloud-url", $cloudList, "--port", $port, "--network-one-way-ms", "2.5",
            "--bandwidth-gbps", "10", "--shared-link-url", $linkUrl, "--torch-threads", "1"
        ) -WorkingDirectory $Q2 -WindowStyle Hidden `
          -RedirectStandardOutput (Join-Path $Runtime "$label.log") `
          -RedirectStandardError (Join-Path $Runtime "$label.err.log") -PassThru
        $processes += $process
        $url = "http://127.0.0.1:$port"
        $enterpriseUrls += $url
        Wait-Health "$url/health" $process
        Write-Host "Ready: $label (PID $($process.Id))"
    }

    foreach ($url in $cloudUrls) { Invoke-RestMethod -Uri "$url/metrics/reset" -Method Post -ContentType "application/json" -Body "{}" | Out-Null }
    Invoke-RestMethod -Uri "$linkUrl/metrics/reset" -Method Post -ContentType "application/json" -Body "{}" | Out-Null
    $loadArgs = @(
        (Join-Path $Root "sustained_load_test.py"), "--schedule", $Schedule,
        "--duration", $DurationSeconds, "--cloud-url", $cloudUrls[0],
        "--label", "real-10-process", "--output", $LoadResult
    )
    foreach ($url in $enterpriseUrls) { $loadArgs += @("--enterprise-url", $url) }
    $load = Start-Process -FilePath $Python -ArgumentList $loadArgs -WorkingDirectory $Root -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $Runtime "load.log") `
        -RedirectStandardError (Join-Path $Runtime "load.err.log") -PassThru
    Write-Host "Two-minute load started (PID $($load.Id))"
    while (-not $load.HasExited) {
        $os = Get-CimInstance Win32_OperatingSystem
        $page = Get-CimInstance Win32_PageFileUsage | Select-Object -First 1
        $pythonMemory = (Get-Process | Where-Object { $_.ProcessName -like "python*" } | Measure-Object WorkingSet64 -Sum).Sum
        $samples += [pscustomobject]@{
            at = (Get-Date).ToString("o")
            free_ram_gb = [math]::Round($os.FreePhysicalMemory / 1MB, 3)
            python_working_set_gb = [math]::Round($pythonMemory / 1GB, 3)
            pagefile_usage_gb = [math]::Round($page.CurrentUsage / 1024, 3)
        }
        Start-Sleep -Seconds 5
        $load.Refresh()
    }
    $loadError = Join-Path $Runtime "load.err.log"
    if (-not (Test-Path -LiteralPath $LoadResult) -or ((Get-Item -LiteralPath $loadError).Length -gt 0)) {
        throw "Load test failed; see $loadError"
    }

    $cloudMetrics = foreach ($url in $cloudUrls) { Invoke-RestMethod -Uri "$url/metrics" -TimeoutSec 10 }
    $linkMetrics = Invoke-RestMethod -Uri "$linkUrl/metrics" -TimeoutSec 10
    $summary = [ordered]@{
        configuration = @{ enterprise_processes = 2; cloud_processes = 8; duration_seconds = $DurationSeconds; users = $Users; per_process_torch_threads = 1; shared_link = "full-duplex 10 Gbps, 2.5 ms one-way" }
        load = Get-Content -LiteralPath $LoadResult -Raw -Encoding UTF8 | ConvertFrom-Json
        cloud_workers = $cloudMetrics
        shared_link = $linkMetrics
        memory_samples = $samples
        memory_summary = @{
            min_free_ram_gb = ($samples.free_ram_gb | Measure-Object -Minimum).Minimum
            max_python_working_set_gb = ($samples.python_working_set_gb | Measure-Object -Maximum).Maximum
            max_pagefile_usage_gb = ($samples.pagefile_usage_gb | Measure-Object -Maximum).Maximum
        }
    }
    $summary | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $SummaryResult -Encoding UTF8
    $summary.memory_summary | Format-List
    $summary.load.client | Format-List
    Write-Host "Result: $SummaryResult"
}
finally {
    foreach ($process in $processes) {
        if ($process -and -not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
    }
}

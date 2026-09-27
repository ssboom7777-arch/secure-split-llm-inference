param(
    [double[]]$Rates = @(0.20, 0.30, 0.40, 0.50),
    [int]$DurationSeconds = 45,
    [int]$MaxNewTokens = 64,
    [double]$TtftSloSeconds = 3.0,
    [double]$TpotSloSeconds = 0.1
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Q2 = Resolve-Path (Join-Path $Root "..\q2-split-inference")
$Q3 = Resolve-Path (Join-Path $Root "..\q3-scheduler-poc")
$Python = Join-Path $Q2 ".venv\Scripts\python.exe"
$Weights = Resolve-Path (Join-Path $Q2 "assets\model.safetensors")
$Tokenizer = Resolve-Path (Join-Path $Q2 "assets\tokenizer.json")
$Results = Join-Path $Root "results\open-loop-validation"
$Runtime = Join-Path $Root "runtime\open-loop-validation"
New-Item -ItemType Directory -Force -Path $Results, $Runtime | Out-Null

function Wait-Health([string]$Url, [System.Diagnostics.Process]$Process) {
    $deadline = (Get-Date).AddMinutes(3)
    do {
        Start-Sleep -Milliseconds 300
        try { $ready = (Invoke-RestMethod -Uri $Url -TimeoutSec 2).status -eq "ok" } catch { $ready = $false }
        if ($Process.HasExited) { throw "Service exited before healthy: $Url" }
    } until ($ready -or (Get-Date) -gt $deadline)
    if (-not $ready) { throw "Timed out waiting for $Url" }
}

$resultFiles = @()
foreach ($rate in $Rates) {
    $label = "qps-$($rate.ToString('0.00', [Globalization.CultureInfo]::InvariantCulture))"
    $processes = @()
    try {
        $linkUrl = "http://127.0.0.1:8799"
        $link = Start-Process -FilePath $Python -ArgumentList @(
            (Join-Path $Q3 "shared_link_server.py"), "--port", "8799", "--bandwidth-gbps", "10", "--propagation-ms", "2.5"
        ) -WorkingDirectory $Q3 -WindowStyle Hidden -RedirectStandardOutput (Join-Path $Runtime "$label-link.log") -RedirectStandardError (Join-Path $Runtime "$label-link.err.log") -PassThru
        $processes += $link; Wait-Health "$linkUrl/health" $link

        $cloudUrl = "http://127.0.0.1:8801"
        $cloud = Start-Process -FilePath $Python -ArgumentList @(
            (Join-Path $Q2 "cloud_server.py"), "--weights", $Weights, "--port", "8801",
            "--max-batch-size", "1", "--batch-window-ms", "0", "--scheduling-policy", "decode-first", "--torch-threads", "1"
        ) -WorkingDirectory $Q2 -WindowStyle Hidden -RedirectStandardOutput (Join-Path $Runtime "$label-cloud.log") -RedirectStandardError (Join-Path $Runtime "$label-cloud.err.log") -PassThru
        $processes += $cloud; Wait-Health "$cloudUrl/health" $cloud

        $enterpriseUrl = "http://127.0.0.1:8800"
        $enterprise = Start-Process -FilePath $Python -ArgumentList @(
            (Join-Path $Q2 "enterprise_server.py"), "--weights", $Weights, "--tokenizer", $Tokenizer,
            "--cloud-url", $cloudUrl, "--port", "8800", "--network-one-way-ms", "2.5",
            "--bandwidth-gbps", "10", "--shared-link-url", $linkUrl, "--torch-threads", "1"
        ) -WorkingDirectory $Q2 -WindowStyle Hidden -RedirectStandardOutput (Join-Path $Runtime "$label-enterprise.log") -RedirectStandardError (Join-Path $Runtime "$label-enterprise.err.log") -PassThru
        $processes += $enterprise; Wait-Health "$enterpriseUrl/health" $enterprise

        $output = Join-Path $Results "$label.json"
        & $Python (Join-Path $Root "validate_open_loop.py") --enterprise-url $enterpriseUrl --cloud-url $cloudUrl `
            --offered-qps $rate --duration $DurationSeconds --max-new-tokens $MaxNewTokens `
            --ttft-slo $TtftSloSeconds --tpot-slo $TpotSloSeconds --output $output
        if ($LASTEXITCODE -ne 0) { throw "$label validation failed" }
        $resultFiles += $output
    }
    finally {
        foreach ($process in $processes) {
            if ($process -and -not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
        }
        Start-Sleep -Milliseconds 500
    }
}

$summaryArgs = @((Join-Path $Root "summarize_open_loop.py"))
foreach ($file in $resultFiles) { $summaryArgs += @("--input", $file) }
$summaryArgs += @("--output", (Join-Path $Root "results\q2-q3-open-loop-validation.json"))
& $Python @summaryArgs
if ($LASTEXITCODE -ne 0) { throw "Validation summary failed" }

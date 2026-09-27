$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidFile = Join-Path $Root "runtime\pids.json"
if (-not (Test-Path -LiteralPath $PidFile)) { Write-Host "No PID file."; exit 0 }
$Ids = Get-Content -LiteralPath $PidFile -Encoding UTF8 -Raw | ConvertFrom-Json
foreach ($IdValue in @($Ids.enterprise, $Ids.cloud)) {
    $Process = Get-Process -Id $IdValue -ErrorAction SilentlyContinue
    if ($Process) { Stop-Process -Id $IdValue; Write-Host "Stopped PID $IdValue" }
}

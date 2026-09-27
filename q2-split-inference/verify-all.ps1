$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
& (Join-Path $Root "run.ps1")
& (Join-Path $Root "verify.ps1")
& (Join-Path $Root "verify-http.ps1")
Write-Host "All Q2 checks passed. Services remain running for interactive questions."
Write-Host "Stop them with: powershell -ExecutionPolicy Bypass -File .\q2-split-inference\stop.ps1"

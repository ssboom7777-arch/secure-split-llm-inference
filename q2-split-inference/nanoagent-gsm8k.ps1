param([int]$Samples = 50, [int]$MaxTokens = 128)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Workspace = Resolve-Path (Join-Path $Root "..\..")
$Bun = Join-Path $Workspace ".tools\bun\bun-windows-x64\bun.exe"
& (Join-Path $Workspace "nanoagent\local-demo\start-model.ps1")
if ($LASTEXITCODE -ne 0) { throw "Qwen3 server failed to start" }
$env:GSM8K_ROOT = $Root -replace '\\', '/'
$env:GSM8K_SAMPLES = "$Samples"
$env:GSM8K_MAX_TOKENS = "$MaxTokens"
Push-Location (Join-Path $Workspace "nanoagent")
try {
  & $Bun run .\examples\gsm8k-eval.ts
  if ($LASTEXITCODE -ne 0) { throw "NanoAgent GSM8K evaluation failed" }
} finally {
  Pop-Location
}

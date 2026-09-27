$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root "q2-split-inference\.venv\Scripts\python.exe"
$Destination = Join-Path $Root "assets\qwen3-1.7b"
$Revision = "70d244cc86ccca08cf5af4e1e306ecf908b1ad5e"
$BaseUrl = "https://hf-mirror.com/Qwen/Qwen3-1.7B/resolve/$Revision"
$Files = @(
    "LICENSE", "README.md", "config.json", "generation_config.json",
    "tokenizer.json", "tokenizer_config.json", "vocab.json", "merges.txt",
    "model.safetensors.index.json", "model-00001-of-00002.safetensors",
    "model-00002-of-00002.safetensors"
)

New-Item -ItemType Directory -Force -Path $Destination | Out-Null
foreach ($File in $Files) {
    $Target = Join-Path $Destination $File
    if (-not (Test-Path -LiteralPath $Target)) {
        Write-Host "Downloading Qwen3-1.7B/$File ..."
        & curl.exe -L --fail --retry 5 --retry-delay 2 -o $Target "$BaseUrl/$File"
        if ($LASTEXITCODE -ne 0) { throw "Qwen3 download failed: $File" }
    }
}

if (-not (Test-Path -LiteralPath $Python)) {
    & (Join-Path $Root "q2-split-inference\setup.ps1")
    if ($LASTEXITCODE -ne 0) { throw "Python runtime setup failed" }
}
& $Python (Join-Path $Root "verify_qwen_download.py") `
    $Destination (Join-Path $Root "assets\qwen3-1.7b.manifest.json")
if ($LASTEXITCODE -ne 0) { throw "Qwen3 checksum verification failed" }

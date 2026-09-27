$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root "q2-split-inference\.venv\Scripts\python.exe"
$Destination = Join-Path $Root "assets\qwen3-1.7b"
$Revision = "70d244cc86ccca08cf5af4e1e306ecf908b1ad5e"

if (-not (Test-Path -LiteralPath $Python)) {
    & (Join-Path $Root "q2-split-inference\setup.ps1")
    if ($LASTEXITCODE -ne 0) { throw "Python runtime setup failed" }
}

New-Item -ItemType Directory -Force -Path $Destination | Out-Null
& $Python -c @"
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id='Qwen/Qwen3-1.7B',
    revision='$Revision',
    local_dir=r'$Destination',
    allow_patterns=[
        'LICENSE', 'README.md', 'config.json', 'generation_config.json',
        'tokenizer.json', 'tokenizer_config.json', 'vocab.json', 'merges.txt',
        'model.safetensors.index.json', 'model-*.safetensors'
    ],
)
"@
if ($LASTEXITCODE -ne 0) { throw "Qwen3 download failed" }

& $Python (Join-Path $Root "verify_qwen_download.py") $Destination $Revision
if ($LASTEXITCODE -ne 0) { throw "Manifest generation failed" }

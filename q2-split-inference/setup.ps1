$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Venv = Join-Path $Root ".venv"
$Python = Join-Path $Venv "Scripts\python.exe"
$WheelDir = Join-Path $Root "wheels"
New-Item -ItemType Directory -Force -Path $WheelDir | Out-Null

if (-not (Test-Path -LiteralPath $Python)) {
    & py -3.11 -m venv --system-site-packages $Venv
    if ($LASTEXITCODE -ne 0) { throw "Python 3.11 is required" }
}

function Get-PypiWheel {
    param([string]$Package, [string]$Version, [string]$Filename)
    $Destination = Join-Path $WheelDir $Filename
    if (Test-Path -LiteralPath $Destination) { return $Destination }
    $Metadata = Invoke-RestMethod -Uri "https://pypi.org/pypi/$Package/$Version/json"
    $Entry = $Metadata.urls | Where-Object { $_.filename -ceq $Filename } | Select-Object -First 1
    if (-not $Entry) { throw "Wheel not found: $Filename" }
    $Url = $Entry.url -replace '^https://files.pythonhosted.org/packages/', 'https://mirrors.aliyun.com/pypi/packages/'
    & curl.exe -L --fail --retry 3 -sS -o $Destination $Url
    if ($LASTEXITCODE -ne 0) { throw "Download failed: $Filename" }
    return $Destination
}

$TorchWheel = Join-Path $WheelDir "torch-2.6.0+cpu-cp311-cp311-win_amd64.whl"
if (-not (Test-Path -LiteralPath $TorchWheel)) {
    & curl.exe -L --fail --retry 3 -o $TorchWheel "https://download.pytorch.org/whl/cpu/torch-2.6.0%2Bcpu-cp311-cp311-win_amd64.whl"
    if ($LASTEXITCODE -ne 0) { throw "PyTorch download failed" }
}

$Wheels = @(
    $TorchWheel,
    (Get-PypiWheel tokenizers 0.22.1 "tokenizers-0.22.1-cp39-abi3-win_amd64.whl"),
    (Get-PypiWheel numpy 2.2.6 "numpy-2.2.6-cp311-cp311-win_amd64.whl"),
    (Get-PypiWheel typing_extensions 4.15.0 "typing_extensions-4.15.0-py3-none-any.whl"),
    (Get-PypiWheel filelock 3.19.1 "filelock-3.19.1-py3-none-any.whl"),
    (Get-PypiWheel fsspec 2025.9.0 "fsspec-2025.9.0-py3-none-any.whl"),
    (Get-PypiWheel networkx 3.4.2 "networkx-3.4.2-py3-none-any.whl"),
    (Get-PypiWheel jinja2 3.1.6 "jinja2-3.1.6-py3-none-any.whl"),
    (Get-PypiWheel MarkupSafe 3.0.2 "MarkupSafe-3.0.2-cp311-cp311-win_amd64.whl"),
    (Get-PypiWheel sympy 1.13.1 "sympy-1.13.1-py3-none-any.whl"),
    (Get-PypiWheel mpmath 1.3.0 "mpmath-1.3.0-py3-none-any.whl")
)

& $Python -m pip install --disable-pip-version-check --no-index --no-deps @Wheels
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed" }
& $Python -c "import torch, tokenizers, numpy; print('runtime ready:', torch.__version__)"
if ($LASTEXITCODE -ne 0) { throw "Runtime validation failed" }

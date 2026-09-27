$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"

$PocRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Weights = Join-Path $PocRoot "..\q2-split-inference\assets\model.safetensors"
$Tokenizer = Join-Path $PocRoot "..\q2-split-inference\assets\tokenizer.json"
$Results = Join-Path $PocRoot "results\results.json"

if (-not (Test-Path -LiteralPath $Tokenizer)) {
    & (Join-Path $PocRoot "..\q2-split-inference\download-model.ps1")
    if ($LASTEXITCODE -ne 0) { throw "MiniMind tokenizer download failed" }
}

if (-not (Test-Path -LiteralPath $Weights)) {
    & (Join-Path $PocRoot "..\q2-split-inference\download-model.ps1")
    if ($LASTEXITCODE -ne 0) { throw "MiniMind model download failed" }
}

$PythonCommand = (Get-Command python -ErrorAction Stop).Source
$PythonPrefix = @()
& $PythonCommand -c "import numpy" 2>$null
if ($LASTEXITCODE -ne 0) {
    $Launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($Launcher) {
        & $Launcher.Source -3.10 -c "import numpy" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $PythonCommand = $Launcher.Source
            $PythonPrefix = @("-3.10")
        }
    }
}
if ($LASTEXITCODE -ne 0) {
    throw "NumPy is required. Install it with: python -m pip install numpy"
}

& $PythonCommand @PythonPrefix (Join-Path $PocRoot "q1_hidden_state_attack.py") `
    --weights $Weights `
    --tokenizer $Tokenizer `
    --output $Results `
    @args
if ($LASTEXITCODE -ne 0) { throw "PoC failed" }

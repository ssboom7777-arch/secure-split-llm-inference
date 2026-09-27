$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Destination = Join-Path $Root "assets\model.safetensors"
$Tokenizer = Join-Path $Root "assets\tokenizer.json"
$ExpectedSha256 = "3ADF69402B5D22E693151CABADC12528F923C4BA6BF343738AAF13F0892162E8"
$TokenizerSha256 = "8BF5868ABFC7EA919186B57E2B411ADEFE3B0922B53052C5839937E166B9D395"
$Url = "https://hf-mirror.com/jingyaogong/minimind-3/resolve/main/model.safetensors"
$TokenizerUrl = "https://hf-mirror.com/jingyaogong/minimind-3/resolve/main/tokenizer.json"

if (-not (Test-Path -LiteralPath $Destination)) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Destination) | Out-Null
    Write-Host "Downloading MiniMind-3 weights (about 122 MiB)..."
    & curl.exe -L --fail --retry 3 -o $Destination $Url
    if ($LASTEXITCODE -ne 0) { throw "MiniMind model download failed" }
}
$Actual = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash
if ($Actual -ne $ExpectedSha256) {
    throw "MiniMind weight checksum mismatch: $Actual"
}
Write-Host "MiniMind weights ready: $Destination"
if (-not (Test-Path -LiteralPath $Tokenizer)) {
    Write-Host "Downloading MiniMind tokenizer..."
    & curl.exe -L --fail --retry 3 -o $Tokenizer $TokenizerUrl
    if ($LASTEXITCODE -ne 0) { throw "MiniMind tokenizer download failed" }
}
$TokenizerActual = (Get-FileHash -LiteralPath $Tokenizer -Algorithm SHA256).Hash
if ($TokenizerActual -ne $TokenizerSha256) {
    throw "MiniMind tokenizer checksum mismatch: $TokenizerActual"
}
Write-Host "MiniMind tokenizer ready: $Tokenizer"

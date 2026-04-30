$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

if (-not $env:GATEWAY_PORT) {
    $env:GATEWAY_PORT = "8767"
}

Write-Host "Starting Anthropic-to-OpenAI gateway on http://127.0.0.1:$env:GATEWAY_PORT"
if ($env:OPENAI_BASE_URL) {
    Write-Host "Upstream: $env:OPENAI_BASE_URL"
} else {
    Write-Host "Upstream: https://api.openai.com/v1"
}
if ($env:OPENAI_MODEL) {
    Write-Host "Model override: $env:OPENAI_MODEL"
} else {
    Write-Host "Model: resolved dynamically from model-map.json"
}

python .\anthropic_openai_gateway.py

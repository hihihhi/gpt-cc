param(
    [string]$Prompt,
    [string]$Model = "claude-sonnet-4-6"
)

$ErrorActionPreference = "Stop"

if (-not $env:GATEWAY_PORT) {
    $env:GATEWAY_PORT = "8767"
}

$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:$env:GATEWAY_PORT"
$env:ANTHROPIC_AUTH_TOKEN = "dummy"
$env:ANTHROPIC_API_KEY = "dummy"

if ($Prompt) {
    claude --model $Model -p $Prompt
} else {
    claude --model $Model
}

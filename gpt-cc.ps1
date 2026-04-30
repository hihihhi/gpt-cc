param(
    [Parameter(Position = 0)]
    [string]$Command = "help",

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$RealClaude = Join-Path $env:USERPROFILE ".local\bin\claude.exe"
if (-not (Test-Path $RealClaude)) {
    $RealClaudeCommand = Get-Command claude.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($RealClaudeCommand) {
        $RealClaude = $RealClaudeCommand.Source
    }
}

function Show-Help {
    @"
gpt-cc - run Claude Code against an OpenAI-compatible GPT backend

Usage:
  .\gpt-cc.ps1 gateway              Start the local Anthropic-compatible gateway
  .\gpt-cc.ps1 claude [args...]     Run claude with ANTHROPIC_BASE_URL pointed at the gateway
  .\gpt-cc.ps1 exec "prompt"        Run claude -p through the gateway
  .\gpt-cc.ps1 login [args...]      Delegate login to codex login
  .\gpt-cc.ps1 doctor               Check local dependencies and environment
  .\gpt-cc.ps1 health               Query the running gateway
  .\gpt-cc.ps1 models               Show configured model routing

Key env vars:
  OPENAI_API_KEY                    Required for api.openai.com
  OPENAI_BASE_URL                   Default: https://api.openai.com/v1
  OPENAI_MODEL                      Optional exact upstream override
  GPT_CC_MODEL_MAP                  Optional path to model-map.json
  GATEWAY_PORT                      Default: 8767
"@
}

function Set-ClaudeGatewayEnv {
    if (-not $env:GATEWAY_PORT) {
        $env:GATEWAY_PORT = "8767"
    }
    $env:ANTHROPIC_BASE_URL = "http://127.0.0.1:$env:GATEWAY_PORT"
    $env:ANTHROPIC_AUTH_TOKEN = "dummy"
    $env:ANTHROPIC_API_KEY = "dummy"
}

function Test-Command($Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

switch ($Command.ToLowerInvariant()) {
    "gateway" {
        Set-Location $Root
        python .\anthropic_openai_gateway.py
    }
    "claude" {
        Set-ClaudeGatewayEnv
        & $RealClaude @Rest
    }
    "exec" {
        Set-ClaudeGatewayEnv
        & $RealClaude -p @Rest
    }
    "login" {
        & codex login @Rest
        Write-Host ""
        Write-Host "gpt-cc uses OPENAI_API_KEY or OPENAI_BASE_URL for the gateway."
        Write-Host "Codex login is delegated as requested, but it does not export an OpenAI API key."
    }
    "doctor" {
        $checks = [ordered]@{
            python = Test-Command "python"
            claude = Test-Command "claude"
            codex = Test-Command "codex"
            gh = Test-Command "gh"
            real_claude = $RealClaude
            openai_api_key = [bool]$env:OPENAI_API_KEY
            openai_base_url = if ($env:OPENAI_BASE_URL) { $env:OPENAI_BASE_URL } else { "https://api.openai.com/v1" }
            model_map = if ($env:GPT_CC_MODEL_MAP) { $env:GPT_CC_MODEL_MAP } else { Join-Path $Root "model-map.json" }
        }
        [pscustomobject]$checks | Format-List
    }
    "health" {
        if (-not $env:GATEWAY_PORT) {
            $env:GATEWAY_PORT = "8767"
        }
        Invoke-RestMethod "http://127.0.0.1:$env:GATEWAY_PORT/health"
    }
    "models" {
        Get-Content (Join-Path $Root "model-map.json")
    }
    "help" {
        Show-Help
    }
    default {
        Write-Error "Unknown gpt-cc command '$Command'. Run .\gpt-cc.ps1 help."
    }
}

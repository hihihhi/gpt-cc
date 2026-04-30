$ErrorActionPreference = "Stop"
$ScriptArgs = @($args)
$Command = if ($ScriptArgs.Count -gt 0) { [string]$ScriptArgs[0] } else { "help" }
$Rest = if ($ScriptArgs.Count -gt 1) { @($ScriptArgs | Select-Object -Skip 1) } else { @() }

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
gpt-cc - run Claude Code against GPT through an OpenAI API or Codex CLI backend

Usage:
  .\gpt-cc.ps1 gateway              Start the local Anthropic-compatible gateway
  .\gpt-cc.ps1 claude [args...]     Run claude with ANTHROPIC_BASE_URL pointed at the gateway
  .\gpt-cc.ps1 exec "prompt"        Run claude -p through the gateway
  .\gpt-cc.ps1 login [args...]      Delegate login to codex login
  .\gpt-cc.ps1 doctor               Check local dependencies and environment
  .\gpt-cc.ps1 health               Query the running gateway
  .\gpt-cc.ps1 models               Show configured model routing

Key env vars:
  GPT_CC_BACKEND                    auto, openai, or codex. Default: auto
  OPENAI_API_KEY                    Enables the OpenAI-compatible API backend
  OPENAI_BASE_URL                   Enables a custom OpenAI-compatible API backend
  OPENAI_MODEL                      Optional exact upstream API model override
  GPT_CC_CODEX_MODEL                Optional Codex CLI model override
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
    Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
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
        Write-Host "gpt-cc can now use this Codex login through the Codex CLI backend."
        Write-Host "If OPENAI_API_KEY or OPENAI_BASE_URL is set, auto mode uses the OpenAI-compatible API backend instead."
    }
    "doctor" {
        $checks = [ordered]@{
            python = Test-Command "python"
            claude = Test-Command "claude"
            codex = Test-Command "codex"
            gh = Test-Command "gh"
            real_claude = $RealClaude
            backend = if ($env:GPT_CC_BACKEND) { $env:GPT_CC_BACKEND } else { "auto" }
            openai_api_key = [bool]$env:OPENAI_API_KEY
            openai_base_url = if ($env:OPENAI_BASE_URL) { $env:OPENAI_BASE_URL } else { "(unset; auto uses codex backend)" }
            codex_model = if ($env:GPT_CC_CODEX_MODEL) { $env:GPT_CC_CODEX_MODEL } else { "(codex default)" }
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

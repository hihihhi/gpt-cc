param(
    [string]$ProfilePath = $PROFILE.CurrentUserAllHosts
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$markerStart = "# >>> gpt-cc wrapper >>>"
$markerEnd = "# <<< gpt-cc wrapper <<<"

$block = @"
$markerStart
`$script:GptCcRoot = "$Root"
`$script:RealClaudeExe = Join-Path `$env:USERPROFILE ".local\bin\claude.exe"

function Test-GptCcGateway {
    if (-not `$env:GATEWAY_PORT) {
        `$env:GATEWAY_PORT = "8767"
    }
    `$client = [System.Net.Sockets.TcpClient]::new()
    try {
        `$async = `$client.BeginConnect("127.0.0.1", [int]`$env:GATEWAY_PORT, `$null, `$null)
        if (-not `$async.AsyncWaitHandle.WaitOne(250, `$false)) {
            return `$false
        }
        `$client.EndConnect(`$async)
        `$health = Invoke-RestMethod -Uri "http://127.0.0.1:`$env:GATEWAY_PORT/health" -TimeoutSec 2
        return `$health.status -eq "ok"
    } catch {
        return `$false
    } finally {
        `$client.Close()
    }
}

function Start-GptCcGateway {
    if (Test-GptCcGateway) {
        return
    }

    if (-not `$env:OPENAI_API_KEY -and -not `$env:OPENAI_BASE_URL) {
        Write-Warning "gpt-cc is starting, but no OPENAI_API_KEY or OPENAI_BASE_URL is set. Codex login/subscription is not an API endpoint, so model calls will fail until you configure a backend."
    }

    `$stdoutLog = Join-Path `$script:GptCcRoot "gpt-cc-gateway.out.log"
    `$stderrLog = Join-Path `$script:GptCcRoot "gpt-cc-gateway.err.log"
    Start-Process -FilePath "python" ``
        -ArgumentList ".\anthropic_openai_gateway.py" ``
        -WorkingDirectory `$script:GptCcRoot ``
        -RedirectStandardOutput `$stdoutLog ``
        -RedirectStandardError `$stderrLog ``
        -WindowStyle Hidden | Out-Null

    for (`$i = 0; `$i -lt 40; `$i++) {
        Start-Sleep -Milliseconds 250
        if (Test-GptCcGateway) {
            return
        }
    }

    throw "gpt-cc gateway did not become healthy. Check `$stdoutLog and `$stderrLog"
}

function gpt-cc-on {
    `$env:GPT_CC_ENABLED = "1"
    Write-Host "gpt-cc enabled: claude routes through `$script:GptCcRoot" -ForegroundColor Green
}

function gpt-cc-off {
    `$env:GPT_CC_ENABLED = "0"
    Write-Host "gpt-cc disabled: claude calls the real Claude Code binary" -ForegroundColor Yellow
}

function gpt-cc-status {
    `$enabled = if (`$env:GPT_CC_ENABLED -eq "0") { "off" } else { "on" }
    `$gateway = if (Test-GptCcGateway) { "running" } else { "stopped" }
    [pscustomobject]@{
        enabled = `$enabled
        gateway = `$gateway
        openai_api_key = [bool]`$env:OPENAI_API_KEY
        openai_base_url = if (`$env:OPENAI_BASE_URL) { `$env:OPENAI_BASE_URL } else { "https://api.openai.com/v1" }
        real_claude = `$script:RealClaudeExe
    }
}

function claude {
    if (`$args.Count -gt 0 -and `$args[0] -eq "--real") {
        & `$script:RealClaudeExe @(`$args | Select-Object -Skip 1)
        return
    }

    if (`$env:GPT_CC_ENABLED -eq "0") {
        & `$script:RealClaudeExe @args
        return
    }

    Start-GptCcGateway
    & (Join-Path `$script:GptCcRoot "gpt-cc.ps1") claude @args
}
$markerEnd
"@

$profileDir = Split-Path -Parent $ProfilePath
if (-not (Test-Path $profileDir)) {
    New-Item -ItemType Directory -Path $profileDir -Force | Out-Null
}

$existing = if (Test-Path $ProfilePath) { Get-Content $ProfilePath -Raw } else { "" }
$pattern = "(?s)" + [regex]::Escape($markerStart) + ".*?" + [regex]::Escape($markerEnd)
if ($existing -match $pattern) {
    $updated = [regex]::Replace($existing, $pattern, [System.Text.RegularExpressions.MatchEvaluator]{ param($m) $block })
} else {
    $updated = $existing.TrimEnd() + "`r`n`r`n" + $block + "`r`n"
}

Set-Content -Path $ProfilePath -Value $updated -Encoding UTF8
Write-Host "Installed gpt-cc wrapper into $ProfilePath" -ForegroundColor Green
Write-Host "Open a new PowerShell window, or run: . `$PROFILE.CurrentUserAllHosts" -ForegroundColor Cyan

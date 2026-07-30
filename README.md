## One sentence

gpt-cc is a compatibility gateway so Claude Code can use OpenAI-compatible models through `gpt-cc` while keeping Claude Code as the outer framework.

## Result

No headline result. gpt-cc is not an official Anthropic, OpenAI, or Codex service. It does not convert a ChatGPT, Codex, or Claude subscription into an API key and it does not read or reuse private session tokens.

## How it works

- `GPT_CC_BACKEND` selects `auto`, `openai`, or `codex`.
- In `auto` mode, `OPENAI_API_KEY` or `OPENAI_BASE_URL` selects API mode.
- Without API configuration, the gateway uses the local `codex exec` flow.
- `model-map.json` controls request compatibility; `ARCHITECTURE.md`, `FEATURES.md`, and `SECURITY.md` document the boundaries.

## The interesting decision

The `codex` path is intentionally a CLI adapter (`codex exec`) instead of scraping web sessions or using unofficial HTTP endpoints. This keeps authentication with the installed CLI and preserves the Claude Code boundary; API mode is the direct model-endpoint path, while Codex mode is a non-streaming fallback.

## Run it

Requirements: Python 3.10+, Claude Code available as `claude`, plus either OpenAI-compatible API config or `codex` installed.

PowerShell setup and wrapper onboarding:

```powershell
cd C:\path\to\gpt-cc
.\install-powershell-wrapper.ps1
```

Start a test session:

```powershell
cd C:\path\to\gpt-cc
.\gpt-cc.ps1 doctor
.\gpt-cc.ps1 gateway
```

In another PowerShell window:

```powershell
cd C:\path\to\repo
C:\path\to\gpt-cc\gpt-cc.ps1 claude
```

Optional command controls:

```powershell
claude                  # uses gateway if wrapper is enabled
claude --real           # force real Claude executable
gpt-cc-off              # disable wrapper for current shell
gpt-cc-on               # re-enable wrapper for current shell
gpt-cc-status           # show current wrapper state
```

Login and explicit backend selection:

```powershell
.\gpt-cc.ps1 login
$env:GPT_CC_BACKEND = "codex"
.\gpt-cc.ps1 gateway
```

macOS/Linux quick path:

```sh
cd ~/gpt-cc
./gpt-cc doctor
./gpt-cc gateway
```

Then:

```sh
cd /path/to/repo
~/gpt-cc/gpt-cc claude
```

One-shot command:

```powershell
.\gpt-cc.ps1 exec "Inspect this repo and summarize it."
```

## Status

Active. Gateway code, wrappers, model routing, and unit tests are in this repo. By default, unknown Anthropic features are blocked (`unsupported_feature`) unless explicitly mapped, and secrets remain environment-driven. Security expectations and allowed operational behavior remain in `SECURITY.md`.

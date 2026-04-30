# gpt-cc

`gpt-cc` is a local compatibility gateway for running Claude Code against GPT
through either an OpenAI-compatible API backend or a logged-in Codex CLI backend.

Claude Code talks to `gpt-cc` as if it were an Anthropic Messages API gateway.
`gpt-cc` translates requests to OpenAI Chat Completions when an API backend is
configured. If no API backend is configured, it can call `codex exec` and adapt
that structured output back into Anthropic-style responses.

This is not official Anthropic or OpenAI functionality. It does not turn a
ChatGPT, Codex, or Claude subscription into an API key; the Codex path shells
out to your locally logged-in `codex` CLI.

Design goal: Claude Code remains the agent framework. `gpt-cc` is only the
model-endpoint adapter. See `ARCHITECTURE.md` for the exact boundary.
See `FEATURES.md` for the current Claude-to-GPT compatibility matrix.

Security policy: see `SECURITY.md`. In short, this repo does not vendor leaked
Claude Code source or copy router projects such as CC-Switch.

## Requirements

- Python 3.10+
- Claude Code CLI installed as `claude`
- Codex CLI installed as `codex` for the no-API-key backend
- Optional: `OPENAI_API_KEY`, or an `OPENAI_BASE_URL` for an OpenAI-compatible
  gateway you control

## Start

```powershell
cd C:\Users\heiwa\gpt-cc
.\gpt-cc.ps1 gateway
```

Then in another PowerShell window:

```powershell
cd C:\path\to\repo
C:\Users\heiwa\gpt-cc\gpt-cc.ps1 claude
```

macOS/Linux:

```sh
cd ~/gpt-cc
./gpt-cc gateway
```

Then:

```sh
cd /path/to/repo
~/gpt-cc/gpt-cc claude
```

## Make `claude` Route Through gpt-cc

PowerShell:

```powershell
cd C:\Users\heiwa\gpt-cc
.\install-powershell-wrapper.ps1
```

Open a new PowerShell window. After that:

```powershell
claude
```

routes through `gpt-cc` by default.

Escape hatches:

```powershell
claude --real     # normal Claude Code binary
gpt-cc-off        # normal Claude for this shell
gpt-cc-on         # re-enable gpt-cc for this shell
gpt-cc-status
```

One-shot:

```powershell
C:\Users\heiwa\gpt-cc\gpt-cc.ps1 exec "Inspect this repo and summarize it."
```

## Login

```powershell
.\gpt-cc.ps1 login
```

This delegates to:

```powershell
codex login
```

That is intentional. `gpt-cc` does not read or reuse private Codex session
tokens. With no `OPENAI_API_KEY` and no `OPENAI_BASE_URL`, gateway auto mode
uses the logged-in `codex exec` command as the backend.

A Codex subscription/login is not an OpenAI API key and is not a reusable HTTP
model endpoint. `gpt-cc` will not scrape Codex or ChatGPT private session
tokens.

This follows Claude Code's documented gateway mode: Claude Code can use an
Anthropic Messages-compatible `ANTHROPIC_BASE_URL`, while
`ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_API_KEY` supplies the request auth header.

## Backends

`GPT_CC_BACKEND` controls provider selection:

| Value | Behavior |
| --- | --- |
| `auto` | Default. Use OpenAI-compatible API if `OPENAI_API_KEY` or `OPENAI_BASE_URL` is configured; otherwise use Codex CLI. |
| `openai` | Force OpenAI-compatible API mode. |
| `codex` | Force Codex CLI mode. |

API mode is the most faithful path because it is a direct model endpoint. Codex
CLI mode is for the no-API-key case: it runs `codex exec --ephemeral --sandbox
read-only --ignore-rules` and asks Codex to return the next Claude Messages
assistant block as structured JSON. It preserves Claude Code as the outer
framework, but it is slower and does not provide true token-by-token streaming.

Useful Codex backend overrides:

```powershell
$env:GPT_CC_BACKEND = "codex"
$env:GPT_CC_CODEX_MODEL = "gpt-5.4"
$env:GPT_CC_CODEX_TIMEOUT_SECONDS = "900"
$env:GPT_CC_CODEX_COMMAND = "codex"
```

## Model Routing

Model routing is data-driven through `model-map.json`.

By default:

- Claude Opus-style model names route to the `flagship` GPT profile.
- Claude Sonnet-style model names route to the `coding` GPT profile.
- Claude Haiku-style model names route to the `mini` GPT profile.

If `OPENAI_MODEL` is set, it wins as an exact override.

If `OPENAI_MODEL` is not set, `gpt-cc` tries to query:

```text
$OPENAI_BASE_URL/models
```

It chooses the newest model matching the selected profile's include/exclude
regexes. If the upstream model list is unavailable, it falls back to the profile
fallback in `model-map.json`.

Useful overrides:

```powershell
$env:OPENAI_MODEL = "gpt-5.4"
$env:OPENAI_BASE_URL = "https://api.openai.com/v1"
$env:GPT_CC_MODEL_MAP = "C:\path\to\model-map.json"
$env:GPT_CC_AUTO_MODELS = "0"
```

## Unsupported Features

Claude Code may add new Anthropic-specific request fields or tool types. `gpt-cc`
fails closed by default:

- Unknown request fields return `unsupported_feature`.
- `context_management.edits` is handled locally for documented context edits:
  `clear_tool_uses_20250919` and `clear_thinking_20251015`.
- `thinking` maps to GPT reasoning controls.
- `output_config.effort` maps to GPT reasoning controls.
- `service_tier` maps through `model-map.json`.
- `container` and `top_k` are accepted as no-op compatibility fields.
- Known Anthropic-only fields like Messages API `mcp_servers` still return
  `unsupported_feature`.
- Unknown/server-side Anthropic tool types return `unsupported_feature`.

To allow unknown request fields while developing a new mapping:

```powershell
$env:GPT_CC_STRICT_UNKNOWN_FIELDS = "0"
```

Do not leave that disabled for shared use unless you are comfortable with silent
feature loss.

## Claude Code Command Behavior

`gpt-cc` does not replace the Claude Code binary. It wraps it with gateway
environment variables:

```powershell
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:8767"
$env:ANTHROPIC_AUTH_TOKEN = "dummy"
```

Command mapping:

| Command | Behavior |
| --- | --- |
| `gpt-cc.ps1 gateway` | Starts the local Anthropic-compatible gateway. |
| `gpt-cc.ps1 claude [args...]` | Runs `claude [args...]` against the gateway. |
| `gpt-cc.ps1 exec "prompt"` | Runs `claude -p "prompt"` against the gateway. |
| `gpt-cc.ps1 login` | Runs `codex login`; does not extract Codex credentials. |
| `gpt-cc.ps1 doctor` | Checks local commands and relevant env vars. |
| `gpt-cc.ps1 health` | Calls the running gateway health endpoint. |
| `gpt-cc.ps1 models` | Prints `model-map.json`. |

Claude-only features:

| Feature | gpt-cc behavior |
| --- | --- |
| Claude model names | Mapped by `model-map.json` or `OPENAI_MODEL`. |
| Claude Code built-in tools | Translated as OpenAI function tools when Claude Code exposes them in the request. |
| Claude in Chrome | No direct GPT equivalent in this gateway. If Claude Code exposes Chrome actions as normal tools, they pass through; otherwise unsupported. |
| Claude server-side tools | Unsupported unless you add an explicit mapping. |
| Top-level Anthropic `thinking` | Mapped to OpenAI `reasoning_effort`; use `OPENAI_REASONING_EFFORT` to force a value or `GPT_CC_REASONING_MODE=off` to disable. |
| `output_config.effort` | Mapped to OpenAI `reasoning_effort`; `max` maps to `xhigh`. |
| `context_management.clear_tool_uses_20250919` | Applied locally before forwarding to GPT. Old tool-use/tool-result pairs are pruned while preserving the most recent configured count. |
| `context_management.clear_thinking_20251015` | Applied locally before forwarding to GPT by removing Anthropic `thinking` content blocks. |
| MCP servers configured inside Claude Code | Not translated by the gateway. If Claude Code exposes a resulting action as a normal function tool, it may work. |

For all other Claude Code commands, use the pass-through form:

```powershell
.\gpt-cc.ps1 claude agents
.\gpt-cc.ps1 claude mcp list
.\gpt-cc.ps1 claude plugin list
.\gpt-cc.ps1 claude --output-format stream-json -p "Summarize this repo"
```

## Environment

```powershell
$env:OPENAI_API_KEY = "sk-..."
$env:OPENAI_BASE_URL = "https://api.openai.com/v1"
$env:OPENAI_REASONING_EFFORT = "high"
$env:OPENAI_MAX_TOKEN_FIELD = "max_completion_tokens"
$env:GPT_CC_BACKEND = "auto"
$env:GPT_CC_CODEX_MODEL = "gpt-5.4"
$env:GATEWAY_PORT = "8767"
```

For older OpenAI-compatible gateways:

```powershell
$env:OPENAI_MAX_TOKEN_FIELD = "max_tokens"
```

## Smoke Tests

```powershell
python -m py_compile .\anthropic_openai_gateway.py
python -m unittest discover -s tests
.\gpt-cc.ps1 gateway
```

With the gateway running:

```powershell
.\gpt-cc.ps1 health
```

## Security Notes

- Do not commit API keys, browser cookies, Claude tokens, Codex tokens, or local
  session files.
- `gpt-cc` does not depend on private ChatGPT web cookies.
- Keep `model-map.json` under version control; keep secrets in environment
  variables or your normal secret manager.

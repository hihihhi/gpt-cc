# Architecture

`gpt-cc` keeps Claude Code as the primary framework.

Claude Code still owns:

- Prompt construction and output style
- Local and project settings
- Permissions
- Hooks
- MCP configuration
- Subagents and custom agents
- Tool schemas and tool execution loop
- Terminal UI and JSON/streaming output modes

`gpt-cc` only owns:

- The local `ANTHROPIC_BASE_URL` endpoint
- Translation from Anthropic Messages requests to OpenAI Chat Completions
- Translation from OpenAI text/tool-call responses back to Anthropic Messages
- Data-driven Claude-model-name to GPT-model-name resolution
- Fail-closed reporting for features with no GPT equivalent

Feature-level compatibility lives in `FEATURES.md`.

## Request Flow

```text
Claude Code
  |
  | Anthropic Messages API
  | POST /v1/messages
  | POST /v1/messages/count_tokens
  v
gpt-cc gateway
  |
  | OpenAI-compatible Chat Completions API
  | POST /chat/completions
  | GET /models
  v
OpenAI-compatible backend
```

Claude Code's own prompts, settings, permissions, agents, and tools are not
reimplemented. They appear in the Anthropic-format request and are translated
only where needed to satisfy the GPT backend.

## Model Resolution

Model resolution is configured in `model-map.json`.

Priority order:

1. `OPENAI_MODEL`, if set.
2. Exact `explicit_model_map` entry.
3. Profile selected by `claude_model_profiles`.
4. Newest matching upstream model from `GET $OPENAI_BASE_URL/models`.
5. Profile fallback from `model-map.json`.

This keeps the update path out of code. When OpenAI-compatible gateways expose
new model IDs in `/models`, `gpt-cc` can pick them without a code change, as
long as they match the configured regex profile.

## Feature Policy

Unknown Claude Code request fields fail with `unsupported_feature` by default.
Known Anthropic-only features also fail with `unsupported_feature` unless an
explicit mapping is added.

This is intentional. A silent partial translation is worse than a clear failure
when the framework has gained a feature the gateway cannot preserve.

The exception is documented context editing. `gpt-cc` applies
`clear_tool_uses_20250919` and `clear_thinking_20251015` locally before
forwarding the request to GPT, then reports `context_management.applied_edits`
in the Anthropic-shaped response. Other context-management edits remain
unsupported until they have an explicit local implementation.

Set this only while developing a new mapping:

```powershell
$env:GPT_CC_STRICT_UNKNOWN_FIELDS = "0"
```

## Commands

`gpt-cc claude [args...]` is the escape hatch for almost every Claude Code
command. It sets gateway environment variables, then forwards arguments to the
real `claude` binary.

Examples:

```powershell
.\gpt-cc.ps1 claude
.\gpt-cc.ps1 claude --output-format stream-json -p "Summarize this repo"
.\gpt-cc.ps1 claude agents
.\gpt-cc.ps1 claude mcp list
.\gpt-cc.ps1 claude plugin list
```

`gpt-cc login` deliberately runs `codex login`. It does not scrape or reuse
Codex credentials. For API calls, configure `OPENAI_API_KEY` or point
`OPENAI_BASE_URL` at a gateway that handles auth.

## No Direct Equivalents

Some Claude Code features do not have model-endpoint equivalents.

| Feature | Behavior |
| --- | --- |
| Claude in Chrome | Passed through only if Claude Code exposes actions as normal tools. There is no GPT model-endpoint replacement here. |
| Anthropic server-side tools | Unsupported until a specific mapping is added. |
| Anthropic thinking blocks | Top-level Anthropic thinking config maps to GPT reasoning effort. `context_management.clear_thinking_20251015` can remove historical thinking blocks locally. |
| Provider-side prompt caching | Not implemented by this gateway. Claude Code may still send cache metadata; unsupported fields fail closed unless explicitly mapped. |
| New Claude Code betas | Unsupported unless represented in `model-map.json` and gateway translation code. |

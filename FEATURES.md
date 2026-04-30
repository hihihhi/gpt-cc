# Feature Compatibility

`gpt-cc` tracks Claude Messages features explicitly. The policy is:

- Preserve directly when OpenAI Chat Completions has an equivalent.
- Approximate only when the behavior is close enough and configurable.
- Apply locally when the feature is request-context editing.
- Fail closed when dropping the feature would silently change behavior or expose
  data.

## Top-Level Request Fields

| Claude field | gpt-cc behavior |
| --- | --- |
| `model` | Resolves through `model-map.json` or `OPENAI_MODEL`. |
| `messages` | Translates Anthropic message/content blocks to Chat Completions messages. |
| `system` | Translates to an OpenAI `system` message. |
| `max_tokens` | Maps to `OPENAI_MAX_TOKEN_FIELD`, default `max_completion_tokens`. |
| `metadata` | Accepted, not forwarded. |
| `stop_sequences` | Maps to OpenAI `stop`. |
| `stream` | Maps streaming SSE in both directions. |
| `temperature` | Forwarded. |
| `tool_choice` | Translated to OpenAI tool choice. |
| `tools` | Client/function tools are translated to OpenAI function tools. Server-side tool types fail closed unless mapped. |
| `top_p` | Forwarded. |
| `top_k` | Accepted as no-op. OpenAI Chat Completions has no equivalent. |
| `thinking` | Maps to OpenAI `reasoning_effort` unless `GPT_CC_REASONING_MODE=off`. |
| `context_management` | Applies documented context edits locally. |
| `service_tier` | Maps Anthropic tiers to OpenAI tiers through `model-map.json`. |
| `container` | Accepted as no-op. No Chat Completions equivalent; Claude Code still owns local state. |
| `mcp_servers` | Fails closed for Messages API server-side MCP. Claude Code local MCP should still work through its normal tool loop. |

## Content Blocks

| Claude content block | GPT/OpenAI equivalent | gpt-cc behavior |
| --- | --- | --- |
| `text` | Chat Completions text content | Preserved. |
| `image` | Chat Completions `image_url` content part | Base64 and URL images are translated. |
| `document` | No Chat Completions equivalent. Responses API has richer file inputs, but that would require a different backend driver. | Replaced with a placeholder. |
| `thinking` | No visible content equivalent. GPT reasoning tokens are not returned as content blocks. | Historical blocks can be removed by context management; otherwise reduced to text only if present in user-visible history. |
| `tool_use` | OpenAI function `tool_calls` | Translated. |
| `tool_result` | OpenAI `tool` role message | Translated. |

## Claude Code vs Codex vs gpt-cc

This table explains when a feature is genuinely not replaceable inside this
gateway.

| Capability | Claude Code / Anthropic side | Codex / OpenAI side | gpt-cc decision |
| --- | --- | --- | --- |
| Local project instructions | `CLAUDE.md`, settings, hooks, skills/plugins loaded by Claude Code before the API call. | Codex uses its own instructions/config such as `AGENTS.md` and `~/.codex/config.toml`. | Not translated by the gateway. Claude Code still loads its own files normally. |
| Local tools | Claude Code executes local Bash/Edit/Read/etc. through its own permission system. | Codex executes tools through Codex’s own sandbox/approval model. | Preserved because Claude Code remains the runtime and sends tool schemas/results through Messages. |
| Local MCP | `claude mcp ...` configures MCP inside Claude Code. | `codex mcp ...` configures MCP inside Codex. | Preserved only when Claude Code exposes the resulting actions as client tools in the Messages loop. |
| Messages API `mcp_servers` | Anthropic server connects directly to remote MCP servers from the API request. | OpenAI has MCP/connectors in other product/API surfaces, but not a Chat Completions field equivalent. | Fails closed. Silently dropping it would pretend remote server tools exist. |
| Server-side Anthropic tools | Examples include Anthropic-managed web/search/code/memory-style tools. The provider runs the tool. | OpenAI has tools such as functions, web search, file search, and computer use, mainly through OpenAI-native APIs/models. | Fails closed unless implemented explicitly. Mapping these would require a tool-execution layer, not just an endpoint adapter. |
| `container` | Anthropic server-side state reuse. | Codex has session/resume; OpenAI APIs have their own stateful surfaces, but Chat Completions has no equivalent field. | Accepted as no-op. Claude Code owns local session state; server-side state cannot be preserved here. |
| `thinking` | Claude-specific extended/adaptive thinking config and possible thinking content blocks. | OpenAI exposes reasoning controls such as `reasoning_effort`; reasoning tokens are not surfaced as content blocks. | Top-level config maps to `reasoning_effort`; thinking content blocks are not recreated. |
| Context editing | Anthropic server edits conversation context before the model sees it. | Codex manages context in its own runtime; OpenAI Chat Completions has no same request field. | Implemented locally for documented edits. |
| `service_tier` | Anthropic accepts `auto` and `standard_only`. | OpenAI Chat Completions accepts service tier values such as `auto`, `default`, `flex`, `priority`. | Mapped through `model-map.json`. |
| `top_k` | Anthropic sampling knob. | No Chat Completions equivalent. | Accepted as no-op because it only affects sampling distribution. |
| Images | Anthropic image blocks. | OpenAI Chat Completions image content parts. | Translated. |
| Documents/files in message payload | Anthropic document blocks and Files API surfaces. | OpenAI has file-capable APIs, but not a drop-in Chat Completions equivalent for every document block. | Placeholder today; future work could add Responses API mode. |

## Thinking Mapping

`thinking` is not forwarded as Anthropic thinking. Instead:

- `{"type":"disabled"}` -> no `reasoning_effort`
- `{"type":"adaptive"}` -> configured `adaptive_effort`, default `high`
- `{"type":"enabled","budget_tokens":...}` -> effort chosen by budget/max-token ratio
- `OPENAI_REASONING_EFFORT` overrides all automatic mapping
- `GPT_CC_REASONING_MODE=off` disables automatic mapping

## Context Management

Implemented locally:

- `clear_tool_uses_20250919`
- `clear_thinking_20251015`

Unknown context edits fail closed until implemented.

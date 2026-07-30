# gpt-cc

A local Python gateway that presents an Anthropic Messages-compatible endpoint to Claude Code and translates supported requests to either an OpenAI Chat Completions-compatible API or a local Codex CLI invocation.

## Status & honesty

**Status:** active compatibility harness. **Result:** No headline performance result; the deliverable is the harness. It has 29 offline unit tests covering request mapping, model selection, tool/message translation, context edits, and two boundary cases. This is not official Anthropic, OpenAI, Claude Code, or Codex software; it does not create an API key or reuse private session credentials.

## Architecture

- Claude Code remains the outer runtime: it owns its prompts, settings, permissions, hooks, local MCP configuration, agents, and tool loop.
- `gpt-cc` accepts only the local Messages-compatible boundary (`POST /v1/messages` and `POST /v1/messages/count_tokens`) and resolves model names through `model-map.json` or `OPENAI_MODEL`.
- In API mode, it maps supported Anthropic message/content blocks, function tools, streaming SSE, selected reasoning controls, and documented context edits to an OpenAI Chat Completions-compatible endpoint.
- In Codex CLI mode, it invokes `codex exec --ephemeral --ignore-rules --sandbox read-only`, requests structured output, and maps returned text or tool-use blocks back to Messages-shaped responses.
- Unknown request fields, unsupported server-side tools, and unknown context edits fail closed rather than being silently dropped. `top_k` and `container` are accepted as no-ops; document blocks become a placeholder; thinking content is not recreated as visible reasoning.

```mermaid
flowchart LR
    C[Claude Code] -->|Anthropic Messages-compatible HTTP| G[gpt-cc: local gateway]
    G -->|OpenAI Chat Completions-compatible HTTP| A[Configured API backend]
    G -->|codex exec structured output| X[Local Codex CLI]
    A --> G
    X --> G
    G -->|Messages-shaped response| C
```

## The interesting decision

The gateway is deliberately an endpoint adapter, not a reimplementation of either client runtime. Claude-specific features without a reliable Chat Completions equivalent fail closed, while two documented context edits are applied locally and reported in the response. The tradeoff is lower apparent compatibility in exchange for avoiding silent tool, state, or context loss; API mode is more faithful than the Codex CLI fallback, whose output is pseudo-streamed only after its command completes.

## Provenance

- Protocol boundary: the public Anthropic Messages API request/response shape, used locally for Claude Code's gateway configuration; this repository is not affiliated with Anthropic.
- API backend boundary: an OpenAI Chat Completions-compatible endpoint and its `/models` discovery endpoint; this repository is not affiliated with OpenAI and does not claim an OpenAI API implementation.
- CLI fallback boundary: the locally installed `codex` CLI's `codex exec` command. Authentication is delegated to that CLI; this project neither reads nor exports Codex credentials.
- Compatibility claims and exceptions are implemented in `anthropic_openai_gateway.py`, configured in `model-map.json`, specified in `FEATURES.md`, and tested in `tests/test_gateway.py`. No external router project code is vendored.
- **License:** UNKNOWN. No license file or authorship/provenance record establishes that an MIT grant is appropriate, so no license was added.

## Run it

Requirements: Python 3.11+, a local `claude` CLI for the wrapper path, and either a configured OpenAI-compatible API backend or a locally authenticated `codex` CLI. Keep credentials in environment variables or a local secret manager; do not put them in this repository.

```powershell
# Windows PowerShell
cd C:\path\to\gpt-cc
python -m unittest discover -s tests -v
.\gpt-cc.ps1 doctor
.\gpt-cc.ps1 gateway
```

In a second PowerShell window, start Claude Code through the local gateway:

```powershell
cd C:\path\to\your-project
C:\path\to\gpt-cc\gpt-cc.ps1 claude
```

```sh
# macOS/Linux
cd /path/to/gpt-cc
python3 -m unittest discover -s tests -v
./gpt-cc doctor
./gpt-cc gateway
```

```sh
# In a second shell
cd /path/to/your-project
/path/to/gpt-cc/gpt-cc claude
```

For the direct API path, configure `OPENAI_API_KEY` or `OPENAI_BASE_URL` outside the repository and use `GPT_CC_BACKEND=openai` if you need to force it. For the CLI fallback, authenticate with `codex login` and use `GPT_CC_BACKEND=codex` if you need to force it.

## Limitations

- The gateway does not translate Anthropic server-side tools, Messages API `mcp_servers`, provider-side prompt caching, or unimplemented Claude-specific beta fields.
- Local Claude Code MCP can work only when Claude Code exposes the resulting action as a normal client tool; it is not converted into provider-side MCP.
- Base64/URL images and client tool calls are mapped; document blocks are represented by a placeholder. The gateway does not make GPT reasoning tokens visible as Anthropic thinking blocks.
- Model routing depends on `model-map.json`, an exact `OPENAI_MODEL` override, or the configured backend's `/models` response; an unavailable or incompatible model endpoint can prevent resolution.
- This project has no measured latency, throughput, accuracy, cost, or benchmark result in the repository.
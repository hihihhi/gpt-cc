# Security Notes

`gpt-cc` is intentionally dependency-light and does not vendor external router
projects.

## External References

- Do not copy leaked Claude Code source or private/internal Anthropic code.
- Do not import code from CC-Switch, OpenCode, OpenClaw, or other router projects
  without an explicit review of license, network behavior, filesystem access,
  secret handling, install scripts, and update mechanisms.
- GitHub stars are not a trust signal. Treat popular repositories as untrusted
  until reviewed.

The public `farion1231/cc-switch` repository was checked only for metadata and
high-level comparison. No code from it is vendored here.

## Secrets

- Do not commit `OPENAI_API_KEY`, Claude tokens, Codex tokens, ChatGPT cookies,
  browser session cookies, or GitHub tokens.
- `gpt-cc` reads provider credentials from environment variables at runtime.
- Logs must not print credential values. The gateway may report whether a key is
  present, but not the key itself.

## Network Boundary

The gateway binds to `127.0.0.1` by default. Keep it on loopback unless you have
a specific, reviewed reason to expose it to another host.

## Updating Compatibility

When Claude Code sends a new request field or tool type:

1. Prefer official API documentation.
2. Add an explicit entry to `model-map.json`.
3. Implement the minimal local translation.
4. Add tests.
5. Fail closed with `unsupported_feature` if no safe equivalent exists.

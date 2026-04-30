#!/usr/bin/env python3
"""
Minimal Anthropic Messages API facade for Claude Code backed by an
OpenAI-compatible Chat Completions endpoint.

This is intentionally small and dependency-free. It implements the endpoints
Claude Code normally needs from an LLM gateway:

  GET  /health
  GET  /v1/models
  POST /v1/messages
  POST /v1/messages/count_tokens

Set OPENAI_API_KEY for the upstream API, or leave it unset to use `codex exec`
through the logged-in Codex CLI backend.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import uuid
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.parse import urlparse


SCRIPT_DIR = Path(__file__).resolve().parent
HOST = os.environ.get("GATEWAY_HOST", "127.0.0.1")
PORT = int(os.environ.get("GATEWAY_PORT", "8767"))

OPENAI_BASE_URL_CONFIGURED = "OPENAI_BASE_URL" in os.environ
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "")
OPENAI_ORG = os.environ.get("OPENAI_ORG", "")
OPENAI_PROJECT = os.environ.get("OPENAI_PROJECT", "")
OPENAI_REASONING_EFFORT = os.environ.get("OPENAI_REASONING_EFFORT", "")
REASONING_MODE = os.environ.get("GPT_CC_REASONING_MODE", "auto").lower()
MAX_TOKEN_FIELD = os.environ.get("OPENAI_MAX_TOKEN_FIELD", "max_completion_tokens")
REQUEST_TIMEOUT = int(os.environ.get("OPENAI_TIMEOUT_SECONDS", "600"))
CODEX_TIMEOUT = int(os.environ.get("GPT_CC_CODEX_TIMEOUT_SECONDS", "600"))
CODEX_COMMAND = os.environ.get("GPT_CC_CODEX_COMMAND", "codex")
CODEX_MODEL = os.environ.get("GPT_CC_CODEX_MODEL", "")
BACKEND_MODE = os.environ.get("GPT_CC_BACKEND", "auto").lower()
MODEL_MAP_FILE = Path(os.environ.get("GPT_CC_MODEL_MAP", str(SCRIPT_DIR / "model-map.json")))
MODEL_CACHE_SECONDS = int(os.environ.get("GPT_CC_MODEL_CACHE_SECONDS", "300"))
AUTO_MODEL_RESOLUTION = os.environ.get("GPT_CC_AUTO_MODELS", "1").lower() not in {"0", "false", "no"}
STRICT_UNKNOWN_REQUEST_FIELDS = os.environ.get("GPT_CC_STRICT_UNKNOWN_FIELDS", "1").lower() not in {"0", "false", "no"}
DEBUG_IO = os.environ.get("GPT_CC_DEBUG_IO", "0").lower() in {"1", "true", "yes"}
TOOL_RESULT_CLEARED_PLACEHOLDER = "[tool result cleared by gpt-cc context management]"

_MODEL_CACHE: dict[str, Any] = {"loaded_at": 0.0, "models": []}


class GatewayUnsupportedError(Exception):
    pass


class GatewayConfigError(Exception):
    pass


def now_unix() -> int:
    return int(time.time())


def message_id() -> str:
    return "msg_" + uuid.uuid4().hex


def call_id() -> str:
    return "call_" + uuid.uuid4().hex[:24]


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def debug_log(message: str) -> None:
    if DEBUG_IO:
        sys.stderr.write(f"gpt-cc debug: {message}\n")


def active_backend() -> str:
    if BACKEND_MODE in {"openai", "codex"}:
        return BACKEND_MODE
    if OPENAI_API_KEY or OPENAI_BASE_URL_CONFIGURED:
        return "openai"
    return "codex"


def load_model_config() -> dict[str, Any]:
    if not MODEL_MAP_FILE.exists():
        raise GatewayConfigError(f"Model map file not found: {MODEL_MAP_FILE}")
    with MODEL_MAP_FILE.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def regex_any(patterns: list[str], value: str) -> bool:
    return any(re.search(pattern, value, flags=re.IGNORECASE) for pattern in patterns)


def openai_models_url() -> str:
    return f"{OPENAI_BASE_URL}/models"


def fetch_openai_models() -> list[dict[str, Any]]:
    if not AUTO_MODEL_RESOLUTION:
        return []
    if not OPENAI_API_KEY and not OPENAI_BASE_URL.startswith(("http://127.0.0.1", "http://localhost")):
        return []

    now = time.time()
    if now - float(_MODEL_CACHE["loaded_at"]) < MODEL_CACHE_SECONDS:
        return list(_MODEL_CACHE["models"])

    req = urllib.request.Request(openai_models_url(), headers=upstream_headers(), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []

    models = data.get("data") if isinstance(data, dict) else []
    if not isinstance(models, list):
        models = []
    _MODEL_CACHE["loaded_at"] = now
    _MODEL_CACHE["models"] = models
    return list(models)


def model_version_key(model_id: str) -> tuple[int, ...]:
    match = re.search(r"gpt-(\d+(?:\.\d+)*)", model_id, flags=re.IGNORECASE)
    if not match:
        return (0,)
    parts = tuple(int(part) for part in match.group(1).split("."))
    return parts + (0,) * (4 - len(parts))


def select_model_for_profile(profile_name: str, config: dict[str, Any]) -> str:
    profiles = config.get("profiles") or {}
    profile = profiles.get(profile_name) or profiles.get(config.get("default_profile")) or {}
    fallback = profile.get("fallback")

    candidates: list[dict[str, Any]] = []
    include_regex = profile.get("include_regex") or [r"^gpt-"]
    exclude_regex = profile.get("exclude_regex") or []
    prefer_regex = profile.get("prefer_regex") or []

    for model in fetch_openai_models():
        model_id = model.get("id") if isinstance(model, dict) else None
        if not isinstance(model_id, str):
            continue
        if not regex_any(include_regex, model_id):
            continue
        if exclude_regex and regex_any(exclude_regex, model_id):
            continue
        candidates.append(model)

    if candidates:
        def sort_key(model: dict[str, Any]) -> tuple[Any, ...]:
            model_id = model.get("id", "")
            preference = 0
            for index, pattern in enumerate(prefer_regex):
                if re.search(pattern, model_id, flags=re.IGNORECASE):
                    preference = len(prefer_regex) - index
                    break
            alias_bonus = 0 if re.search(r"-\d{4}-\d{2}-\d{2}$", model_id) else 1
            return (model_version_key(model_id), preference, alias_bonus, int(model.get("created", 0)))

        return max(candidates, key=sort_key)["id"]

    if fallback:
        return str(fallback)
    raise GatewayUnsupportedError(f"No GPT-equivalent model is configured for profile '{profile_name}'.")


def resolve_openai_model(requested_model: str | None) -> str:
    if OPENAI_MODEL:
        return OPENAI_MODEL

    config = load_model_config()
    requested = requested_model or ""
    explicit = (config.get("explicit_model_map") or {}).get(requested)
    if explicit:
        return str(explicit)

    profile_name = config.get("default_profile", "coding")
    for rule in config.get("claude_model_profiles") or []:
        pattern = rule.get("regex") or rule.get("match")
        if pattern and re.search(pattern, requested, flags=re.IGNORECASE):
            profile_name = rule.get("profile") or profile_name
            break
    return select_model_for_profile(profile_name, config)


def advertised_claude_models(config: dict[str, Any]) -> list[str]:
    models = config.get("advertised_claude_models")
    if isinstance(models, list) and all(isinstance(item, str) for item in models):
        return models
    return ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"]


def validate_request(body: dict[str, Any]) -> None:
    config = load_model_config()
    request_fields = config.get("request_fields") or {}
    allowed = set(request_fields.get("allowed") or [])
    ignored = set(request_fields.get("ignored") or [])
    unsupported = set(request_fields.get("unsupported") or [])

    present_unsupported = [name for name in unsupported if body.get(name) not in (None, [], {}, "")]
    if present_unsupported:
        feature = present_unsupported[0]
        raise GatewayUnsupportedError(
            f"Claude Code requested Anthropic feature '{feature}', but gpt-cc has no configured GPT equivalent."
        )

    if STRICT_UNKNOWN_REQUEST_FIELDS and allowed:
        unknown = sorted(set(body.keys()) - allowed - ignored - unsupported)
        if unknown:
            raise GatewayUnsupportedError(
                "Claude Code sent unsupported request field(s): "
                + ", ".join(unknown)
                + ". Add an explicit mapping in model-map.json before using this feature."
            )

    context_management = body.get("context_management")
    if isinstance(context_management, dict):
        edits = context_management.get("edits")
        if isinstance(edits, list):
            supported_edits = set((config.get("context_management") or {}).get("supported_edits") or [])
            for edit in edits:
                if not isinstance(edit, dict):
                    continue
                edit_type = edit.get("type")
                if edit_type and edit_type not in supported_edits:
                    raise GatewayUnsupportedError(
                        f"Claude Code requested context management edit '{edit_type}', but gpt-cc has no local implementation."
                    )

    output_config = body.get("output_config")
    if isinstance(output_config, dict):
        unsupported_output_config = sorted(set(output_config.keys()) - {"effort"})
        if unsupported_output_config:
            raise GatewayUnsupportedError(
                "Claude Code requested output_config field(s) with no configured GPT equivalent: "
                + ", ".join(unsupported_output_config)
            )


def normalize_reasoning_effort(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    lowered = value.lower()
    if lowered in {"disabled", "none", "off"}:
        return None
    if lowered == "max":
        return "xhigh"
    if lowered in {"low", "medium", "high", "xhigh"}:
        return lowered
    raise GatewayUnsupportedError(f"Claude Code requested reasoning effort '{value}', but gpt-cc has no configured GPT mapping.")


def thinking_to_reasoning_effort(body: dict[str, Any]) -> str | None:
    if REASONING_MODE in {"off", "none", "disabled", "drop"}:
        return None
    if OPENAI_REASONING_EFFORT:
        return OPENAI_REASONING_EFFORT

    output_config = body.get("output_config")
    if isinstance(output_config, dict):
        output_effort = normalize_reasoning_effort(output_config.get("effort"))
        if output_effort:
            return output_effort

    thinking = body.get("thinking")
    if not isinstance(thinking, dict):
        return None

    thinking_type = thinking.get("type")
    if thinking_type in {"disabled", "none"}:
        return None

    config = load_model_config()
    thinking_config = config.get("thinking") or {}
    if thinking_config.get("mode", "reasoning_effort") != "reasoning_effort":
        return None

    if thinking_type == "adaptive":
        return str(thinking_config.get("adaptive_effort") or "high")

    budget_tokens = thinking.get("budget_tokens")
    max_tokens = body.get("max_tokens")
    if isinstance(budget_tokens, int) and isinstance(max_tokens, int) and max_tokens > 0:
        ratio = budget_tokens / max_tokens
        for threshold in thinking_config.get("budget_thresholds") or []:
            if not isinstance(threshold, dict):
                continue
            max_ratio = threshold.get("max_ratio")
            if max_ratio is None or ratio <= max_ratio:
                return str(threshold.get("effort") or thinking_config.get("enabled_default_effort") or "high")

    return str(thinking_config.get("enabled_default_effort") or "high")


def map_service_tier(service_tier: Any) -> str | None:
    if not isinstance(service_tier, str) or not service_tier:
        return None
    config = load_model_config()
    service_tier_map = (config.get("service_tier") or {}).get("map") or {}
    mapped = service_tier_map.get(service_tier)
    if mapped:
        return str(mapped)
    raise GatewayUnsupportedError(
        f"Claude Code requested service_tier '{service_tier}', but gpt-cc has no configured OpenAI mapping."
    )


def should_apply_context_edit(edit: dict[str, Any], body: dict[str, Any], tool_use_count: int, default_input_tokens: int | None = None) -> bool:
    trigger = edit.get("trigger") if isinstance(edit.get("trigger"), dict) else {}
    trigger_type = trigger.get("type")
    trigger_value = trigger.get("value")

    if trigger_type == "tool_uses" and isinstance(trigger_value, int):
        return tool_use_count > trigger_value

    if trigger_type == "input_tokens" and isinstance(trigger_value, int):
        return estimate_tokens(body) > trigger_value

    if trigger:
        return False

    if default_input_tokens is None:
        return True
    return estimate_tokens(body) > default_input_tokens


def context_edit_keep_count(edit: dict[str, Any]) -> int:
    keep = edit.get("keep") if isinstance(edit.get("keep"), dict) else {}
    if keep.get("type") == "tool_uses" and isinstance(keep.get("value"), int):
        return max(0, keep["value"])
    if isinstance(edit.get("keep"), int):
        return max(0, edit["keep"])
    return 4


def collect_tool_uses(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    ids: list[str] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for block in content_blocks(message.get("content")):
            if block.get("type") == "tool_use" and block.get("id"):
                ids.append({"id": str(block["id"]), "name": str(block.get("name") or "")})
    return ids


def clear_tool_uses(messages: list[dict[str, Any]], clear_ids: set[str], clear_tool_inputs: bool = False) -> tuple[list[dict[str, Any]], int]:
    cleared = 0
    out: list[dict[str, Any]] = []
    cleared_result_ids: set[str] = set()

    for message in messages:
        if not isinstance(message, dict):
            out.append(message)
            continue

        role = message.get("role")
        blocks = content_blocks(message.get("content"))
        kept_blocks: list[dict[str, Any]] = []

        if role == "assistant":
            for block in blocks:
                if block.get("type") == "tool_use" and str(block.get("id")) in clear_ids:
                    if clear_tool_inputs:
                        continue
                kept_blocks.append(block)
        elif role == "user":
            for block in blocks:
                if block.get("type") == "tool_result":
                    tool_id = str(block.get("tool_use_id") or block.get("id"))
                    if tool_id in clear_ids:
                        cleared_result_ids.add(tool_id)
                        if clear_tool_inputs:
                            continue
                        replacement = dict(block)
                        replacement["content"] = TOOL_RESULT_CLEARED_PLACEHOLDER
                        kept_blocks.append(replacement)
                        continue
                kept_blocks.append(block)
        else:
            kept_blocks = blocks

        if kept_blocks:
            new_message = dict(message)
            new_message["content"] = kept_blocks
            out.append(new_message)

    cleared = len(cleared_result_ids)
    return out, cleared


def thinking_keep_turns(edit: dict[str, Any]) -> int | None:
    keep = edit.get("keep")
    if keep == "all":
        return None
    if isinstance(keep, dict) and keep.get("type") == "thinking_turns" and isinstance(keep.get("value"), int):
        return max(0, keep["value"])
    return 1


def clear_thinking_blocks(messages: list[dict[str, Any]], keep_turns: int | None = 1) -> tuple[list[dict[str, Any]], int]:
    if keep_turns is None:
        return messages, 0

    assistant_thinking_indexes = [
        index
        for index, message in enumerate(messages)
        if isinstance(message, dict)
        and message.get("role") == "assistant"
        and any(block.get("type") == "thinking" for block in content_blocks(message.get("content")))
    ]
    preserve_indexes = set(assistant_thinking_indexes[-keep_turns:] if keep_turns else [])
    cleared = 0
    out: list[dict[str, Any]] = []

    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            out.append(message)
            continue

        blocks = content_blocks(message.get("content"))
        kept_blocks = []
        removed_from_turn = False
        for block in blocks:
            if block.get("type") == "thinking" and index not in preserve_indexes:
                removed_from_turn = True
                continue
            kept_blocks.append(block)

        if removed_from_turn:
            cleared += 1

        if kept_blocks:
            new_message = dict(message)
            new_message["content"] = kept_blocks
            out.append(new_message)

    return out, cleared


def apply_context_management(body: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    context_management = body.get("context_management")
    if not isinstance(context_management, dict):
        return body, []

    edits = context_management.get("edits")
    if not isinstance(edits, list):
        return body, []

    working_body = dict(body)
    working_messages = list(body.get("messages") or [])
    applied_edits: list[dict[str, Any]] = []

    for edit in edits:
        if not isinstance(edit, dict):
            continue

        edit_type = edit.get("type")
        before_tokens = estimate_tokens({**working_body, "messages": working_messages})

        if edit_type == "clear_tool_uses_20250919":
            tool_uses = collect_tool_uses(working_messages)
            if not should_apply_context_edit(edit, {**working_body, "messages": working_messages}, len(tool_uses), default_input_tokens=100000):
                continue

            keep_count = context_edit_keep_count(edit)
            exclude_tools = set(edit.get("exclude_tools") or [])
            clearable_tool_uses = [tool_use for tool_use in tool_uses if tool_use["name"] not in exclude_tools]
            clear_tool_inputs = bool(edit.get("clear_tool_inputs"))
            clear_ids = set(tool_use["id"] for tool_use in (clearable_tool_uses[:-keep_count] if keep_count else clearable_tool_uses))
            if not clear_ids:
                continue

            working_messages, cleared = clear_tool_uses(working_messages, clear_ids, clear_tool_inputs=clear_tool_inputs)
            if cleared:
                after_tokens = estimate_tokens({**working_body, "messages": working_messages})
                min_clear = edit.get("clear_at_least") if isinstance(edit.get("clear_at_least"), dict) else {}
                if min_clear.get("type") == "input_tokens" and isinstance(min_clear.get("value"), int):
                    if max(0, before_tokens - after_tokens) < min_clear["value"]:
                        continue
                applied_edits.append(
                    {
                        "type": "clear_tool_uses_20250919",
                        "cleared_tool_uses": cleared,
                        "cleared_input_tokens": max(0, before_tokens - after_tokens),
                    }
                )
            continue

        if edit_type == "clear_thinking_20251015":
            if not should_apply_context_edit(edit, {**working_body, "messages": working_messages}, len(collect_tool_uses(working_messages))):
                continue

            working_messages, cleared = clear_thinking_blocks(working_messages, keep_turns=thinking_keep_turns(edit))
            if cleared:
                after_tokens = estimate_tokens({**working_body, "messages": working_messages})
                applied_edits.append(
                    {
                        "type": "clear_thinking_20251015",
                        "cleared_thinking_turns": cleared,
                        "cleared_input_tokens": max(0, before_tokens - after_tokens),
                    }
                )

    if applied_edits:
        working_body["messages"] = working_messages
    return working_body, applied_edits


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def content_blocks(content: Any) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [block if isinstance(block, dict) else {"type": "text", "text": as_text(block)} for block in content]
    if isinstance(content, dict):
        return [content]
    return [{"type": "text", "text": as_text(content)}]


def block_to_text(block: dict[str, Any]) -> str:
    block_type = block.get("type")
    if block_type == "text":
        return as_text(block.get("text"))
    if block_type == "image":
        return "[image omitted by gateway]"
    if block_type == "document":
        return "[document omitted by gateway]"
    if block_type == "thinking":
        return as_text(block.get("thinking"))
    if block_type == "tool_result":
        content = block.get("content")
        if isinstance(content, list):
            return "\n".join(part for part in (block_to_text(x) for x in content_blocks(content)) if part)
        return as_text(content)
    return as_text(block)


def block_to_openai_content_part(block: dict[str, Any]) -> dict[str, Any]:
    block_type = block.get("type")
    if block_type == "text":
        return {"type": "text", "text": as_text(block.get("text"))}
    if block_type == "image":
        source = block.get("source") if isinstance(block.get("source"), dict) else {}
        if source.get("type") == "base64" and source.get("media_type") and source.get("data"):
            return {
                "type": "image_url",
                "image_url": {"url": f"data:{source['media_type']};base64,{source['data']}"},
            }
        if source.get("type") == "url" and source.get("url"):
            return {"type": "image_url", "image_url": {"url": source["url"]}}
        return {"type": "text", "text": "[image omitted by gateway: unsupported image source]"}
    return {"type": "text", "text": block_to_text(block)}


def openai_user_content(parts: list[dict[str, Any]]) -> Any:
    if all(part.get("type") == "text" for part in parts):
        return "\n\n".join(part.get("text", "") for part in parts if part.get("text"))
    return parts


def system_to_text(system: Any) -> str:
    if not system:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts: list[str] = []
        for block in system:
            if isinstance(block, dict):
                text = block.get("text") if block.get("type") == "text" else block_to_text(block)
                if text:
                    parts.append(as_text(text))
            else:
                parts.append(as_text(block))
        return "\n\n".join(parts)
    return as_text(system)


def anthropic_messages_to_openai(body: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    system_text = system_to_text(body.get("system"))
    if system_text:
        out.append({"role": "system", "content": system_text})

    for message in body.get("messages") or []:
        role = message.get("role")
        blocks = content_blocks(message.get("content"))

        if role == "assistant":
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for block in blocks:
                if block.get("type") == "tool_use":
                    arguments = block.get("input")
                    if arguments is None:
                        arguments = {}
                    tool_calls.append(
                        {
                            "id": block.get("id") or call_id(),
                            "type": "function",
                            "function": {
                                "name": block.get("name") or "tool",
                                "arguments": json_dumps(arguments),
                            },
                        }
                    )
                else:
                    text = block_to_text(block)
                    if text:
                        text_parts.append(text)

            if not text_parts and not tool_calls:
                continue
            assistant_message: dict[str, Any] = {"role": "assistant", "content": "\n\n".join(text_parts) or None}
            if tool_calls:
                assistant_message["tool_calls"] = tool_calls
            out.append(assistant_message)
            continue

        if role == "user":
            content_parts: list[dict[str, Any]] = []
            for block in blocks:
                if block.get("type") == "tool_result":
                    if content_parts:
                        out.append({"role": "user", "content": openai_user_content(content_parts)})
                        content_parts = []
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id") or block.get("id") or call_id(),
                            "content": block_to_text(block),
                        }
                    )
                else:
                    part = block_to_openai_content_part(block)
                    if part.get("type") != "text" or part.get("text"):
                        content_parts.append(part)
            if content_parts:
                out.append({"role": "user", "content": openai_user_content(content_parts)})
            continue

        if role in {"system", "tool"}:
            out.append({"role": role, "content": "\n\n".join(block_to_text(b) for b in blocks)})

    return out


def anthropic_tools_to_openai(tools: Any) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    if not isinstance(tools, list):
        return converted

    config = load_model_config()
    supported_tool_types = set((config.get("tool_types") or {}).get("supported") or ["custom", "function"])

    for tool in tools:
        if not isinstance(tool, dict):
            continue
        tool_type = tool.get("type", "custom")
        if tool_type not in supported_tool_types:
            raise GatewayUnsupportedError(
                f"Claude Code requested tool type '{tool_type}', but gpt-cc has no configured GPT equivalent."
            )
        name = tool.get("name")
        if not name:
            raise GatewayUnsupportedError("Claude Code sent a tool without a name; cannot map it to an OpenAI function.")
        parameters = tool.get("input_schema") or tool.get("parameters") or {"type": "object", "properties": {}}
        if not isinstance(parameters, dict):
            raise GatewayUnsupportedError(f"Tool '{name}' has a non-object schema; cannot map it to an OpenAI function.")
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description", ""),
                    "parameters": parameters,
                },
            }
        )
    return converted


def anthropic_tool_choice_to_openai(choice: Any) -> Any:
    if not choice:
        return None
    if isinstance(choice, str):
        if choice in {"auto", "none", "required"}:
            return choice
        return {"type": "function", "function": {"name": choice}}
    if isinstance(choice, dict):
        choice_type = choice.get("type")
        if choice_type == "auto":
            return "auto"
        if choice_type == "none":
            return "none"
        if choice_type in {"any", "required"}:
            return "required"
        if choice_type == "tool" and choice.get("name"):
            return {"type": "function", "function": {"name": choice["name"]}}
    return None


def build_openai_payload(body: dict[str, Any]) -> dict[str, Any]:
    resolved_model = resolve_openai_model(body.get("model"))
    payload: dict[str, Any] = {
        "model": resolved_model,
        "messages": anthropic_messages_to_openai(body),
        "stream": bool(body.get("stream")),
    }

    max_tokens = body.get("max_tokens")
    if isinstance(max_tokens, int) and max_tokens > 0:
        payload[MAX_TOKEN_FIELD] = max_tokens

    if "temperature" in body and body.get("temperature") is not None:
        payload["temperature"] = body.get("temperature")

    if "top_p" in body and body.get("top_p") is not None:
        payload["top_p"] = body.get("top_p")

    if body.get("stop_sequences"):
        payload["stop"] = body.get("stop_sequences")

    reasoning_effort = thinking_to_reasoning_effort(body)
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort

    service_tier = map_service_tier(body.get("service_tier"))
    if service_tier:
        payload["service_tier"] = service_tier

    tools = anthropic_tools_to_openai(body.get("tools"))
    if tools:
        payload["tools"] = tools
        tool_choice = anthropic_tool_choice_to_openai(body.get("tool_choice"))
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

    if payload["stream"]:
        payload["stream_options"] = {"include_usage": True}

    return payload


def upstream_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if OPENAI_API_KEY:
        headers["Authorization"] = f"Bearer {OPENAI_API_KEY}"
    if OPENAI_ORG:
        headers["OpenAI-Organization"] = OPENAI_ORG
    if OPENAI_PROJECT:
        headers["OpenAI-Project"] = OPENAI_PROJECT
    return headers


def upstream_request(payload: dict[str, Any]) -> urllib.request.Request:
    url = f"{OPENAI_BASE_URL}/chat/completions"
    return urllib.request.Request(url, data=json_dumps(payload).encode("utf-8"), headers=upstream_headers(), method="POST")


def map_stop_reason(finish_reason: str | None, has_tool_calls: bool = False) -> str:
    if has_tool_calls or finish_reason == "tool_calls":
        return "tool_use"
    if finish_reason == "length":
        return "max_tokens"
    if finish_reason in {"content_filter", "stop", None}:
        return "end_turn"
    return "end_turn"


def safe_json_loads(text: str) -> Any:
    try:
        return json.loads(text) if text else {}
    except json.JSONDecodeError:
        return {}


def openai_response_to_anthropic(data: dict[str, Any], requested_model: str, applied_edits: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = msg.get("content") or ""
    tool_calls = msg.get("tool_calls") or []

    blocks: list[dict[str, Any]] = []
    if isinstance(content, str) and content:
        blocks.append({"type": "text", "text": content})

    for tool_call in tool_calls:
        fn = tool_call.get("function") or {}
        blocks.append(
            {
                "type": "tool_use",
                "id": tool_call.get("id") or call_id(),
                "name": fn.get("name") or "tool",
                "input": safe_json_loads(fn.get("arguments") or "{}"),
            }
        )

    usage = data.get("usage") or {}
    if not blocks:
        blocks.append({"type": "text", "text": ""})

    response = {
        "id": message_id(),
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": blocks,
        "stop_reason": map_stop_reason(choice.get("finish_reason"), bool(tool_calls)),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }
    if applied_edits:
        response["context_management"] = {"applied_edits": applied_edits}
    return response


def codex_prompt(body: dict[str, Any]) -> str:
    request = {
        "system": body.get("system"),
        "messages": body.get("messages") or [],
        "tools": body.get("tools") or [],
        "tool_choice": body.get("tool_choice"),
        "max_tokens": body.get("max_tokens"),
        "temperature": body.get("temperature"),
    }
    return (
        "You are the model backend for Claude Code. Claude Code, not you, owns filesystem edits, shell execution, "
        "MCP, permissions, and the user interface. Your job is only to choose the next assistant content.\n\n"
        "Follow the system and conversation content inside the Claude Messages request. If tools are provided, "
        "request tool calls instead of describing tool results. If no tool is needed, answer normally.\n\n"
        "Return ONLY valid JSON matching this shape:\n"
        "{\n"
        '  "content": [\n'
        '    {"type":"text","text":"...","id":null,"name":null,"input_json":null},\n'
        '    {"type":"tool_use","text":null,"id":null,"name":"tool name from tools","input_json":"{}"}\n'
        "  ],\n"
        '  "stop_reason": "end_turn" | "tool_use"\n'
        "}\n\n"
        "Use a tool_use block only when the provided tools are necessary. Do not claim to have executed tools. "
        "For tool_use, put the tool arguments in input_json as a JSON object string. "
        "Do not inspect files or run commands yourself; Claude Code will do that after you request tools. "
        "Do not include Markdown fences around the JSON.\n\n"
        "Claude Messages request JSON:\n"
        + json.dumps(request, ensure_ascii=False, indent=2)
    )


def codex_response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "content": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "type": {"type": "string", "enum": ["text", "tool_use"]},
                        "text": {"type": ["string", "null"]},
                        "id": {"type": ["string", "null"]},
                        "name": {"type": ["string", "null"]},
                        "input_json": {"type": ["string", "null"]},
                    },
                    "required": ["type", "text", "id", "name", "input_json"],
                },
            },
            "stop_reason": {"type": "string", "enum": ["end_turn", "tool_use", "max_tokens"]},
        },
        "required": ["content", "stop_reason"],
    }


def codex_command_prefix() -> list[str]:
    resolved = shutil.which(CODEX_COMMAND) or CODEX_COMMAND
    suffix = Path(resolved).suffix.lower()
    if suffix == ".ps1":
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", resolved]
    return [resolved]


def run_codex_backend(body: dict[str, Any]) -> dict[str, Any]:
    with NamedTemporaryFile("w", encoding="utf-8", suffix=".schema.json", delete=False) as schema_file:
        json.dump(codex_response_schema(), schema_file)
        schema_path = schema_file.name
    with NamedTemporaryFile("w", encoding="utf-8", suffix=".out.json", delete=False) as output_file:
        output_path = output_file.name

    command = codex_command_prefix() + [
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-rules",
        "--sandbox",
        "read-only",
        "--output-schema",
        schema_path,
        "--output-last-message",
        output_path,
        "--color",
        "never",
    ]
    if CODEX_MODEL:
        command.extend(["--model", CODEX_MODEL])
    command.append("-")

    try:
        proc = subprocess.run(
            command,
            input=codex_prompt(body),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=CODEX_TIMEOUT,
            cwd=os.getcwd(),
        )
        if proc.returncode != 0:
            raise GatewayUnsupportedError(
                "Codex backend failed. stderr: "
                + proc.stderr[-2000:]
                + ("\nstdout: " + proc.stdout[-2000:] if proc.stdout else "")
            )

        with open(output_path, "r", encoding="utf-8") as fh:
            raw = fh.read().strip()
        parsed = safe_json_loads(raw)
        if not isinstance(parsed, dict):
            parsed = {"content": [{"type": "text", "text": raw or proc.stdout.strip()}], "stop_reason": "end_turn"}
        return parsed
    finally:
        for path in (schema_path, output_path):
            try:
                os.unlink(path)
            except OSError:
                pass


def codex_response_to_anthropic(data: dict[str, Any], requested_model: str, applied_edits: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    for block in content_blocks(data.get("content")):
        if block.get("type") == "tool_use":
            parsed_input = block.get("input") if isinstance(block.get("input"), dict) else None
            if parsed_input is None and isinstance(block.get("input_json"), str):
                candidate = safe_json_loads(block["input_json"])
                parsed_input = candidate if isinstance(candidate, dict) else {}
            blocks.append(
                {
                    "type": "tool_use",
                    "id": block.get("id") or call_id(),
                    "name": block.get("name") or "tool",
                    "input": parsed_input or {},
                }
            )
        elif block.get("type") == "text":
            blocks.append({"type": "text", "text": as_text(block.get("text"))})

    if not blocks:
        blocks.append({"type": "text", "text": ""})

    stop_reason = data.get("stop_reason")
    if stop_reason not in {"end_turn", "tool_use", "max_tokens"}:
        stop_reason = "tool_use" if any(block.get("type") == "tool_use" for block in blocks) else "end_turn"

    response = {
        "id": message_id(),
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": estimate_tokens({"messages": [{"role": "assistant", "content": blocks}]}),
        },
    }
    if applied_edits:
        response["context_management"] = {"applied_edits": applied_edits}
    return response


def response_debug_summary(response: dict[str, Any]) -> str:
    parts = []
    for block in content_blocks(response.get("content")):
        block_type = block.get("type")
        if block_type == "text":
            preview = as_text(block.get("text")).replace("\n", " ")[:120]
            parts.append(f"text({len(as_text(block.get('text')))}):{preview!r}")
        elif block_type == "tool_use":
            parts.append(f"tool_use:{block.get('name')}")
        else:
            parts.append(str(block_type))
    return f"stop={response.get('stop_reason')} blocks=[{', '.join(parts)}]"


def estimate_tokens(body: dict[str, Any]) -> int:
    text = json.dumps(body.get("system", ""), ensure_ascii=False)
    text += json.dumps(body.get("messages", []), ensure_ascii=False)
    text += json.dumps(body.get("tools", []), ensure_ascii=False)
    return max(1, len(text) // 4)


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "anthropic-openai-gateway/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))

    def _send_json(self, status: int, data: Any, extra_headers: dict[str, str] | None = None) -> None:
        body = json_dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, message: str, error_type: str = "api_error") -> None:
        if error_type in {"unsupported_feature", "invalid_request_error", "configuration_error", "authentication_error"}:
            self.log_message("gateway error %s: %s", error_type, message.replace("\n", " ")[:500])
        elif status >= 500:
            first_line = message.splitlines()[0] if message else ""
            self.log_message("gateway error %s: %s", error_type, first_line[:500])
        self._send_json(status, {"type": "error", "error": {"type": error_type, "message": message}})

    def _read_json(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            self._send_error(400, f"Invalid JSON: {exc}", "invalid_request_error")
            return None

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "authorization,content-type,x-api-key,anthropic-version,anthropic-beta")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()

    def do_HEAD(self) -> None:
        path = urlparse(self.path).path
        self.send_response(200 if path in {"/", "/health"} else 404)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            try:
                default_resolved_model = resolve_openai_model("claude-sonnet-4-6")
                model_error = None
            except Exception as exc:
                default_resolved_model = None
                model_error = str(exc)
            self._send_json(
                200,
                {
                    "status": "ok",
                    "backend": active_backend(),
                    "upstream": OPENAI_BASE_URL,
                    "model": default_resolved_model,
                    "model_override": OPENAI_MODEL or None,
                    "model_map": str(MODEL_MAP_FILE),
                    "model_error": model_error,
                    "has_openai_api_key": bool(OPENAI_API_KEY),
                },
            )
            return

        if path == "/v1/models":
            config = load_model_config()
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": model_id,
                            "type": "model",
                            "display_name": f"gpt-cc route for {model_id}",
                            "created_at": "2026-01-01T00:00:00Z",
                        }
                        for model_id in advertised_claude_models(config)
                    ],
                },
            )
            return

        self._send_error(404, f"Unknown endpoint: {path}", "not_found_error")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        body = self._read_json()
        if body is None:
            return

        if path == "/v1/messages/count_tokens":
            original_tokens = estimate_tokens(body)
            managed_body, _ = apply_context_management(body)
            response = {"input_tokens": estimate_tokens(managed_body)}
            if isinstance(body.get("context_management"), dict):
                response["context_management"] = {"original_input_tokens": original_tokens}
            self._send_json(200, response)
            return

        if path != "/v1/messages":
            self._send_error(404, f"Unknown endpoint: {path}", "not_found_error")
            return

        try:
            validate_request(body)
        except GatewayUnsupportedError as exc:
            self._send_error(400, str(exc), "unsupported_feature")
            return
        except GatewayConfigError as exc:
            self._send_error(500, str(exc), "configuration_error")
            return

        backend = active_backend()
        if backend == "openai" and not OPENAI_API_KEY and not OPENAI_BASE_URL.startswith(("http://127.0.0.1", "http://localhost")):
            self._send_error(
                401,
                "OPENAI_API_KEY is not set. Set it, or point OPENAI_BASE_URL at a local gateway that does not require auth.",
                "authentication_error",
            )
            return

        try:
            managed_body, applied_edits = apply_context_management(body)
            payload = build_openai_payload(managed_body)
        except GatewayUnsupportedError as exc:
            self._send_error(400, str(exc), "unsupported_feature")
            return
        except GatewayConfigError as exc:
            self._send_error(500, str(exc), "configuration_error")
            return

        if backend == "codex":
            debug_log(f"request backend=codex stream={bool(payload.get('stream'))} model={body.get('model')}")
            if payload.get("stream"):
                self._stream_codex_message(managed_body, applied_edits)
            else:
                self._complete_codex_message(managed_body, applied_edits)
        elif payload.get("stream"):
            debug_log(f"request backend=openai stream=true model={body.get('model')}")
            self._stream_message(managed_body, payload, applied_edits)
        else:
            debug_log(f"request backend=openai stream=false model={body.get('model')}")
            self._complete_message(managed_body, payload, applied_edits)

    def _complete_codex_message(self, body: dict[str, Any], applied_edits: list[dict[str, Any]]) -> None:
        try:
            data = run_codex_backend(body)
            response = codex_response_to_anthropic(data, body.get("model") or "codex", applied_edits)
            debug_log("codex response " + response_debug_summary(response))
            self._send_json(200, response)
        except GatewayUnsupportedError as exc:
            self._send_error(502, str(exc), "codex_backend_error")
        except subprocess.TimeoutExpired:
            self._send_error(504, f"Codex backend timed out after {CODEX_TIMEOUT} seconds.", "codex_backend_timeout")
        except Exception as exc:
            self._send_error(500, f"Codex backend gateway error: {exc}\n{traceback.format_exc()}")

    def _complete_message(self, body: dict[str, Any], payload: dict[str, Any], applied_edits: list[dict[str, Any]]) -> None:
        try:
            with urllib.request.urlopen(upstream_request(payload), timeout=REQUEST_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            self._send_json(200, openai_response_to_anthropic(data, body.get("model") or OPENAI_MODEL, applied_edits))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            self._send_error(exc.code, f"Upstream OpenAI-compatible API error: {detail}")
        except Exception as exc:
            self._send_error(500, f"Gateway error: {exc}\n{traceback.format_exc()}")

    def _sse(self, event: str, data: dict[str, Any]) -> None:
        self.wfile.write(f"event: {event}\n".encode("utf-8"))
        self.wfile.write(f"data: {json_dumps(data)}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _stream_codex_message(self, body: dict[str, Any], applied_edits: list[dict[str, Any]]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        msg_id = message_id()
        try:
            data = run_codex_backend(body)
            response = codex_response_to_anthropic(data, body.get("model") or "codex", applied_edits)
            debug_log("codex stream response " + response_debug_summary(response))
            self._sse(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": msg_id,
                        "type": "message",
                        "role": "assistant",
                        "model": response["model"],
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0},
                    },
                },
            )
            for index, block in enumerate(response["content"]):
                if block.get("type") == "tool_use":
                    self._sse(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": index,
                            "content_block": {
                                "type": "tool_use",
                                "id": block.get("id") or call_id(),
                                "name": block.get("name") or "tool",
                                "input": {},
                            },
                        },
                    )
                    input_value = block.get("input") if isinstance(block.get("input"), dict) else {}
                    if input_value:
                        self._sse(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": index,
                                "delta": {"type": "input_json_delta", "partial_json": json_dumps(input_value)},
                            },
                        )
                else:
                    self._sse(
                        "content_block_start",
                        {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}},
                    )
                    text = as_text(block.get("text"))
                    if text:
                        self._sse(
                            "content_block_delta",
                            {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": text}},
                        )
                self._sse("content_block_stop", {"type": "content_block_stop", "index": index})
            message_delta = {
                "type": "message_delta",
                "delta": {"stop_reason": response["stop_reason"], "stop_sequence": None},
                "usage": {"output_tokens": response["usage"]["output_tokens"]},
            }
            if applied_edits:
                message_delta["context_management"] = {"applied_edits": applied_edits}
            self._sse("message_delta", message_delta)
            self._sse("message_stop", {"type": "message_stop"})
        except subprocess.TimeoutExpired:
            self._sse("error", {"type": "error", "error": {"type": "codex_backend_timeout", "message": f"Codex backend timed out after {CODEX_TIMEOUT} seconds."}})
        except Exception as exc:
            self._sse("error", {"type": "error", "error": {"type": "codex_backend_error", "message": f"{exc}\n{traceback.format_exc()}"}})

    def _stream_message(self, body: dict[str, Any], payload: dict[str, Any], applied_edits: list[dict[str, Any]]) -> None:
        try:
            resp = urllib.request.urlopen(upstream_request(payload), timeout=REQUEST_TIMEOUT)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            self._send_error(exc.code, f"Upstream OpenAI-compatible API error: {detail}")
            return
        except Exception as exc:
            self._send_error(500, f"Gateway error opening upstream stream: {exc}")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        msg_id = message_id()
        output_tokens = 0
        input_tokens = 0
        stop_reason = "end_turn"
        next_index = 0
        text_index: int | None = None
        text_open = False
        emitted_any_block = False
        tool_blocks: dict[int, dict[str, Any]] = {}
        pending_tool_args: dict[int, str] = {}

        start_message = {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": body.get("model") or OPENAI_MODEL,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0},
        }
        self._sse(
            "message_start",
            {
                "type": "message_start",
                "message": start_message,
            },
        )

        try:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data_text = line[5:].strip()
                if data_text == "[DONE]":
                    break

                chunk = json.loads(data_text)
                usage = chunk.get("usage") or {}
                input_tokens = usage.get("prompt_tokens", input_tokens)
                output_tokens = usage.get("completion_tokens", output_tokens)

                for choice in chunk.get("choices") or []:
                    finish_reason = choice.get("finish_reason")
                    if finish_reason:
                        stop_reason = map_stop_reason(finish_reason, finish_reason == "tool_calls")

                    delta = choice.get("delta") or {}
                    content_delta = delta.get("content")
                    if content_delta:
                        if text_index is None:
                            text_index = next_index
                            next_index += 1
                            text_open = True
                            emitted_any_block = True
                            self._sse(
                                "content_block_start",
                                {"type": "content_block_start", "index": text_index, "content_block": {"type": "text", "text": ""}},
                            )
                        self._sse(
                            "content_block_delta",
                            {"type": "content_block_delta", "index": text_index, "delta": {"type": "text_delta", "text": content_delta}},
                        )

                    for tool_call_delta in delta.get("tool_calls") or []:
                        openai_idx = int(tool_call_delta.get("index", 0))
                        fn = tool_call_delta.get("function") or {}
                        args_delta = fn.get("arguments") or ""

                        if openai_idx not in tool_blocks:
                            name = fn.get("name")
                            tool_id = tool_call_delta.get("id")
                            if not name:
                                pending_tool_args[openai_idx] = pending_tool_args.get(openai_idx, "") + args_delta
                                continue
                            content_index = next_index
                            next_index += 1
                            tool_blocks[openai_idx] = {"index": content_index, "id": tool_id or call_id(), "name": name}
                            emitted_any_block = True
                            self._sse(
                                "content_block_start",
                                {
                                    "type": "content_block_start",
                                    "index": content_index,
                                    "content_block": {
                                        "type": "tool_use",
                                        "id": tool_blocks[openai_idx]["id"],
                                        "name": name,
                                        "input": {},
                                    },
                                },
                            )
                            pending = pending_tool_args.pop(openai_idx, "")
                            if pending:
                                self._sse(
                                    "content_block_delta",
                                    {
                                        "type": "content_block_delta",
                                        "index": content_index,
                                        "delta": {"type": "input_json_delta", "partial_json": pending},
                                    },
                                )

                        if args_delta and openai_idx in tool_blocks:
                            self._sse(
                                "content_block_delta",
                                {
                                    "type": "content_block_delta",
                                    "index": tool_blocks[openai_idx]["index"],
                                    "delta": {"type": "input_json_delta", "partial_json": args_delta},
                                },
                            )

            if text_open and text_index is not None:
                self._sse("content_block_stop", {"type": "content_block_stop", "index": text_index})

            for state in sorted(tool_blocks.values(), key=lambda x: x["index"]):
                self._sse("content_block_stop", {"type": "content_block_stop", "index": state["index"]})

            if not emitted_any_block:
                self._sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
                self._sse("content_block_stop", {"type": "content_block_stop", "index": 0})

            message_delta = {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": output_tokens},
            }
            if applied_edits:
                message_delta["context_management"] = {"applied_edits": applied_edits}

            self._sse(
                "message_delta",
                message_delta,
            )
            self._sse("message_stop", {"type": "message_stop"})
        except Exception:
            self._sse("error", {"type": "error", "error": {"type": "api_error", "message": traceback.format_exc()}})
        finally:
            try:
                resp.close()
            except Exception:
                pass


def main() -> None:
    httpd = ThreadingHTTPServer((HOST, PORT), GatewayHandler)
    try:
        default_model = resolve_openai_model("claude-sonnet-4-6")
    except Exception as exc:
        default_model = f"unresolved ({exc})"
    backend = active_backend()
    print(f"anthropic-openai-gateway listening on http://{HOST}:{PORT}", flush=True)
    print(
        "backend="
        + backend
        + f" upstream={OPENAI_BASE_URL} default_model={default_model} override={OPENAI_MODEL or '-'} "
        + f"has_openai_api_key={bool(OPENAI_API_KEY)} codex_command={CODEX_COMMAND}",
        flush=True,
    )
    httpd.serve_forever()


if __name__ == "__main__":
    main()

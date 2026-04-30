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

Set OPENAI_API_KEY for the upstream API. Set OPENAI_BASE_URL and OPENAI_MODEL
to point at a compatible gateway/model.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
import uuid
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


SCRIPT_DIR = Path(__file__).resolve().parent
HOST = os.environ.get("GATEWAY_HOST", "127.0.0.1")
PORT = int(os.environ.get("GATEWAY_PORT", "8767"))

OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "")
OPENAI_ORG = os.environ.get("OPENAI_ORG", "")
OPENAI_PROJECT = os.environ.get("OPENAI_PROJECT", "")
OPENAI_REASONING_EFFORT = os.environ.get("OPENAI_REASONING_EFFORT", "")
MAX_TOKEN_FIELD = os.environ.get("OPENAI_MAX_TOKEN_FIELD", "max_completion_tokens")
REQUEST_TIMEOUT = int(os.environ.get("OPENAI_TIMEOUT_SECONDS", "600"))
MODEL_MAP_FILE = Path(os.environ.get("GPT_CC_MODEL_MAP", str(SCRIPT_DIR / "model-map.json")))
MODEL_CACHE_SECONDS = int(os.environ.get("GPT_CC_MODEL_CACHE_SECONDS", "300"))
AUTO_MODEL_RESOLUTION = os.environ.get("GPT_CC_AUTO_MODELS", "1").lower() not in {"0", "false", "no"}
STRICT_UNKNOWN_REQUEST_FIELDS = os.environ.get("GPT_CC_STRICT_UNKNOWN_FIELDS", "1").lower() not in {"0", "false", "no"}

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
    unsupported = set(request_fields.get("unsupported") or [])

    present_unsupported = [name for name in unsupported if body.get(name) not in (None, [], {}, "")]
    if present_unsupported:
        feature = present_unsupported[0]
        raise GatewayUnsupportedError(
            f"Claude Code requested Anthropic feature '{feature}', but gpt-cc has no configured GPT equivalent."
        )

    if STRICT_UNKNOWN_REQUEST_FIELDS and allowed:
        unknown = sorted(set(body.keys()) - allowed - unsupported)
        if unknown:
            raise GatewayUnsupportedError(
                "Claude Code sent unsupported request field(s): "
                + ", ".join(unknown)
                + ". Add an explicit mapping in model-map.json before using this feature."
            )


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

            assistant_message: dict[str, Any] = {"role": "assistant", "content": "\n\n".join(text_parts) or None}
            if tool_calls:
                assistant_message["tool_calls"] = tool_calls
            out.append(assistant_message)
            continue

        if role == "user":
            text_parts: list[str] = []
            for block in blocks:
                if block.get("type") == "tool_result":
                    if text_parts:
                        out.append({"role": "user", "content": "\n\n".join(text_parts)})
                        text_parts = []
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id") or block.get("id") or call_id(),
                            "content": block_to_text(block),
                        }
                    )
                else:
                    text = block_to_text(block)
                    if text:
                        text_parts.append(text)
            if text_parts:
                out.append({"role": "user", "content": "\n\n".join(text_parts)})
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

    if OPENAI_REASONING_EFFORT:
        payload["reasoning_effort"] = OPENAI_REASONING_EFFORT

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


def openai_response_to_anthropic(data: dict[str, Any], requested_model: str) -> dict[str, Any]:
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

    return {
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
            self._send_json(200, {"input_tokens": estimate_tokens(body)})
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

        if not OPENAI_API_KEY and not OPENAI_BASE_URL.startswith(("http://127.0.0.1", "http://localhost")):
            self._send_error(
                401,
                "OPENAI_API_KEY is not set. Set it, or point OPENAI_BASE_URL at a local gateway that does not require auth.",
                "authentication_error",
            )
            return

        try:
            payload = build_openai_payload(body)
        except GatewayUnsupportedError as exc:
            self._send_error(400, str(exc), "unsupported_feature")
            return
        except GatewayConfigError as exc:
            self._send_error(500, str(exc), "configuration_error")
            return
        if payload.get("stream"):
            self._stream_message(body, payload)
        else:
            self._complete_message(body, payload)

    def _complete_message(self, body: dict[str, Any], payload: dict[str, Any]) -> None:
        try:
            with urllib.request.urlopen(upstream_request(payload), timeout=REQUEST_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            self._send_json(200, openai_response_to_anthropic(data, body.get("model") or OPENAI_MODEL))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            self._send_error(exc.code, f"Upstream OpenAI-compatible API error: {detail}")
        except Exception as exc:
            self._send_error(500, f"Gateway error: {exc}\n{traceback.format_exc()}")

    def _sse(self, event: str, data: dict[str, Any]) -> None:
        self.wfile.write(f"event: {event}\n".encode("utf-8"))
        self.wfile.write(f"data: {json_dumps(data)}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _stream_message(self, body: dict[str, Any], payload: dict[str, Any]) -> None:
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

        self._sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": msg_id,
                    "type": "message",
                    "role": "assistant",
                    "model": body.get("model") or OPENAI_MODEL,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0},
                },
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

            self._sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {"output_tokens": output_tokens},
                },
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
    print(f"anthropic-openai-gateway listening on http://{HOST}:{PORT}", flush=True)
    print(f"upstream={OPENAI_BASE_URL} default_model={default_model} override={OPENAI_MODEL or '-'} has_openai_api_key={bool(OPENAI_API_KEY)}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()

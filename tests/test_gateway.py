import unittest
from unittest.mock import patch

import anthropic_openai_gateway as gw


class GatewayMappingTests(unittest.TestCase):
    def test_sonnet_resolves_to_latest_flagship_coding_model(self):
        models = [
            {"id": "gpt-5.4", "created": 1},
            {"id": "gpt-5.4-mini", "created": 2},
            {"id": "gpt-5.5", "created": 3},
            {"id": "text-embedding-3-large", "created": 4},
        ]
        with patch.object(gw, "OPENAI_MODEL", ""), patch.object(gw, "fetch_openai_models", return_value=models):
            self.assertEqual(gw.resolve_openai_model("claude-sonnet-4-6"), "gpt-5.5")

    def test_haiku_resolves_to_latest_mini_model(self):
        models = [
            {"id": "gpt-5-mini", "created": 1},
            {"id": "gpt-5.4-mini", "created": 2},
            {"id": "gpt-5.4-nano", "created": 3},
        ]
        with patch.object(gw, "OPENAI_MODEL", ""), patch.object(gw, "fetch_openai_models", return_value=models):
            self.assertEqual(gw.resolve_openai_model("claude-haiku-4-5"), "gpt-5.4-mini")

    def test_openai_model_env_is_exact_override(self):
        with patch.object(gw, "OPENAI_MODEL", "gpt-custom"):
            self.assertEqual(gw.resolve_openai_model("claude-opus-4-6"), "gpt-custom")

    def test_coding_profile_prefers_codex_when_version_matches(self):
        models = [
            {"id": "gpt-5.5", "created": 1},
            {"id": "gpt-5.5-codex", "created": 2},
        ]
        with patch.object(gw, "OPENAI_MODEL", ""), patch.object(gw, "fetch_openai_models", return_value=models):
            self.assertEqual(gw.resolve_openai_model("claude-sonnet-4-6"), "gpt-5.5-codex")

    def test_anthropic_tool_use_round_trips_as_openai_tool_call(self):
        messages = gw.anthropic_messages_to_openai(
            {
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_123",
                                "name": "Read",
                                "input": {"file_path": "README.md"},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_123",
                                "content": "file contents",
                            }
                        ],
                    },
                ]
            }
        )
        self.assertEqual(messages[0]["tool_calls"][0]["id"], "toolu_123")
        self.assertEqual(messages[0]["tool_calls"][0]["function"]["name"], "Read")
        self.assertEqual(messages[1]["role"], "tool")
        self.assertEqual(messages[1]["tool_call_id"], "toolu_123")

    def test_image_block_maps_to_openai_image_url(self):
        messages = gw.anthropic_messages_to_openai(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "look"},
                            {
                                "type": "image",
                                "source": {"type": "base64", "media_type": "image/png", "data": "abc123"},
                            },
                        ],
                    }
                ]
            }
        )
        self.assertEqual(messages[0]["content"][0], {"type": "text", "text": "look"})
        self.assertEqual(messages[0]["content"][1]["type"], "image_url")
        self.assertEqual(messages[0]["content"][1]["image_url"]["url"], "data:image/png;base64,abc123")

    def test_thinking_maps_to_reasoning_effort(self):
        body = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": "hi"}],
            "thinking": {"type": "enabled", "budget_tokens": 1024},
        }
        gw.validate_request(body)
        with patch.object(gw, "OPENAI_MODEL", "gpt-test"):
            payload = gw.build_openai_payload(body)
        self.assertEqual(payload["reasoning_effort"], "low")

    def test_adaptive_thinking_maps_to_high_reasoning(self):
        body = {
            "model": "claude-sonnet-4-6",
            "messages": [{"role": "user", "content": "hi"}],
            "thinking": {"type": "adaptive"},
        }
        with patch.object(gw, "OPENAI_MODEL", "gpt-test"):
            payload = gw.build_openai_payload(body)
        self.assertEqual(payload["reasoning_effort"], "high")

    def test_disabled_thinking_does_not_set_reasoning_effort(self):
        body = {
            "model": "claude-sonnet-4-6",
            "messages": [{"role": "user", "content": "hi"}],
            "thinking": {"type": "disabled"},
        }
        with patch.object(gw, "OPENAI_MODEL", "gpt-test"):
            payload = gw.build_openai_payload(body)
        self.assertNotIn("reasoning_effort", payload)

    def test_service_tier_maps_to_openai_tier(self):
        body = {
            "model": "claude-sonnet-4-6",
            "messages": [{"role": "user", "content": "hi"}],
            "service_tier": "standard_only",
        }
        with patch.object(gw, "OPENAI_MODEL", "gpt-test"):
            payload = gw.build_openai_payload(body)
        self.assertEqual(payload["service_tier"], "default")

    def test_top_k_is_accepted_as_noop(self):
        body = {
            "model": "claude-sonnet-4-6",
            "messages": [{"role": "user", "content": "hi"}],
            "top_k": 5,
        }
        gw.validate_request(body)
        with patch.object(gw, "OPENAI_MODEL", "gpt-test"):
            payload = gw.build_openai_payload(body)
        self.assertNotIn("top_k", payload)

    def test_container_is_accepted_as_noop(self):
        gw.validate_request(
            {
                "model": "claude-sonnet-4-6",
                "messages": [{"role": "user", "content": "hi"}],
                "container": "container_123",
            }
        )

    def test_context_management_is_accepted_as_noop(self):
        gw.validate_request(
            {
                "model": "claude-sonnet-4-6",
                "messages": [{"role": "user", "content": "hi"}],
                "context_management": {"edits": []},
            }
        )

    def test_context_management_clears_old_tool_results_locally(self):
        body = {
            "model": "claude-sonnet-4-6",
            "messages": [
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "toolu_old", "name": "Read", "input": {"file_path": "a"}}],
                },
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "toolu_old", "content": "old result"}],
                },
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "toolu_new", "name": "Read", "input": {"file_path": "b"}}],
                },
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "toolu_new", "content": "new result"}],
                },
            ],
            "context_management": {
                "edits": [
                    {
                        "type": "clear_tool_uses_20250919",
                        "trigger": {"type": "tool_uses", "value": 1},
                        "keep": {"type": "tool_uses", "value": 1},
                    }
                ]
            },
        }
        managed, applied = gw.apply_context_management(body)
        serialized = str(managed["messages"])
        self.assertIn("toolu_old", serialized)
        self.assertIn(gw.TOOL_RESULT_CLEARED_PLACEHOLDER, serialized)
        self.assertNotIn("old result", serialized)
        self.assertIn("toolu_new", serialized)
        self.assertEqual(applied[0]["type"], "clear_tool_uses_20250919")
        self.assertEqual(applied[0]["cleared_tool_uses"], 1)

    def test_context_management_can_clear_tool_inputs_too(self):
        body = {
            "model": "claude-sonnet-4-6",
            "messages": [
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "toolu_old", "name": "Read", "input": {"file_path": "a"}}],
                },
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "toolu_old", "content": "old result"}],
                },
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "toolu_new", "name": "Read", "input": {"file_path": "b"}}],
                },
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "toolu_new", "content": "new result"}],
                },
            ],
            "context_management": {
                "edits": [
                    {
                        "type": "clear_tool_uses_20250919",
                        "trigger": {"type": "tool_uses", "value": 1},
                        "keep": {"type": "tool_uses", "value": 1},
                        "clear_tool_inputs": True,
                    }
                ]
            },
        }
        managed, _ = gw.apply_context_management(body)
        serialized = str(managed["messages"])
        self.assertNotIn("toolu_old", serialized)
        self.assertNotIn("old result", serialized)
        self.assertIn("toolu_new", serialized)

    def test_context_management_respects_excluded_tools(self):
        body = {
            "model": "claude-sonnet-4-6",
            "messages": [
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "toolu_old", "name": "Read", "input": {"file_path": "a"}}],
                },
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "toolu_old", "content": "old result"}],
                },
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "toolu_new", "name": "Write", "input": {"file_path": "b"}}],
                },
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "toolu_new", "content": "new result"}],
                },
            ],
            "context_management": {
                "edits": [
                    {
                        "type": "clear_tool_uses_20250919",
                        "trigger": {"type": "tool_uses", "value": 1},
                        "keep": {"type": "tool_uses", "value": 0},
                        "exclude_tools": ["Read"],
                    }
                ]
            },
        }
        managed, _ = gw.apply_context_management(body)
        serialized = str(managed["messages"])
        self.assertIn("old result", serialized)
        self.assertIn(gw.TOOL_RESULT_CLEARED_PLACEHOLDER, serialized)
        self.assertNotIn("new result", serialized)

    def test_context_management_clears_thinking_blocks_locally(self):
        body = {
            "model": "claude-sonnet-4-6",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "old reasoning"},
                        {"type": "text", "text": "old visible"},
                    ],
                },
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "new reasoning"},
                        {"type": "text", "text": "new visible"},
                    ],
                }
            ],
            "context_management": {
                "edits": [
                    {
                        "type": "clear_thinking_20251015",
                        "trigger": {"type": "input_tokens", "value": 1},
                        "keep": {"type": "thinking_turns", "value": 1},
                    }
                ]
            },
        }
        managed, applied = gw.apply_context_management(body)
        serialized = str(managed["messages"])
        self.assertNotIn("old reasoning", serialized)
        self.assertIn("old visible", serialized)
        self.assertIn("new reasoning", serialized)
        self.assertEqual(applied[0]["type"], "clear_thinking_20251015")

    def test_context_management_keeps_recent_thinking_turns(self):
        body = {
            "model": "claude-sonnet-4-6",
            "messages": [
                {"role": "assistant", "content": [{"type": "thinking", "thinking": "old"}, {"type": "text", "text": "a"}]},
                {"role": "assistant", "content": [{"type": "thinking", "thinking": "new"}, {"type": "text", "text": "b"}]},
            ],
            "context_management": {
                "edits": [{"type": "clear_thinking_20251015", "keep": {"type": "thinking_turns", "value": 1}}]
            },
        }
        managed, _ = gw.apply_context_management(body)
        serialized = str(managed["messages"])
        self.assertNotIn("old", serialized)
        self.assertIn("new", serialized)

    def test_unknown_context_management_edit_fails_closed(self):
        with self.assertRaises(gw.GatewayUnsupportedError):
            gw.validate_request(
                {
                    "model": "claude-sonnet-4-6",
                    "messages": [{"role": "user", "content": "hi"}],
                    "context_management": {"edits": [{"type": "future_edit_20990101"}]},
                }
            )

    def test_unsupported_tool_type_fails_closed(self):
        with self.assertRaises(gw.GatewayUnsupportedError):
            gw.anthropic_tools_to_openai(
                [
                    {
                        "type": "web_search_20250305",
                        "name": "web_search",
                        "input_schema": {"type": "object", "properties": {}},
                    }
                ]
            )


if __name__ == "__main__":
    unittest.main()

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

    def test_unsupported_thinking_fails_closed(self):
        with self.assertRaises(gw.GatewayUnsupportedError):
            gw.validate_request(
                {
                    "model": "claude-sonnet-4-6",
                    "messages": [{"role": "user", "content": "hi"}],
                    "thinking": {"type": "enabled", "budget_tokens": 1024},
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

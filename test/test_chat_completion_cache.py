from __future__ import annotations

import unittest
from unittest import mock
import json

from services.config import config
from services.protocol import openai_v1_chat_complete, openai_v1_response
from services.protocol.chat_completion_cache import chat_completion_cache
from services.protocol.conversation import iter_conversation_payloads, sanitize_output_text


class ChatCompletionCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_cache_settings = config.data.get("chat_completion_cache")
        config.data["chat_completion_cache"] = {
            "enabled": True,
            "ttl_seconds": 60,
            "max_entries": 32,
            "dedupe_inflight": True,
            "stream_cache": True,
            "normalize_messages": True,
            "drop_adjacent_duplicates": True,
            "drop_assistant_history": False,
        }
        chat_completion_cache.clear()

    def tearDown(self) -> None:
        if self.old_cache_settings is None:
            config.data.pop("chat_completion_cache", None)
        else:
            config.data["chat_completion_cache"] = self.old_cache_settings
        chat_completion_cache.clear()

    def test_repeated_non_stream_text_completion_uses_cache(self) -> None:
        calls = 0

        def fake_collect_text(_backend, _request):
            nonlocal calls
            calls += 1
            return f"cached answer {calls}"

        body = {
            "model": "auto",
            "messages": [{"role": "user", "content": "cache this exact prompt"}],
        }

        with (
            mock.patch("services.protocol.openai_v1_chat_complete.text_backend", return_value=object()),
            mock.patch("services.protocol.openai_v1_chat_complete.collect_text", side_effect=fake_collect_text),
        ):
            first = openai_v1_chat_complete.handle(body)
            second = openai_v1_chat_complete.handle(body)

        self.assertEqual(calls, 1)
        self.assertEqual(
            first["choices"][0]["message"]["content"],
            second["choices"][0]["message"]["content"],
        )

    def test_repeated_stream_text_completion_replays_cached_chunks(self) -> None:
        calls = 0

        def fake_stream_text_deltas(_backend, _request):
            nonlocal calls
            calls += 1
            yield "streamed"
            yield " answer"

        body = {
            "model": "auto",
            "stream": True,
            "messages": [{"role": "user", "content": "stream cache this exact prompt"}],
        }

        with (
            mock.patch("services.protocol.openai_v1_chat_complete.text_backend", return_value=object()),
            mock.patch(
                "services.protocol.openai_v1_chat_complete.stream_text_deltas",
                side_effect=fake_stream_text_deltas,
            ),
        ):
            first = list(openai_v1_chat_complete.handle(body))
            second = list(openai_v1_chat_complete.handle(body))

        self.assertEqual(calls, 1)
        self.assertEqual(first, second)
        content = "".join(str(chunk["choices"][0]["delta"].get("content") or "") for chunk in second)
        self.assertEqual(content, "streamed answer")

    def test_adjacent_duplicate_messages_are_removed_before_upstream_call(self) -> None:
        captured_messages = []

        def fake_collect_text(_backend, request):
            captured_messages.extend(request.messages or [])
            return "ok"

        body = {
            "model": "auto",
            "messages": [
                {"role": "user", "content": "repeat me"},
                {"role": "user", "content": "repeat me"},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "next prompt"},
            ],
        }

        with (
            mock.patch("services.protocol.openai_v1_chat_complete.text_backend", return_value=object()),
            mock.patch("services.protocol.openai_v1_chat_complete.collect_text", side_effect=fake_collect_text),
        ):
            openai_v1_chat_complete.handle(body)

        self.assertEqual(
            captured_messages,
            [
                {"role": "user", "content": "repeat me"},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "next prompt"},
            ],
        )

    def test_chat_completion_usage_includes_cached_tokens(self) -> None:
        with (
            mock.patch("services.protocol.openai_v1_chat_complete.text_backend", return_value=object()),
            mock.patch("services.protocol.openai_v1_chat_complete.collect_text", return_value="ok"),
        ):
            response = openai_v1_chat_complete.handle({
                "model": "auto",
                "messages": [{"role": "user", "content": "usage shape"}],
            })

        details = response["usage"]["prompt_tokens_details"]
        self.assertEqual(details["cached_tokens"], 0)
        output_details = response["usage"]["completion_tokens_details"]
        self.assertEqual(output_details["reasoning_tokens"], 0)

    def test_responses_completed_usage_includes_cached_tokens(self) -> None:
        with (
            mock.patch("services.protocol.openai_v1_response.text_backend", return_value=object()),
            mock.patch("services.protocol.openai_v1_response.stream_text_deltas", return_value=iter(["ok"])),
        ):
            response = openai_v1_response.handle({
                "model": "auto",
                "input": "usage shape",
            })

        details = response["usage"]["input_tokens_details"]
        self.assertEqual(details["cached_tokens"], 0)
        output_details = response["usage"]["output_tokens_details"]
        self.assertEqual(output_details["reasoning_tokens"], 0)

    def test_repeated_responses_text_request_uses_cache(self) -> None:
        calls = 0

        def fake_stream_text_deltas(_backend, _request):
            nonlocal calls
            calls += 1
            yield f"response cache {calls}"

        body = {
            "model": "auto",
            "input": "cache this responses prompt",
            "stream": True,
        }

        with (
            mock.patch("services.protocol.openai_v1_response.text_backend", return_value=object()),
            mock.patch("services.protocol.openai_v1_response.stream_text_deltas", side_effect=fake_stream_text_deltas),
        ):
            first = list(openai_v1_response.handle(body))
            second = list(openai_v1_response.handle(body))

        self.assertEqual(calls, 1)
        self.assertEqual(first, second)

    def test_output_sanitizer_removes_chatgpt_annotation_markup(self) -> None:
        text = (
            "Repo: \ue200url\ue202basketikun/chatgpt2api"
            "\ue202https://github.com/basketikun/chatgpt2api\ue201 "
            "details \ue200cite\ue202turn0search0\ue201."
        )

        self.assertEqual(
            sanitize_output_text(text),
            "Repo: basketikun/chatgpt2api (https://github.com/basketikun/chatgpt2api) details .",
        )

    def test_stream_sanitizer_does_not_emit_partial_annotation_or_repeat_prefix(self) -> None:
        events = [
            {"p": "/message/content/parts/0", "o": "append", "v": "Repo: \ue200url\ue202chat"},
            {"p": "/message/content/parts/0", "o": "append", "v": "gpt2api\ue202turn0search0\ue201 done \ue200cite\ue202turn0\ue201."},
            "[DONE]",
        ]
        payloads = [json.dumps(event, ensure_ascii=False) if isinstance(event, dict) else event for event in events]
        deltas = [
            str(event.get("delta") or "")
            for event in iter_conversation_payloads(iter(payloads))
            if event.get("type") == "conversation.delta"
        ]

        self.assertEqual("".join(deltas), "Repo: chatgpt2api done .")
        self.assertFalse(any("\ue200" in delta or "\ue202" in delta or "\ue201" in delta for delta in deltas))

    def test_responses_tools_add_honest_no_tool_guard(self) -> None:
        model, messages = openai_v1_response.text_response_parts({
            "model": "auto",
            "input": "run echo hi",
            "tools": [{"type": "function", "name": "shell"}],
        })

        self.assertEqual(model, "auto")
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("cannot execute local tools", str(messages[0]["content"]))


if __name__ == "__main__":
    unittest.main()

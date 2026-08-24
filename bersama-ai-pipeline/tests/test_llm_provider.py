"""Tests for the provider-neutral LLM adapter and the four features that use it.

Run from bersama-ai-pipeline/:   python -m unittest discover -s tests -v
(no network: every provider response is served by an httpx MockTransport, so
this suite never makes a billable request and needs no API key.)
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import llm  # noqa: E402
from pipeline.llm import (DEFAULT_BASE_URL, DEFAULT_MODEL, LLMError,  # noqa: E402
                          MalformedOutput, NoToolCall, extract_tool_args,
                          llm_config, require_api_key, structured_call)

FAKE_KEY = "test-key-not-a-real-secret"

TOOL = {
    "name": "emit_thing",
    "description": "Emit one thing.",
    "input_schema": {
        "type": "object",
        "required": ["value"],
        "properties": {"value": {"type": "string"}},
    },
}


def _response_body(output: list[dict], *, status: str = "completed",
                   incomplete_reason: str | None = None) -> dict:
    """A minimal but SDK-parseable Responses API payload."""
    body = {
        "id": "resp_test",
        "object": "response",
        "created_at": 0,
        "model": DEFAULT_MODEL,
        "status": status,
        "output": output,
        "parallel_tool_calls": False,
        "tool_choice": "required",
        "tools": [],
    }
    if incomplete_reason:
        body["incomplete_details"] = {"reason": incomplete_reason}
    return body


def _function_call(args: str, name: str = "emit_thing") -> dict:
    return {"type": "function_call", "id": "fc_1", "call_id": "call_1",
            "name": name, "arguments": args, "status": "completed"}


def _text_message(text: str) -> dict:
    return {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


class RecordingProvider:
    """Fake OpenCode Go endpoint. Records every request; replies with `body`."""

    def __init__(self, body: dict | None = None, status_code: int = 200,
                 exc: Exception | None = None):
        self.body, self.status_code, self.exc = body, status_code, exc
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.exc:
            raise self.exc
        return httpx.Response(self.status_code, json=self.body or {})

    def client(self, *, base_url: str = DEFAULT_BASE_URL, api_key: str = FAKE_KEY):
        from openai import OpenAI
        return OpenAI(api_key=api_key, base_url=base_url, max_retries=0,
                      http_client=httpx.Client(transport=httpx.MockTransport(self.handler)))

    @property
    def last_json(self) -> dict:
        return json.loads(self.requests[-1].content)


# ── provider configuration ───────────────────────────────────────────────────

class TestConfig(unittest.TestCase):
    def test_defaults_are_opencode_go_and_luna(self):
        cfg = llm_config({})
        self.assertEqual(cfg["base_url"], "https://opencode.ai/zen/go/v1")
        self.assertEqual(cfg["model"], "gpt-5.6-luna")
        self.assertEqual(cfg["api_key"], "")

    def test_reads_neutral_env_vars(self):
        cfg = llm_config({"LLM_API_KEY": FAKE_KEY,
                          "LLM_BASE_URL": "https://example.test/v1",
                          "LLM_MODEL": "some-other-model"})
        self.assertEqual(cfg, {"api_key": FAKE_KEY, "model": "some-other-model",
                               "base_url": "https://example.test/v1"})

    def test_no_undocumented_fallback_to_old_provider_vars(self):
        cfg = llm_config({"ZAI_API_KEY": "legacy", "ZAI_BASE_URL": "https://api.z.ai/x",
                          "GLM_MODEL": "glm-5.2"})
        self.assertEqual(cfg["api_key"], "")
        self.assertEqual(cfg["base_url"], DEFAULT_BASE_URL)
        self.assertEqual(cfg["model"], DEFAULT_MODEL)

    def test_full_documented_endpoint_is_normalized(self):
        # Guards against the .../responses/responses double-append.
        cfg = llm_config({"LLM_BASE_URL": "https://opencode.ai/zen/go/v1/responses"})
        self.assertEqual(cfg["base_url"], "https://opencode.ai/zen/go/v1")

    def test_require_api_key_names_the_var_but_never_a_value(self):
        with self.assertRaises(LLMError) as ctx:
            require_api_key({"api_key": ""}, "judge news")
        self.assertIn("LLM_API_KEY", str(ctx.exception))
        with self.assertRaises(LLMError):
            require_api_key({}, "summarize")
        require_api_key({"api_key": FAKE_KEY}, "summarize")  # no raise


# ── routing / request shape ──────────────────────────────────────────────────

class TestRouting(unittest.TestCase):
    def test_request_hits_v1_responses_with_the_configured_model(self):
        prov = RecordingProvider(_response_body([_function_call('{"value": "ok"}')]))
        cfg = llm_config({"LLM_API_KEY": FAKE_KEY})
        data = structured_call(system="sys", user="usr", tool=TOOL,
                               max_output_tokens=512, client=prov.client(**{}), **cfg)
        self.assertEqual(data, {"value": "ok"})
        self.assertEqual(str(prov.requests[-1].url), "https://opencode.ai/zen/go/v1/responses")
        self.assertEqual(prov.last_json["model"], "gpt-5.6-luna")

    def test_no_double_responses_segment_when_base_url_has_it(self):
        prov = RecordingProvider(_response_body([_function_call('{"value": "ok"}')]))
        cfg = llm_config({"LLM_API_KEY": FAKE_KEY,
                          "LLM_BASE_URL": "https://opencode.ai/zen/go/v1/responses/"})
        structured_call(system="s", user="u", tool=TOOL, max_output_tokens=512,
                        client=prov.client(base_url=cfg["base_url"]), **cfg)
        self.assertNotIn("/responses/responses", str(prov.requests[-1].url))

    def test_tool_is_forced_in_the_flat_responses_shape(self):
        prov = RecordingProvider(_response_body([_function_call('{"value": "ok"}')]))
        structured_call(system="sys", user="usr", tool=TOOL, api_key=FAKE_KEY,
                        model=DEFAULT_MODEL, base_url=DEFAULT_BASE_URL,
                        max_output_tokens=512, client=prov.client())
        sent = prov.last_json
        self.assertEqual(sent["tool_choice"], "required")
        self.assertEqual(sent["instructions"], "sys")
        self.assertEqual(sent["input"], [{"role": "user", "content": "usr"}])
        tool = sent["tools"][0]
        self.assertEqual(tool["type"], "function")
        self.assertEqual(tool["name"], "emit_thing")          # flat, not {"function": {...}}
        self.assertEqual(tool["parameters"], TOOL["input_schema"])
        self.assertNotIn("max_completion_tokens", sent)        # chat-completions param is gone
        self.assertIn("max_output_tokens", sent)

    def test_missing_key_fails_before_any_request(self):
        prov = RecordingProvider(_response_body([]))
        with self.assertRaises(LLMError):
            structured_call(system="s", user="u", tool=TOOL, api_key="",
                            model=DEFAULT_MODEL, base_url=DEFAULT_BASE_URL,
                            max_output_tokens=512, client=prov.client())
        self.assertEqual(prov.requests, [])


# ── response parsing / structured-output validation ──────────────────────────

class TestParsing(unittest.TestCase):
    def test_malformed_arguments_raise_malformed_output(self):
        prov = RecordingProvider(_response_body([_function_call("{not json")]))
        with self.assertRaises(MalformedOutput):
            structured_call(system="s", user="u", tool=TOOL, api_key=FAKE_KEY,
                            model=DEFAULT_MODEL, base_url=DEFAULT_BASE_URL,
                            max_output_tokens=512, client=prov.client())

    def test_non_object_arguments_raise(self):
        prov = RecordingProvider(_response_body([_function_call('["a", "b"]')]))
        with self.assertRaises(MalformedOutput):
            structured_call(system="s", user="u", tool=TOOL, api_key=FAKE_KEY,
                            model=DEFAULT_MODEL, base_url=DEFAULT_BASE_URL,
                            max_output_tokens=512, client=prov.client())

    def test_text_only_answer_raises_no_tool_call(self):
        prov = RecordingProvider(_response_body([_text_message("I'd rather chat.")]))
        with self.assertRaises(NoToolCall):
            structured_call(system="s", user="u", tool=TOOL, api_key=FAKE_KEY,
                            model=DEFAULT_MODEL, base_url=DEFAULT_BASE_URL,
                            max_output_tokens=512, client=prov.client())

    def test_bare_json_in_text_output_is_still_accepted(self):
        prov = RecordingProvider(_response_body([_text_message('{"value": "ok"}')]))
        data = structured_call(system="s", user="u", tool=TOOL, api_key=FAKE_KEY,
                               model=DEFAULT_MODEL, base_url=DEFAULT_BASE_URL,
                               max_output_tokens=512, client=prov.client())
        self.assertEqual(data, {"value": "ok"})

    def test_truncated_response_is_an_error_not_a_silent_partial(self):
        with self.assertRaises(LLMError) as ctx:
            extract_tool_args({"status": "incomplete",
                               "incomplete_details": {"reason": "max_output_tokens"},
                               "output": []}, "emit_thing")
        self.assertIn("incomplete", str(ctx.exception))


# ── failure modes: auth, transport, timeout ──────────────────────────────────

class TestFailures(unittest.TestCase):
    def test_auth_failure_is_an_llm_error_without_the_key(self):
        os.environ["LLM_API_KEY"] = FAKE_KEY
        self.addCleanup(os.environ.pop, "LLM_API_KEY", None)
        prov = RecordingProvider({"error": {"message": "Invalid API key"}}, status_code=401)
        with self.assertRaises(LLMError) as ctx:
            structured_call(system="s", user="u", tool=TOOL, api_key=FAKE_KEY,
                            model=DEFAULT_MODEL, base_url=DEFAULT_BASE_URL,
                            max_output_tokens=512, client=prov.client())
        self.assertNotIn(FAKE_KEY, str(ctx.exception))

    def test_timeout_is_an_llm_error(self):
        prov = RecordingProvider(exc=httpx.ReadTimeout("timed out"))
        with self.assertRaises(LLMError):
            structured_call(system="s", user="u", tool=TOOL, api_key=FAKE_KEY,
                            model=DEFAULT_MODEL, base_url=DEFAULT_BASE_URL,
                            max_output_tokens=512, client=prov.client())

    def test_transport_failure_is_an_llm_error(self):
        prov = RecordingProvider(exc=httpx.ConnectError("provider down"))
        with self.assertRaises(LLMError):
            structured_call(system="s", user="u", tool=TOOL, api_key=FAKE_KEY,
                            model=DEFAULT_MODEL, base_url=DEFAULT_BASE_URL,
                            max_output_tokens=512, client=prov.client())


# ── feature behaviour preserved on top of the new provider ───────────────────

class TestSummarizer(unittest.TestCase):
    def test_valid_five_points_produce_a_summary(self):
        from pipeline import summarize as sm
        payload = {"hook": "h", "points": [f"p{i}" for i in range(5)],
                   "speaker": "S", "source_url": "https://x.test/v", "duration_sec": 61}
        calls = []

        def fake(**kw):
            calls.append(kw)
            return payload
        with _patched(sm, "structured_call", fake):
            out = sm.summarize({"title": "t"}, "transcript", api_key=FAKE_KEY)
        self.assertEqual(len(out.points), 5)
        self.assertEqual(calls[0]["model"], DEFAULT_MODEL)
        self.assertEqual(calls[0]["base_url"], DEFAULT_BASE_URL)

    def test_wrong_point_count_is_retried_then_reported(self):
        from pipeline import summarize as sm
        calls = []

        def fake(**kw):
            calls.append(kw)
            return {"hook": "h", "points": ["only", "three", "points"]}
        with _patched(sm, "structured_call", fake):
            with self.assertRaises(sm.SummarizeError):
                sm.summarize({"title": "t"}, "transcript", api_key=FAKE_KEY)
        self.assertEqual(len(calls), sm.MAX_SUMMARY_ATTEMPTS)   # retry preserved

    def test_provider_failure_is_retried_then_becomes_summarize_error(self):
        from pipeline import summarize as sm
        calls = []

        def fake(**kw):
            calls.append(kw)
            raise LLMError("OpenCode Go API call failed: boom")
        with _patched(sm, "structured_call", fake):
            with self.assertRaises(sm.SummarizeError):
                sm.summarize({"title": "t"}, "transcript", api_key=FAKE_KEY)
        self.assertEqual(len(calls), sm.MAX_SUMMARY_ATTEMPTS)

    def test_no_key_raises_before_calling(self):
        from pipeline import summarize as sm
        with self.assertRaises(sm.SummarizeError):
            sm.summarize({"title": "t"}, "transcript", api_key="")


class TestNewsJudge(unittest.TestCase):
    def test_judge_parses_items_and_drops_unknown_topics(self):
        from pipeline import news
        items = {"items": [
            {"topic": "coding", "category": "LAUNCH", "headline": "H",
             "body": "B", "source_url": "https://x.test/a", "heat_reason": "R"},
            {"topic": "not_a_topic", "category": "UPDATE", "headline": "X",
             "body": "B", "source_url": "https://x.test/b"},
        ]}
        with _patched(news, "structured_call", lambda **kw: items):
            out = news.judge([{"source": "hn", "score": 1, "title": "t",
                               "url": "https://x.test/a", "snippet": ""}],
                             api_key=FAKE_KEY, model=DEFAULT_MODEL, base_url=DEFAULT_BASE_URL)
        self.assertEqual([i.topic for i in out], ["coding"])

    def test_no_tool_call_means_an_empty_digest_not_a_failure(self):
        from pipeline import news

        def fake(**kw):
            raise NoToolCall("model did not return the emit_news function call")
        with _patched(news, "structured_call", fake):
            self.assertEqual(news.judge([], api_key=FAKE_KEY, model=DEFAULT_MODEL,
                                        base_url=DEFAULT_BASE_URL), [])

    def test_provider_failure_raises_news_error(self):
        from pipeline import news

        def fake(**kw):
            raise LLMError("OpenCode Go API call failed: ConnectError: down")
        with _patched(news, "structured_call", fake):
            with self.assertRaises(news.NewsError):
                news.judge([], api_key=FAKE_KEY, model=DEFAULT_MODEL,
                           base_url=DEFAULT_BASE_URL)

    def test_missing_key_raises_news_error_naming_the_neutral_var(self):
        from pipeline import news
        with self.assertRaises(news.NewsError) as ctx:
            news.judge([], api_key="", model=DEFAULT_MODEL, base_url=DEFAULT_BASE_URL)
        self.assertIn("LLM_API_KEY", str(ctx.exception))

    def test_share_without_a_key_degrades_gracefully(self):
        from pipeline import news
        self.assertEqual(
            news.post_url_as_news("https://x.test/a", api_key="", model=DEFAULT_MODEL,
                                  base_url=DEFAULT_BASE_URL, dry_run=True),
            "SHARE_NO_API_KEY")


class TestSerenityGracefulFallback(unittest.TestCase):
    TEXT = "NVDA and MU look strong into the print; HBM demand is nuts."

    def test_llm_topics_union_keyword_topics(self):
        from pipeline import serenity_digest as sd
        with _patched(sd, "structured_call", lambda **kw: {"topics": ["Quantum"]}):
            topics = sd._tag_topics(self.TEXT, api_key=FAKE_KEY, model=DEFAULT_MODEL,
                                    base_url=DEFAULT_BASE_URL)
        self.assertIn("Quantum", topics)
        self.assertIn("Memory, Storage & Servers", topics)   # keyword rule still applied

    def test_llm_failure_falls_back_to_keyword_topics_and_still_tags(self):
        from pipeline import serenity_digest as sd

        def fake(**kw):
            raise LLMError("OpenCode Go API call failed: ReadTimeout")
        with _patched(sd, "structured_call", fake):
            topics = sd._tag_topics(self.TEXT, api_key=FAKE_KEY, model=DEFAULT_MODEL,
                                    base_url=DEFAULT_BASE_URL)
        self.assertTrue(topics)
        self.assertEqual(topics, sd._keyword_topics(self.TEXT))

    def test_no_key_means_keyword_only_no_call(self):
        from pipeline import serenity_digest as sd

        def boom(**kw):
            raise AssertionError("must not call the provider without a key")
        with _patched(sd, "structured_call", boom):
            topics = sd._tag_topics(self.TEXT, api_key="", model=DEFAULT_MODEL,
                                    base_url=DEFAULT_BASE_URL)
        self.assertEqual(topics, sd._keyword_topics(self.TEXT))


class _patched:
    """Tiny context manager so the suite needs no test-only dependency."""

    def __init__(self, module, name, value):
        self.module, self.name, self.value = module, name, value

    def __enter__(self):
        self.old = getattr(self.module, self.name)
        setattr(self.module, self.name, self.value)
        return self.value

    def __exit__(self, *exc):
        setattr(self.module, self.name, self.old)
        return False


if __name__ == "__main__":
    unittest.main()

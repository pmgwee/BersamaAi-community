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
from pipeline.llm import (CODEX_AUTH_SENTINEL, DEFAULT_BASE_URL, DEFAULT_MODEL,  # noqa: E402
                          DEFAULT_REASONING_EFFORT, LLMError, MalformedOutput,
                          NoToolCall, extract_tool_args, llm_config,
                          require_api_key, resolve_auth_mode, resolve_reasoning_effort,
                          structured_call)

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
    """Fake Responses API endpoint. Records every request; replies with `body`."""

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
    def test_defaults_are_codex_oauth_and_luna(self):
        cfg = llm_config({})
        self.assertEqual(cfg["base_url"], "https://api.openai.com/v1")
        self.assertEqual(cfg["model"], "gpt-5.6-luna")
        self.assertEqual(cfg["api_key"], CODEX_AUTH_SENTINEL)

    def test_api_mode_requires_and_reads_the_key(self):
        cfg = llm_config({"LLM_AUTH_MODE": "api", "LLM_API_KEY": FAKE_KEY})
        self.assertEqual(cfg["api_key"], FAKE_KEY)
        self.assertEqual(resolve_auth_mode({"LLM_AUTH_MODE": "api"}), "api")

    def test_config_does_not_carry_effort_into_the_creds_splat(self):
        # main.py does `summarize(..., **llm_creds())`; a 4th key here would be
        # an unexpected kwarg on every feature function.
        self.assertEqual(set(llm_config({})), {"api_key", "model", "base_url"})

    def test_reads_neutral_env_vars(self):
        cfg = llm_config({"LLM_AUTH_MODE": "api", "LLM_API_KEY": FAKE_KEY,
                          "LLM_BASE_URL": "https://example.test/v1",
                          "LLM_MODEL": "some-other-model"})
        self.assertEqual(cfg, {"api_key": FAKE_KEY, "model": "some-other-model",
                               "base_url": "https://example.test/v1"})

    def test_no_undocumented_fallback_to_old_provider_vars(self):
        cfg = llm_config({"LLM_AUTH_MODE": "api", "ZAI_API_KEY": "legacy", "ZAI_BASE_URL": "https://api.z.ai/x",
                          "GLM_MODEL": "glm-5.2"})
        self.assertEqual(cfg["api_key"], "")
        self.assertEqual(cfg["base_url"], DEFAULT_BASE_URL)
        self.assertEqual(cfg["model"], DEFAULT_MODEL)

    def test_full_documented_endpoint_is_normalized(self):
        # Guards against the .../responses/responses double-append.
        cfg = llm_config({"LLM_BASE_URL": "https://api.openai.com/v1/responses"})
        self.assertEqual(cfg["base_url"], "https://api.openai.com/v1")

    def test_require_api_key_names_the_var_but_never_a_value(self):
        with self.assertRaises(LLMError) as ctx:
            require_api_key({"api_key": ""}, "judge news")
        self.assertIn("LLM_API_KEY", str(ctx.exception))
        with self.assertRaises(LLMError):
            require_api_key({}, "summarize")
        require_api_key({"api_key": FAKE_KEY}, "summarize")  # no raise


# ── reasoning effort (GPT-5.6 Luna: low | medium | high | xhigh | max) ──────

class TestReasoningEffort(unittest.TestCase):
    def test_unset_means_max(self):
        self.assertEqual(resolve_reasoning_effort({}), "max")
        self.assertEqual(DEFAULT_REASONING_EFFORT, "max")

    def test_every_documented_level_is_accepted(self):
        for level in ("low", "medium", "high", "xhigh", "max"):
            self.assertEqual(resolve_reasoning_effort({"LLM_REASONING_EFFORT": level}), level)
        self.assertEqual(resolve_reasoning_effort({"LLM_REASONING_EFFORT": " XHigh "}), "xhigh")

    def test_opt_out_values_mean_send_no_parameter(self):
        for raw in ("", "   ", "off", "none"):
            self.assertEqual(resolve_reasoning_effort({"LLM_REASONING_EFFORT": raw}), "")

    def test_unknown_level_degrades_to_default_instead_of_failing_a_run(self):
        self.assertEqual(resolve_reasoning_effort({"LLM_REASONING_EFFORT": "extra-high"}),
                         "max")

    def test_effort_is_sent_in_the_responses_api_shape(self):
        prov = RecordingProvider(_response_body([_function_call('{"value": "ok"}')]))
        structured_call(system="s", user="u", tool=TOOL, api_key=FAKE_KEY,
                        model=DEFAULT_MODEL, base_url=DEFAULT_BASE_URL,
                        max_output_tokens=512, effort="xhigh", client=prov.client())
        self.assertEqual(prov.last_json["reasoning"], {"effort": "xhigh"})

    def test_empty_effort_omits_the_parameter_entirely(self):
        # A model without reasoning support would 400 on reasoning:{effort:null}.
        prov = RecordingProvider(_response_body([_function_call('{"value": "ok"}')]))
        structured_call(system="s", user="u", tool=TOOL, api_key=FAKE_KEY,
                        model=DEFAULT_MODEL, base_url=DEFAULT_BASE_URL,
                        max_output_tokens=512, effort="", client=prov.client())
        self.assertNotIn("reasoning", prov.last_json)

    def test_output_budget_floor_leaves_room_for_reasoning_tokens(self):
        # max_output_tokens covers reasoning too, so a caller asking for a tiny
        # budget must still be clamped up or xhigh returns status=incomplete.
        prov = RecordingProvider(_response_body([_function_call('{"value": "ok"}')]))
        structured_call(system="s", user="u", tool=TOOL, api_key=FAKE_KEY,
                        model=DEFAULT_MODEL, base_url=DEFAULT_BASE_URL,
                        max_output_tokens=16, effort="xhigh", client=prov.client())
        self.assertEqual(prov.last_json["max_output_tokens"], llm.MIN_OUTPUT_TOKENS)
        self.assertGreaterEqual(llm.MIN_OUTPUT_TOKENS, 2048)


# ── routing / request shape ──────────────────────────────────────────────────

class TestRouting(unittest.TestCase):
    def test_codex_oauth_uses_schema_and_scrubs_pipeline_secrets(self):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured.update(cmd=cmd, kwargs=kwargs)
            schema = Path(cmd[cmd.index("--output-schema") + 1])
            captured["schema"] = json.loads(schema.read_text(encoding="utf-8"))
            output = Path(cmd[cmd.index("--output-last-message") + 1])
            output.write_text('{"value": "ok"}', encoding="utf-8")
            return llm.subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        os.environ["DISCORD_TOKEN"] = "must-not-leak"
        self.addCleanup(os.environ.pop, "DISCORD_TOKEN", None)
        with _patched(llm.shutil, "which", lambda _: "/usr/local/bin/codex"), \
                _patched(llm.subprocess, "run", fake_run):
            data = structured_call(system="sys", user="usr", tool=TOOL,
                                   api_key=CODEX_AUTH_SENTINEL,
                                   model=DEFAULT_MODEL, base_url=DEFAULT_BASE_URL,
                                   max_output_tokens=512, effort="max")

        self.assertEqual(data, {"value": "ok"})
        self.assertIn("--output-schema", captured["cmd"])
        self.assertIn("shell_tool", captured["cmd"])
        self.assertIn('model_reasoning_effort="max"', captured["cmd"])
        self.assertFalse(captured["schema"]["additionalProperties"])
        self.assertEqual(captured["schema"]["required"], ["value"])
        self.assertNotIn("DISCORD_TOKEN", captured["kwargs"]["env"])
        self.assertIn("Task input:\nusr", captured["kwargs"]["input"])
        self.assertEqual(captured["kwargs"]["encoding"], "utf-8")

    def test_request_hits_v1_responses_with_the_configured_model(self):
        prov = RecordingProvider(_response_body([_function_call('{"value": "ok"}')]))
        cfg = llm_config({"LLM_AUTH_MODE": "api", "LLM_API_KEY": FAKE_KEY})
        data = structured_call(system="sys", user="usr", tool=TOOL,
                               max_output_tokens=512, client=prov.client(**{}), **cfg)
        self.assertEqual(data, {"value": "ok"})
        self.assertEqual(str(prov.requests[-1].url), "https://api.openai.com/v1/responses")
        self.assertEqual(prov.last_json["model"], "gpt-5.6-luna")

    def test_no_double_responses_segment_when_base_url_has_it(self):
        prov = RecordingProvider(_response_body([_function_call('{"value": "ok"}')]))
        cfg = llm_config({"LLM_AUTH_MODE": "api", "LLM_API_KEY": FAKE_KEY,
                          "LLM_BASE_URL": "https://api.openai.com/v1/responses/"})
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
            raise LLMError("Codex OAuth call failed: boom")
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
            raise LLMError("Codex OAuth call failed: ConnectError: down")
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
            raise LLMError("Codex OAuth call failed: ReadTimeout")
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

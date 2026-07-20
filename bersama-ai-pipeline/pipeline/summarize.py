"""LLM summarization via GLM (Zhipu / Z.ai) through its OpenAI-compatible endpoint,
using a forced function call for strict structured output.

The tool schema (prompts.EMIT_SUMMARY_TOOL.input_schema) requests exactly 5 English
points. GLM does not strictly enforce schemas, so we treat the schema as a *request*
and validate the response ourselves in _coerce (this is also why a malformed response
becomes a clean SummarizeError instead of a crash).

Provider notes:
  - base_url: open.bigmodel.cn (China) or api.z.ai (international) — same wire format.
    Trailing slash is required by the OpenAI SDK or you get 404s.
  - tool_choice="required" forces a function call; with only one tool, it's emit_summary.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from typing import Optional

from openai import OpenAI

from .prompts import SYSTEM_PROMPT, EMIT_SUMMARY_TOOL, build_user_message

GLM_DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"  # trailing slash matters
GLM_DEFAULT_MODEL = "glm-4.6"


@dataclass
class Summary:
    hook: str
    points: list[str]
    speaker: str
    source_url: str
    duration_sec: int

    def to_dict(self) -> dict:
        return asdict(self)


class SummarizeError(Exception):
    pass


def summarize(
    meta: dict,
    transcript: str,
    api_key: str,
    model: str = GLM_DEFAULT_MODEL,
    base_url: str = GLM_DEFAULT_BASE_URL,
) -> Summary:
    """Call GLM with a forced emit_summary function call; return a validated Summary."""
    if not api_key:
        raise SummarizeError("ZAI_API_KEY is missing — cannot summarize.")

    client = OpenAI(api_key=api_key, base_url=base_url)
    tool = {
        "type": "function",
        "function": {
            "name": EMIT_SUMMARY_TOOL["name"],
            "description": EMIT_SUMMARY_TOOL["description"],
            "parameters": EMIT_SUMMARY_TOOL["input_schema"],
        },
    }
    user_msg = build_user_message(meta, transcript)

    try:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=2048,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            tools=[tool],
            tool_choice="required",  # force a tool call; only one tool => emit_summary
        )
    except Exception as e:  # noqa: BLE001
        raise SummarizeError(f"GLM API call failed: {e}") from e

    data = _extract_function_args(resp)
    if data is None:
        raise SummarizeError("Model did not return the emit_summary function call.")

    try:
        return _coerce(data, meta)
    except SummarizeError:
        raise
    except Exception as e:  # noqa: BLE001 — schema/validation surprises (e.g. non-int duration)
        raise SummarizeError(f"summary validation failed: {e}") from e


def _extract_function_args(resp) -> Optional[dict]:
    """Pull the emit_summary arguments out of an OpenAI-format response."""
    msg = resp.choices[0].message
    tool_calls = getattr(msg, "tool_calls", None)
    if not tool_calls:
        return None
    args = getattr(tool_calls[0].function, "arguments", None)
    if not args:
        return None
    try:
        return json.loads(args)
    except (json.JSONDecodeError, TypeError) as e:
        raise SummarizeError(f"emit_summary returned invalid JSON arguments: {e}") from e


def _safe_int(v, default: int = 0) -> int:
    """Coerce to int without raising (the model may return a string/float/None)."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _coerce(data: dict, meta: dict) -> Summary:
    """Validate + fall back to metadata for the passthrough fields."""
    def need_list(key, n=5):
        v = data.get(key) or []
        v = [str(x).strip() for x in v if str(x).strip()]
        if len(v) != n:
            raise SummarizeError(f"'{key}' must have exactly {n} non-empty points, got {len(v)}")
        return v

    return Summary(
        hook=str(data.get("hook", "")).strip(),
        points=need_list("points"),
        speaker=str(data.get("speaker") or meta.get("uploader") or "").strip(),
        source_url=str(data.get("source_url") or meta.get("webpage_url") or "").strip(),
        duration_sec=_safe_int(data.get("duration_sec") or meta.get("duration") or 0),
    )


def stub_summary(meta: dict) -> Summary:
    """A canned English summary for LOCAL TESTING only (no API key needed).

    Exercised when main.py runs with --stub-summary. Never used in production.
    """
    title = meta.get("title", "a great talk")
    return Summary(
        hook=f"Five takeaways from {title} (STUB — replace with real summary).",
        points=[
            "Point one: the core idea, stated plainly.",
            "Point two: why it matters for everyday AI use.",
            "Point three: a concrete thing you can try today.",
            "Point four: a common mistake to avoid.",
            "Point five: where to go deeper next.",
        ],
        speaker=meta.get("uploader") or meta.get("channel") or "Unknown",
        source_url=meta.get("webpage_url") or meta.get("url") or "",
        duration_sec=int(meta.get("duration") or 0),
    )

"""LLM summarization through the provider-neutral adapter (`pipeline.llm`),
using a forced function call for strict structured output.

The tool schema (prompts.EMIT_SUMMARY_TOOL.input_schema) requests exactly 5 English
points. Providers do not strictly enforce schemas, so we treat the schema as a *request*
and validate the response ourselves in _coerce (this is also why a malformed response
becomes a clean SummarizeError instead of a crash).

Provider notes:
  - which provider/model/endpoint is in use lives in `pipeline/llm.py` only.
  - tool_choice="required" forces a function call; with only one tool, it's emit_summary.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from .llm import DEFAULT_BASE_URL, DEFAULT_MODEL, structured_call
from .prompts import SYSTEM_PROMPT, EMIT_SUMMARY_TOOL, build_user_message

MAX_SUMMARY_ATTEMPTS = 3   # the model doesn't strictly enforce the tool schema; retry a malformed reply
SUMMARY_MAX_OUTPUT_TOKENS = 4096  # Responses API counts reasoning tokens too


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
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    lang_hint: str = "other",
) -> Summary:
    """Call the model with a forced emit_summary function call; return a validated Summary.

    `lang_hint` ("en"/"zh"/"other", from fetch.get_transcript) is forwarded to the
    prompt so the summary is written in the source video's language, not translated."""
    if not api_key:
        raise SummarizeError("LLM_API_KEY is missing — cannot summarize.")

    user_msg = build_user_message(meta, transcript, lang_hint)

    # The model does not strictly enforce the emit_summary schema, so a call can
    # occasionally return the wrong point count or malformed JSON. A fresh call
    # almost always succeeds — retry rather than fail the whole video on a
    # transient variance.
    last_err = None
    for attempt in range(1, MAX_SUMMARY_ATTEMPTS + 1):
        try:
            data = structured_call(
                system=SYSTEM_PROMPT,
                user=user_msg,
                tool=EMIT_SUMMARY_TOOL,
                api_key=api_key, model=model, base_url=base_url,
                max_output_tokens=SUMMARY_MAX_OUTPUT_TOKENS,
            )
            return _coerce(data, meta)
        except SummarizeError as e:
            last_err = e
        except Exception as e:  # noqa: BLE001 — transport/schema surprises
            last_err = SummarizeError(f"summary attempt {attempt} failed: {e}")
        if attempt < MAX_SUMMARY_ATTEMPTS:
            print(f"[summarize] attempt {attempt}/{MAX_SUMMARY_ATTEMPTS} rejected ({last_err}); retrying…")
    raise last_err or SummarizeError("summarize failed after retries")


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

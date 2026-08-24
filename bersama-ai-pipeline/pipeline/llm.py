"""Provider-neutral LLM adapter — the pipeline's ONLY door to a model provider.

Everything else in the pipeline (summarizer, news judge, /share card writer,
Serenity topic tagger) calls `structured_call()` here and never learns which
vendor is behind it. Swapping providers again should mean editing this file and
three env vars, not another repo-wide refactor.

Current provider: **OpenCode Go** (https://opencode.ai/docs/go/), model
`gpt-5.6-luna`, reached through its **Responses API**. The official `openai`
Python SDK speaks that wire format, so we point it at OpenCode Go's base URL:

    LLM_BASE_URL = https://opencode.ai/zen/go/v1     (no "/responses" — the SDK
                                                      appends it, giving
                                                      .../v1/responses)

Structured output: every caller wants ONE strict JSON object, so we keep the
proven idiom — declare a single function tool and force it with
`tool_choice="required"` — and validate the returned arguments locally in the
caller (`_coerce` / `TOPIC_BY_KEY` checks / topic enums). Providers do not
strictly enforce schemas, so that local validation stays the real contract; a
malformed or truncated reply raises `LLMError` and the caller's existing
retry / graceful-degradation path takes over.

Config (neutral names only — no provider-specific fallbacks):
    LLM_API_KEY   (required to call anything)
    LLM_BASE_URL  (default: OpenCode Go zen/go/v1)
    LLM_MODEL     (default: gpt-5.6-luna)

The key is read from the environment and never logged or echoed — error text
mentions the variable NAME only.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from openai import OpenAI

PROVIDER_LABEL = "OpenCode Go"
DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1"
DEFAULT_MODEL = "gpt-5.6-luna"

# The Responses API counts REASONING tokens against max_output_tokens, so the
# old chat-completions budgets (which only had to cover the visible tool JSON)
# are raised at the call sites. This is the floor we clamp to.
MIN_OUTPUT_TOKENS = 512


class LLMError(Exception):
    """Any provider/transport/shape failure. Never carries secret material."""


class NoToolCall(LLMError):
    """The model answered, but not with the forced function call. Callers that
    treat "nothing to emit" as a valid outcome (the news judge) key off this."""


class MalformedOutput(LLMError):
    """A function call came back, but its arguments were not a JSON object."""


def _normalize_base_url(raw: str) -> str:
    """Accept the base URL, and tolerate someone pasting the full documented
    endpoint (".../v1/responses") — strip the operation so the SDK cannot end
    up requesting "/responses/responses"."""
    url = (raw or "").strip().rstrip("/")
    if url.endswith("/responses"):
        url = url[: -len("/responses")]
    return url or DEFAULT_BASE_URL


def llm_config(env: Optional[dict] = None) -> dict:
    """Resolve LLM settings from the neutral env vars. A missing key resolves to
    "" so callers that degrade gracefully (Serenity tagging, /share) can check it
    themselves; `require_api_key()` is for paths that must fail loudly."""
    e = os.environ if env is None else env
    return {
        "api_key": (e.get("LLM_API_KEY") or "").strip(),
        "model": (e.get("LLM_MODEL") or "").strip() or DEFAULT_MODEL,
        "base_url": _normalize_base_url(e.get("LLM_BASE_URL") or DEFAULT_BASE_URL),
    }


def require_api_key(cfg: dict, what: str) -> None:
    """Secret-safe config validation: names the variable, never a value."""
    if not cfg.get("api_key"):
        raise LLMError(f"LLM_API_KEY is missing — cannot {what}.")


def function_tool(spec: dict) -> dict:
    """Our internal tool spec ({name, description, input_schema}) -> the
    Responses API's flat function-tool shape."""
    return {
        "type": "function",
        "name": spec["name"],
        "description": spec.get("description", ""),
        "parameters": spec["input_schema"],
        # strict=False: these schemas use optional fields / maxLength and are not
        # written for OpenAI strict mode. Forcing the call + validating locally
        # is the contract (see module docstring).
        "strict": False,
    }


def build_client(*, api_key: str, base_url: str, timeout: Optional[float] = None,
                 max_retries: Optional[int] = None) -> OpenAI:
    """The one place that constructs a provider client."""
    kwargs: dict[str, Any] = {"api_key": api_key, "base_url": base_url}
    if timeout is not None:
        kwargs["timeout"] = timeout
    if max_retries is not None:
        kwargs["max_retries"] = max_retries
    return OpenAI(**kwargs)


def structured_call(
    *,
    system: str,
    user: str,
    tool: dict,
    api_key: str,
    model: str,
    base_url: str,
    max_output_tokens: int,
    timeout: Optional[float] = None,
    max_retries: Optional[int] = None,
    client: Optional[OpenAI] = None,
) -> dict:
    """Force `tool` and return its arguments as a dict.

    Raises LLMError on transport failure, a refused/absent tool call, a
    truncated ("incomplete") response, or non-JSON arguments — the caller
    decides whether that means retry, skip, or fall back.
    `client` is an injection point for tests; production passes nothing.
    """
    if not api_key:
        raise LLMError("LLM_API_KEY is missing — cannot call the model.")
    cl = client or build_client(api_key=api_key, base_url=base_url,
                                timeout=timeout, max_retries=max_retries)
    try:
        resp = cl.responses.create(
            model=model,
            instructions=system,
            input=[{"role": "user", "content": user}],
            tools=[function_tool(tool)],
            tool_choice="required",   # one tool declared => this tool
            max_output_tokens=max(max_output_tokens, MIN_OUTPUT_TOKENS),
        )
    except Exception as e:  # noqa: BLE001 — transport/auth/rate-limit surprises
        raise LLMError(f"{PROVIDER_LABEL} API call failed: {_safe_err(e)}") from e
    return extract_tool_args(resp, tool["name"])


def extract_tool_args(resp: Any, tool_name: str) -> dict:
    """Pull one function call's arguments out of a Responses-API result.

    Tolerates both SDK objects and plain dicts (fixtures), and falls back to a
    bare JSON object in the text output if the provider answered with text
    instead of a call. Local validation in the caller is unchanged either way.
    """
    if _get(resp, "status") == "incomplete":
        reason = _get(_get(resp, "incomplete_details"), "reason") or "unknown"
        raise LLMError(f"response incomplete ({reason}) — no usable {tool_name} arguments")

    for item in _get(resp, "output") or []:
        if _get(item, "type") != "function_call":
            continue
        if tool_name and _get(item, "name") not in (tool_name, None):
            continue
        raw = _get(item, "arguments") or ""
        try:
            data = json.loads(raw or "{}")
        except (json.JSONDecodeError, TypeError) as e:
            raise MalformedOutput(f"{tool_name} returned invalid JSON arguments: {e}") from e
        if not isinstance(data, dict):
            raise MalformedOutput(f"{tool_name} arguments were not a JSON object")
        return data

    text = (_get(resp, "output_text") or "").strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    raise NoToolCall(f"model did not return the {tool_name} function call")


def _get(obj: Any, attr: str) -> Any:
    """Attribute or key access — SDK objects and dict fixtures both work."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(attr)
    return getattr(obj, attr, None)


def _safe_err(e: Exception) -> str:
    """Error text for logs: type + message, with anything that looks like the
    key removed. Provider errors echo request metadata, never the Authorization
    header, but this is the belt-and-braces guard."""
    return _redact(f"{type(e).__name__}: {e}")


def _redact(text: str) -> str:
    key = (os.environ.get("LLM_API_KEY") or "").strip()
    if key and len(key) >= 8:
        text = text.replace(key, "***")
    return text

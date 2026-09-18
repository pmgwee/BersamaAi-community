"""Provider-neutral LLM adapter — the pipeline's ONLY door to a model provider.

Everything else in the pipeline (summarizer, news judge, /share card writer,
Serenity topic tagger) calls `structured_call()` here and never learns which
vendor is behind it. Swapping providers again should mean editing this file and
the LLM_* env vars, not another repo-wide refactor.

Current provider: **Codex with ChatGPT OAuth**, model `gpt-5.6-luna`, invoked
through `codex exec`. This lets the trusted pipeline VM use the owner's ChatGPT
subscription instead of a separately billed API key. The CLI owns OAuth token
storage and refresh; this module never reads or copies `auth.json`.

Reasoning effort: GPT-5.6 Luna supports low / medium / high / xhigh / max. We
run **max** by default, as requested. The Codex subprocess is non-agentic here:
shell, web, apps, and subagents are disabled; it receives only the prompt and a
JSON output schema inside an otherwise empty temporary directory.

Structured output: every caller wants ONE strict JSON object, so we keep the
proven idiom — declare a single function tool and force it with
`tool_choice="required"` — and validate the returned arguments locally in the
caller (`_coerce` / `TOPIC_BY_KEY` checks / topic enums). Providers do not
strictly enforce schemas, so that local validation stays the real contract; a
malformed or truncated reply raises `LLMError` and the caller's existing
retry / graceful-degradation path takes over.

Config:
    LLM_AUTH_MODE         codex (default, ChatGPT OAuth) | api (OpenAI API key)
    LLM_API_KEY           required only when LLM_AUTH_MODE=api
    LLM_BASE_URL          API mode only (default: https://api.openai.com/v1)
    LLM_MODEL             default: gpt-5.6-luna
    LLM_REASONING_EFFORT  default: max; empty/"off" omits it in API mode
    CODEX_CLI_PATH        optional path/name override for the `codex` executable

The key is read from the environment and never logged or echoed — error text
mentions the variable NAME only.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

from openai import OpenAI

PROVIDER_LABEL = "OpenAI"
DEFAULT_AUTH_MODE = "codex"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-5.6-luna"
CODEX_AUTH_SENTINEL = "__codex_chatgpt_oauth__"
CODEX_TIMEOUT_SECONDS = 300

# GPT-5.6 Luna reasoning depth. Anything outside this set falls back to max
# rather than failing a scheduled run on a typo.
DEFAULT_REASONING_EFFORT = "max"
REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max")

# The Responses API counts REASONING tokens against max_output_tokens, so the
# old chat-completions budgets (which only had to cover the visible tool JSON)
# are raised at the call sites. At high effort the model can think for thousands of
# tokens before emitting the tool call, and blowing the cap yields
# status="incomplete" (a hard LLMError), never a partial answer — so this floor
# is deliberately generous. Caps are not spend: unused budget costs nothing.
MIN_OUTPUT_TOKENS = 2048


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
    # NOTE: deliberately does NOT carry the reasoning effort. Callers splat this
    # dict (`summarize(..., **llm_creds())`), so every key here becomes a
    # required keyword on four feature functions; `structured_call` reads the
    # effort from the environment itself instead.
    e = os.environ if env is None else env
    auth_mode = resolve_auth_mode(e)
    return {
        # Existing feature functions accept exactly these three kwargs. The
        # sentinel means "Codex owns OAuth" and is never used as a credential.
        "api_key": (CODEX_AUTH_SENTINEL if auth_mode == "codex"
                    else (e.get("LLM_API_KEY") or "").strip()),
        "model": (e.get("LLM_MODEL") or "").strip() or DEFAULT_MODEL,
        "base_url": _normalize_base_url(e.get("LLM_BASE_URL") or DEFAULT_BASE_URL),
    }


def resolve_auth_mode(env: Optional[dict] = None) -> str:
    """Resolve the backend. Unknown values fall back to the OAuth default."""
    e = os.environ if env is None else env
    value = (e.get("LLM_AUTH_MODE") or DEFAULT_AUTH_MODE).strip().lower()
    return value if value in ("codex", "api") else DEFAULT_AUTH_MODE


def resolve_reasoning_effort(env: Optional[dict] = None) -> str:
    """Read LLM_REASONING_EFFORT -> one of REASONING_EFFORTS, or "" for "don't
    send the parameter at all".

    Unset means max (this project's choice). An explicitly empty value, or
    "off"/"none", opts out — the escape
    hatch for pointing LLM_MODEL at a non-reasoning model. An unrecognised value
    degrades to the default instead of failing the run.
    """
    e = os.environ if env is None else env
    raw = e.get("LLM_REASONING_EFFORT")
    if raw is None:
        return DEFAULT_REASONING_EFFORT
    value = raw.strip().lower()
    if value in ("", "off", "none"):
        return ""
    return value if value in REASONING_EFFORTS else DEFAULT_REASONING_EFFORT


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
    effort: Optional[str] = None,
    client: Optional[OpenAI] = None,
) -> dict:
    """Force `tool` and return its arguments as a dict.

    Raises LLMError on transport failure, a refused/absent tool call, a
    truncated ("incomplete") response, or non-JSON arguments — the caller
    decides whether that means retry, skip, or fall back.
    `effort` overrides the reasoning depth for one call; None resolves it from
    the environment (max unless LLM_REASONING_EFFORT says otherwise).
    `client` is an injection point for tests; production passes nothing.
    """
    if api_key == CODEX_AUTH_SENTINEL:
        return _codex_structured_call(
            system=system,
            user=user,
            tool=tool,
            model=model,
            effort=(resolve_reasoning_effort() if effort is None else effort)
            or DEFAULT_REASONING_EFFORT,
            timeout=timeout,
        )
    if not api_key:
        raise LLMError("LLM_API_KEY is missing — cannot call the model.")
    cl = client or build_client(api_key=api_key, base_url=base_url,
                                timeout=timeout, max_retries=max_retries)
    effort = resolve_reasoning_effort() if effort is None else effort
    extra: dict[str, Any] = {"reasoning": {"effort": effort}} if effort else {}
    try:
        resp = cl.responses.create(
            model=model,
            instructions=system,
            input=[{"role": "user", "content": user}],
            tools=[function_tool(tool)],
            tool_choice="required",   # one tool declared => this tool
            max_output_tokens=max(max_output_tokens, MIN_OUTPUT_TOKENS),
            **extra,
        )
    except Exception as e:  # noqa: BLE001 — transport/auth/rate-limit surprises
        raise LLMError(f"{PROVIDER_LABEL} API call failed: {_safe_err(e)}") from e
    return extract_tool_args(resp, tool["name"])


def _codex_structured_call(*, system: str, user: str, tool: dict, model: str,
                           effort: str, timeout: Optional[float]) -> dict:
    """Run one schema-constrained Codex turn using cached ChatGPT OAuth.

    Tooling is disabled because news candidates and shared URLs are untrusted
    text. The child receives a small allowlisted environment, so Discord and
    other pipeline secrets cannot be surfaced even if the model misbehaves.
    """
    configured = (os.environ.get("CODEX_CLI_PATH") or "codex").strip()
    executable = shutil.which(configured)
    if not executable:
        raise LLMError(
            "Codex CLI is not installed; install it and run `codex login --device-auth`."
        )

    prompt = (
        f"{system}\n\n"
        f"Task input:\n{user}\n\n"
        f"Return the arguments for `{tool['name']}` as the final JSON object. "
        f"{tool.get('description', '')} Do not use tools."
    )
    child_env = _codex_environment()
    with tempfile.TemporaryDirectory(prefix="bersama-codex-") as td:
        root = Path(td)
        schema_path = root / "schema.json"
        output_path = root / "output.json"
        schema_path.write_text(
            json.dumps(_strict_output_schema(tool["input_schema"])), encoding="utf-8"
        )
        cmd = [
            executable, "exec", "-",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--sandbox", "read-only",
            "--disable", "shell_tool",
            "--disable", "unified_exec",
            "--disable", "apps",
            "--model", model,
            "--config", f'model_reasoning_effort="{effort}"',
            "--config", 'web_search="disabled"',
            "--config", "agents.enabled=false",
            "--config", "allow_login_shell=false",
            "--output-schema", str(schema_path),
            "--output-last-message", str(output_path),
            "--color", "never",
            "--cd", str(root),
        ]
        try:
            completed = subprocess.run(
                cmd,
                input=prompt,
                text=True,
                encoding="utf-8",
                capture_output=True,
                env=child_env,
                timeout=timeout or CODEX_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"Codex OAuth call timed out after {e.timeout}s") from e
        except OSError as e:
            raise LLMError(f"Codex CLI failed to start: {_safe_err(e)}") from e

        if completed.returncode != 0:
            detail = _codex_failure_detail(completed.stderr, completed.stdout)
            raise LLMError(f"Codex OAuth call failed (exit {completed.returncode}): {detail}")
        try:
            data = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError) as e:
            raise MalformedOutput(f"{tool['name']} returned invalid JSON: {e}") from e
        if not isinstance(data, dict):
            raise MalformedOutput(f"{tool['name']} arguments were not a JSON object")
        return data


def _strict_output_schema(schema: Any) -> Any:
    """Adapt permissive function schemas to OpenAI strict structured output.

    Codex output schemas require every object to reject extra keys and list all
    properties as required. Formerly optional strings remain harmless because
    callers already treat an empty value as absent.
    """
    if isinstance(schema, list):
        return [_strict_output_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out = {key: _strict_output_schema(value) for key, value in schema.items()}
    if out.get("type") == "object":
        properties = out.get("properties") or {}
        out["properties"] = properties
        out["required"] = list(properties)
        out["additionalProperties"] = False
    return out


def _codex_environment() -> dict[str, str]:
    """Minimal environment for Codex auth/network/runtime; no pipeline secrets."""
    keep = {
        "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "COMSPEC",
        "PATHEXT", "LOCALAPPDATA", "APPDATA", "TEMP", "TMP", "TMPDIR",
        "LANG", "LC_ALL", "CODEX_HOME", "CODEX_CA_CERTIFICATE",
        "SSL_CERT_FILE", "SSL_CERT_DIR",
    }
    return {key: value for key, value in os.environ.items() if key.upper() in keep}


def _codex_failure_detail(stderr: str, stdout: str) -> str:
    """Keep a useful, secret-safe tail from CLI diagnostics."""
    lines = [line.strip() for line in f"{stderr}\n{stdout}".splitlines() if line.strip()]
    return _redact(" | ".join(lines[-8:]) if lines else "no diagnostic output")


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

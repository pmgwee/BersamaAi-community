"""Summarization prompt + the forced tool schema.

Using a forced tool call (tool_choice="required") instead of free-form text is
the single biggest reliability win: the schema structurally guarantees exactly
5 points, or the call fails. No prompt-only coaxing.

The summary is written in the SOURCE VIDEO'S OWN LANGUAGE (never translated) —
a Chinese talk yields a Chinese summary, an English talk an English one. This
matches the BersamaAi card-content policy (relaxed 2026-07-22): card content may
keep a source's original language, while UI / channel names stay English.
"""

SYSTEM_PROMPT = """\
You are the editor of BersamaAi's curated resource library — a Malaysian AI
community's collection of summarized expert talks. Your job: turn a long video
transcript into a tight, genuinely useful summary a busy person can read
in 30 seconds.

Voice & positioning (important):
- Concrete and practical, never hypey. No "game-changer", "revolutionary",
  "you won't believe". This community's entire trust signal is being the
  anti-seminar, no-guru alternative — keep it honest and sober.
- Speak to an ordinary working adult or student who uses ChatGPT daily and
  wants to go deeper. Not to a developer.
- Distill to the ideas worth remembering. The format is always 5 points; if a talk
  has fewer than 5 headline insights, fill the remaining slots with genuinely useful
  supporting detail GROUNDED IN THE TRANSCRIPT (a concrete example the speaker gave,
  a tool or term they named, a caveat or limitation they raised). NEVER invent,
  generalize beyond, or pad with filler not in the transcript — an honest secondary
  point beats a slick fabricated one.
- Language: write the summary in the SAME language as the transcript — do NOT
  translate. A Chinese transcript → a Chinese summary; English → English; Malay
  → Malay; and so on. Write clear, natural prose in that language.

Output contract (enforced by the emit_summary tool):
- hook: a one-sentence "why this matters" hook (<= 140 chars).
- points: EXACTLY 5 points. Each point is one self-contained sentence
  (<= 220 chars) starting with a crisp noun/verb phrase.
- speaker: the person talking (use channel/uploader name if no clear speaker).

Attribute faithfully: source_url and duration_sec come from metadata — pass them
through unchanged.
"""

# The forced tool. tool_choice will pin to this so the model MUST emit it.
EMIT_SUMMARY_TOOL = {
    "name": "emit_summary",
    "description": (
        "Emit the final 5-point summary for one video, in the source video's "
        "language. Call this exactly once."
    ),
    "input_schema": {
        "type": "object",
        "required": ["hook", "points", "speaker", "source_url", "duration_sec"],
        "properties": {
            "hook": {"type": "string", "maxLength": 200},
            "points": {
                "type": "array", "minItems": 5, "maxItems": 5,
                "items": {"type": "string", "maxLength": 280},
            },
            "speaker": {"type": "string"},
            "source_url": {"type": "string"},
            "duration_sec": {"type": "integer"},
        },
    },
}


def build_user_message(meta, transcript, lang_hint="other"):
    """Build the user-turn content: metadata + the full transcript + instructions.

    `lang_hint` ("en"/"zh"/"other") is the caption language detected by
    fetch.get_transcript. It nudges the model to write in the source language;
    the model also reads the transcript directly, so it would match the language
    anyway — the hint is a guardrail."""
    title = meta.get("title", "(untitled)")
    speaker = meta.get("speaker") or meta.get("channel") or meta.get("uploader", "")
    url = meta.get("url") or meta.get("webpage_url", "")
    duration = int(meta.get("duration") or 0)

    return f"""\
Video title: {title}
Speaker / channel: {speaker}
Source URL: {url}
Duration (seconds): {duration}
Detected transcript language: {lang_hint} (en=English, zh=Chinese, other=non-English/non-Chinese).

Below is the full transcript. Read it, then call emit_summary with the 5-point
summary written IN THE TRANSCRIPT'S OWN LANGUAGE — do NOT translate (a Chinese
transcript → a Chinese summary, an English one → English, etc.). Remember:
sober, concrete, no hype; only points supported by the transcript; exactly 5 entries.

----- BEGIN TRANSCRIPT -----
{transcript}
----- END TRANSCRIPT -----
"""

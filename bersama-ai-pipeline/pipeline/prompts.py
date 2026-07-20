"""Summarization prompt + the forced tool schema (English-only).

Using a forced tool call (tool_choice="required") instead of free-form text is
the single biggest reliability win: the schema structurally guarantees exactly
5 English points, or the call fails. No prompt-only coaxing.

This pipeline is English-only (matches the BersamaAi server decision, 2026-07-20).
The earlier bilingual EN+中文 design was retired when the server went English-only.
"""

SYSTEM_PROMPT = """\
You are the editor of BersamaAi's curated resource library — a Malaysian AI
community's collection of summarized expert talks. Your job: turn a long video
transcript into a tight, genuinely useful ENGLISH summary a busy person can read
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
- English only. Write clear, natural English (the community is English-speaking).

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
        "Emit the final English 5-point summary for one video. Call this exactly once."
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


def build_user_message(meta, transcript):
    """Build the user-turn content: metadata + the full transcript + instructions."""
    title = meta.get("title", "(untitled)")
    speaker = meta.get("speaker") or meta.get("channel") or meta.get("uploader", "")
    url = meta.get("url") or meta.get("webpage_url", "")
    duration = int(meta.get("duration") or 0)

    return f"""\
Video title: {title}
Speaker / channel: {speaker}
Source URL: {url}
Duration (seconds): {duration}

Below is the full transcript. Read it, then call emit_summary with the English
5-point summary. Remember: sober, concrete, no hype; only points supported by the
transcript; exactly 5 entries; English only.

----- BEGIN TRANSCRIPT -----
{transcript}
----- END TRANSCRIPT -----
"""

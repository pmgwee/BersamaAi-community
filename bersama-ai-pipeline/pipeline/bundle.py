"""Stage the "manual bundle" — the ready-to-paste content for Threads / Facebook /
other English platforms, which have no auto-post API.

Written under content/<YYYY-MM-DD>_<slug>_<id>/. Quality-gate failures go to
content/_review/, no-caption skips to content/_skipped/.

English-only (matches the BersamaAi server decision, 2026-07-20). The earlier
Chinese XHS bundle was retired when the server went English-only.
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from .summarize import Summary

CONTENT_ROOT = Path(__file__).resolve().parent.parent / "content"

EN_TAGS = "#AI #BersamaAi #OnePersonCompany #AITools #Productivity"


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    text = re.sub(r"[-\s]+", "-", text)
    return text[:60] or "video"


def _date_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def bundle_dir(summary: Summary, meta: dict) -> Path:
    slug = slugify(meta.get("title", "video"))
    vid = meta.get("id") or "novid"
    d = CONTENT_ROOT / f"{_date_str()}_{slug}_{vid}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _md(summary: Summary) -> str:
    lines = [f"# {summary.hook}", ""]
    lines += [f"{i}. {p}" for i, p in enumerate(summary.points, 1)]
    lines += ["", f"**Speaker:** {summary.speaker}",
              f"**Source:** {summary.source_url}"]
    return "\n".join(lines) + "\n"


def write_bundle(summary: Summary, meta: dict, transcript: str = "") -> Path:
    """Write the full manual bundle. Returns the bundle directory."""
    d = bundle_dir(summary, meta)

    # Canonical English summary
    (d / "summary.md").write_text(_md(summary), encoding="utf-8")

    # Threads / Facebook (English) — conversational, link-forward
    en = [summary.hook, ""]
    en += [f"• {p}" for p in summary.points]
    en += ["", f"{summary.speaker} — {summary.source_url}", EN_TAGS]
    (d / "post.threads.txt").write_text("\n".join(en) + "\n", encoding="utf-8")
    (d / "post.facebook.txt").write_text("\n".join(en) + "\n", encoding="utf-8")

    (d / "source.json").write_text(json.dumps({
        "meta": {k: meta.get(k) for k in ("title", "uploader", "channel", "duration", "webpage_url", "id")},
        "summary": summary.to_dict(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    if transcript:
        (d / "transcript.txt").write_text(transcript, encoding="utf-8")
    return d


def write_review(summary: Summary, meta: dict, reason: str, transcript: str = "") -> Path:
    """Quality-gate failure: park for human review, do NOT auto-publish."""
    d = CONTENT_ROOT / "_review" / f"{_date_str()}_{slugify(meta.get('title', 'video'))}_{meta.get('id') or 'novid'}"
    d.mkdir(parents=True, exist_ok=True)
    payload = {"reason": reason}
    if summary is not None:
        payload["summary"] = summary.to_dict()
    payload["meta"] = {k: meta.get(k) for k in ("title", "uploader", "webpage_url", "id", "duration")}
    (d / "review.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if transcript:
        (d / "transcript.txt").write_text(transcript, encoding="utf-8")
    return d


def write_skipped(meta: dict, reason: str) -> Path:
    """No-caption / too-long / etc.: log and move on."""
    d = CONTENT_ROOT / "_skipped"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{_date_str()}.log"
    line = f"[{_date_str()}] {meta.get('id')} :: {meta.get('title','?')} :: {reason}\n"
    with f.open("a", encoding="utf-8") as fh:
        fh.write(line)
    return f

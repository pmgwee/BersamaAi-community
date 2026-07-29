"""Processed-video dedup state, keyed by the 11-char YouTube video_id.

state/processed.json is committed back to the repo by the workflow, so a video
summarized once is never summarized again — across days and across runs.

Safety: a CORRUPT state file (e.g. a botched git merge left conflict markers in
the JSON) must NOT silently read as {} — that would re-publish the entire backlog
to Discord/Telegram. _load raises StateCorruptError instead; main() halts + DMs.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "processed.json"


class StateCorruptError(Exception):
    """Raised when state/processed.json can't be parsed — halts the run loudly."""


def _load() -> dict:
    if not STATE_PATH.exists():
        return {}
    raw = STATE_PATH.read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        # Snapshot the bad file for forensics, then raise so the run halts.
        snap = STATE_PATH.with_name(f"processed.corrupt-{_now_slug()}.json")
        try:
            snap.write_text(raw, encoding="utf-8")
        except OSError:
            pass
        raise StateCorruptError(f"{STATE_PATH} is corrupt and was moved to {snap.name}: {e}") from e


def _save(data: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)  # atomic on the same filesystem


def _now_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# A video is only "done" (deduped forever) at a PERMANENT end state:
#   published     — successfully posted (the happy path)
#   skipped_long  — deliberately over the duration cap (a video won't get shorter)
# Transient run-time failures — skipped_nocaption (captions unavailable because
# YouTube throttled the IP / no GROQ_API_KEY), review_summarize, review_short —
# must stay RETRYABLE; a later run can succeed past them. Deduping those forever
# is how new uploads silently vanished from the creator-watch feed (one bad day
# banned the video for life). Legacy entries with no status field are treated as
# published so the old backlog isn't suddenly re-published.
TERMINAL_STATUSES = {"published", "skipped_long"}


def is_processed(video_id: str) -> bool:
    entry = _load().get(video_id)
    if not entry:
        return False
    return entry.get("status", "published") in TERMINAL_STATUSES


def mark_processed(video_id: str, title: str, bundle_path: str, status: str = "published") -> None:
    data = _load()
    data[video_id] = {
        "title": title,
        "status": status,
        "bundle": bundle_path,
        "processed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _save(data)


def processed_ids() -> set[str]:
    """Only TERMINAL ids. Non-terminal (transient-failure) ids are intentionally
    excluded so run_scheduled gives them another shot instead of skipping them."""
    data = _load()
    return {vid for vid, entry in data.items()
            if entry.get("status", "published") in TERMINAL_STATUSES}

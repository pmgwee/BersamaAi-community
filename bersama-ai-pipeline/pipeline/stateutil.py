"""Shared state I/O for the engagement feedback loop.

news.py / engagement.py / preferences.py / digest.py all read + write small
state files under state/. This centralizes the I/O so every whole-JSON writer
is atomic (tmp + os.replace, mirroring state.py) and every JSONL reader is
defensive: a truncated final line from a mid-run crash is SKIPPED, not raised
on — losing one telemetry row is fine, halting the news run is not.

State files (all under state/, all committed by the news-digest workflow):
  posted_log.jsonl       append-only; one row per posted card (news.py)
  engagement.jsonl       append-only; one row per sweep snapshot (engagement.py)
  preferences.json       whole-file rewrite; the model (preferences.py)
  activity_baseline.json whole-file rewrite; 7d active-member count (engagement.py)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parent.parent / "state"

# Engagement-loop state files (single source of truth for the filenames).
POSTED_LOG = STATE_DIR / "posted_log.jsonl"
ENGAGEMENT_LOG = STATE_DIR / "engagement.jsonl"
PREFERENCES = STATE_DIR / "preferences.json"
ACTIVITY_BASELINE = STATE_DIR / "activity_baseline.json"


def _ensure_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def atomic_write_json(path: Path, data: dict) -> None:
    """Write a JSON file atomically: write to a .tmp sibling, then os.replace.
    Safe against a mid-write crash (the old file is untouched until the replace)
    and safe under the serialized `pipeline-state` workflow group."""
    _ensure_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default):
    """Read+parse a JSON file; return `default` if missing or unparseable."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def append_jsonl(path: Path, row: dict) -> None:
    """Append one JSON object as a single line. Opens-and-closes per call so
    back-to-back serialized runs never interleave partial lines."""
    _ensure_dir()
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def read_jsonl(path: Path):
    """Yield each parsed JSON row from a JSONL file. Unparseable lines (e.g. a
    truncated final line from a crash) are silently skipped — defensive, unlike
    state.py's strict _load which raises. Telemetry must never halt the run."""
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def rewrite_jsonl(path: Path, rows) -> None:
    """Atomically rewrite a JSONL file from an iterable of rows (retention prune)."""
    _ensure_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.replace(tmp, path)

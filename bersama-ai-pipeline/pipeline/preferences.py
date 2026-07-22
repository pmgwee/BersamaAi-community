"""Community preference model for the news engine — an ε-greedy contextual bandit.

Pure computation, no network. Reads state/posted_log.jsonl + state/engagement.jsonl
(joined on message_id), aggregates an exponentially-decayed mean reward per "arm"
along three dimensions (topic / source / category), and writes state/preferences.json.

Run as `python -m pipeline.preferences` at the start of each news run (before
gather), or imported via load_preferences() by news.py.

Design (see ENGAGEMENT-LOOP-PLAN.md §3): no model training — just decayed
averages with a 14-day half-life so recent taste dominates and old taste fades.
Every event is also kept raw in the JSONL logs, so a learned ranker can be
trained later from day-one data when volume justifies it (plan §8).

Kill switch: PREFS_ENABLED env (default "false") AND n_events >= MIN_EVENTS must
both hold, or load_preferences() reports enabled=False and the actuator no-ops —
byte-identical to today's static engine.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .stateutil import (
    POSTED_LOG, ENGAGEMENT_LOG, PREFERENCES,
    read_jsonl, atomic_write_json, read_json,
)

HALF_LIFE_DAYS = 14            # recent taste dominates; old taste fades
MIN_EVENTS = 20                # below this the actuator stays dormant (no learning from noise)

# Per-post reward weights. reward is computed at SWEEP time (engagement.py) and
# stored in engagement.jsonl, so these constants are re-declared there as the
# source of truth for the reward; mirrored here only for the digest/docs.
W_REACT_UP = 2.0     # 👍 useful
W_REACT_FIRE = 3.0   # 🔥 amazing
W_REACT_MEH = -2.0   # 👎 not-for-me
W_REACT_OTHER = 1.0  # any other reaction
W_REPLY = 4.0        # a reply (deeper engagement than a react)
REWARD_MIN, REWARD_MAX = -3.0, 5.0
DEFAULT_ACTIVE_BASELINE = 10
MAX_ENGAGEMENT_AGE_DAYS = 60   # recompute window; >60d contributes <5% to a 14d-half-life EMA

# Dimensions over which preference scores are tracked. Each candidate carries
# one value per dimension (topic from the judge; source + category from the post).
DIMENSIONS = ("topic", "source", "category")


@dataclass
class Preferences:
    """The bandit's state. Always returned by load_preferences() (disabled if
    anything is off), so every caller branches purely on `prefs.enabled`."""
    enabled: bool = False
    n_events: int = 0
    updated_at: str = ""
    half_life_days: int = HALF_LIFE_DAYS
    topic: dict = field(default_factory=dict)
    source: dict = field(default_factory=dict)
    category: dict = field(default_factory=dict)
    exemplars: dict = field(default_factory=dict)
    profile_section: str = ""

    def score(self, dimension: str, key: str, default: float = 0.0) -> float:
        """Preference score for one arm, e.g. prefs.score('topic', 'coding')."""
        return float(self.__dict__.get(dimension, {}).get(key, default))


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_days(posted_at: str, now: datetime) -> float | None:
    """Days between a post's posted_at (ISO Z) and `now`. None if unparseable."""
    if not posted_at:
        return None
    try:
        dt = datetime.fromisoformat(str(posted_at).replace("Z", "+00:00"))
        return max((now - dt).total_seconds() / 86400.0, 0.0)
    except Exception:  # noqa: BLE001
        return None


def load_preferences() -> Preferences:
    """Load state/preferences.json and apply the kill-switch + evidence guard.
    Always returns a Preferences object — disabled unless BOTH PREFS_ENABLED is
    "true" AND n_events >= MIN_EVENTS. The profile_section is empty when disabled
    so the judge sees today's prompt verbatim (byte-identical selection)."""
    raw = read_json(PREFERENCES, {}) or {}
    n = int(raw.get("n_events", 0) or 0)
    enabled = os.environ.get("PREFS_ENABLED", "false").lower() == "true" and n >= MIN_EVENTS
    return Preferences(
        enabled=enabled,
        n_events=n,
        updated_at=raw.get("updated_at", ""),
        half_life_days=int(raw.get("half_life_days", HALF_LIFE_DAYS)),
        topic=raw.get("topic", {}) or {},
        source=raw.get("source", {}) or {},
        category=raw.get("category", {}) or {},
        exemplars=raw.get("exemplars", {}) or {},
        profile_section=build_profile_section(raw) if enabled else "",
    )


def build_profile_section(raw: dict) -> str:
    """The 'taste memo' appended to the judge's system prompt (plan §4.2).
    Additive — it never replaces the trending-first directives above it. The
    last line is load-bearing: preferences only break ties, never override heat.
    Returns '' when there's too little signal to say anything useful."""
    topic = raw.get("topic", {}) or {}
    source = raw.get("source", {}) or {}
    category = raw.get("category", {}) or {}
    n = int(raw.get("n_events", 0) or 0)
    updated = raw.get("updated_at", "recently")
    if n < MIN_EVENTS or not topic:
        return ""

    def top(dim: dict, n: int) -> list[tuple[str, float]]:
        return sorted(((k, float(v)) for k, v in dim.items() if v), key=lambda kv: kv[1], reverse=True)[:n]

    def bottom(dim: dict, n: int) -> list[tuple[str, float]]:
        return sorted(((k, float(v)) for k, v in dim.items() if v), key=lambda kv: kv[1])[:n]

    lines = [
        f"\n\nCOMMUNITY PREFERENCE PROFILE (learned from {n} member-reaction events; updated {updated}):",
    ]
    hot_t = top(topic, 3)
    hot_c = top(category, 3)
    if hot_t or hot_c:
        bits = []
        if hot_c:
            bits.append(" / ".join(f"{c} ({v:+.1f})" for c, v in hot_c))
        if hot_t:
            bits.append("in " + " / ".join(f"{t} ({v:+.1f})" for t, v in hot_t))
        lines.append("- This community engages MOST with: " + "; ".join(bits) + ".")
    cold_t = bottom(topic, 2)
    cold_c = bottom(category, 2)
    cold = [(k, v) for k, v in (cold_c + cold_t) if v < 0][:3]
    if cold:
        lines.append("- Engages LEAST with: " + ", ".join(f"{k} ({v:+.1f})" for k, v in cold) + ".")
    hot_s = top(source, 3)
    if hot_s:
        lines.append("- Sources that land well: " + ", ".join(f"{s} ({v:+.1f})" for s, v in hot_s) + ".")

    exemplars = raw.get("exemplars", {}) or {}
    wins = (exemplars.get("top") or [])[:2]
    misses = (exemplars.get("bottom") or [])[:1]
    if wins:
        lines.append("- Recent hits: " + " · ".join(f'"{w.get("headline", "")[:80]}"' for w in wins))
    if misses:
        lines.append("- Recent misses: " + " · ".join(f'"{w.get("headline", "")[:80]}"' for w in misses))

    lines.append(
        "When choosing between two comparably-hot items, prefer the profile. NEVER use the "
        "profile to post a lukewarm item over a genuinely viral one — heat still wins."
    )
    return "\n".join(lines)


def _ema_by_dimension(events: list[dict]) -> dict[str, dict]:
    """Exponentially-decayed mean reward per arm, per dimension.
    weight_i = 0.5 ** (age_days_i / HALF_LIFE_DAYS); score_arm = Σ(w·r)/Σ(w)."""
    half = HALF_LIFE_DAYS
    out: dict[str, dict] = {dim: {} for dim in DIMENSIONS}
    acc: dict[str, dict[str, list[float]]] = {dim: {} for dim in DIMENSIONS}
    for e in events:
        w = 0.5 ** (e["age_days"] / half) if half > 0 else 1.0
        r = e["reward"]
        for dim in DIMENSIONS:
            key = e.get(dim, "")
            if not key:
                continue
            wa = acc[dim].setdefault(key, [0.0, 0.0])  # [weighted_sum, weight_sum]
            wa[0] += w * r
            wa[1] += w
    for dim in DIMENSIONS:
        for key, (ws, wsum) in acc[dim].items():
            out[dim][key] = round(ws / wsum, 3) if wsum > 0 else 0.0
    return out


def compute() -> Preferences:
    """Recompute preferences.json from raw posted_log + engagement logs.
    Recompute-from-raw (not incremental EMA state) because volume is tiny and it
    eliminates drift/increment bugs. Idempotent."""
    now = datetime.now(timezone.utc)

    # Index posts by message_id (the join key).
    posts: dict[str, dict] = {}
    for row in read_jsonl(POSTED_LOG):
        mid = row.get("message_id")
        if mid:
            posts[str(mid)] = row

    # Join engagement snapshots → in-window events with reward + dimension values.
    events: list[dict] = []
    for row in read_jsonl(ENGAGEMENT_LOG):
        mid = str(row.get("message_id") or "")
        p = posts.get(mid)
        if not p:
            continue
        reward = row.get("reward")
        if reward is None:
            continue
        age_days = _age_days(p.get("posted_at", ""), now)
        if age_days is None or age_days > MAX_ENGAGEMENT_AGE_DAYS:
            continue
        events.append({
            "topic": p.get("topic", ""),
            "source": p.get("source", ""),
            "category": p.get("category", ""),
            "headline": p.get("headline", ""),
            "reward": float(reward),
            "age_days": age_days,
        })

    dims = _ema_by_dimension(events)
    n_events = len(events)

    # Exemplars: concrete recent wins/losses for the judge memo.
    ranked = sorted(events, key=lambda e: e["reward"], reverse=True)
    exemplars = {
        "top": [{"headline": e["headline"], "reward": e["reward"]} for e in ranked[:5] if e["headline"]],
        "bottom": [{"headline": e["headline"], "reward": e["reward"]} for e in ranked[-3:] if e["headline"]],
    }

    out = {
        "updated_at": _now_iso(),
        "half_life_days": HALF_LIFE_DAYS,
        "n_events": n_events,
        "topic": dims["topic"],
        "source": dims["source"],
        "category": dims["category"],
        "exemplars": exemplars,
    }
    atomic_write_json(PREFERENCES, out)

    enabled = os.environ.get("PREFS_ENABLED", "false").lower() == "true" and n_events >= MIN_EVENTS
    print(f"[preferences] n_events={n_events} → {'ENABLED' if enabled else 'dormant (PREFS_ENABLED=false or < MIN_EVENTS)'}")
    if dims["topic"]:
        print(f"[preferences] topic scores: {dims['topic']}")
    return Preferences(
        enabled=enabled, n_events=n_events, updated_at=out["updated_at"],
        topic=dims["topic"], source=dims["source"], category=dims["category"],
        exemplars=exemplars, profile_section=build_profile_section(out) if enabled else "",
    )


def main() -> int:
    compute()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

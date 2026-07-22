"""Local verification for the engagement loop (no network, no real state).

Run:  cd bersama-ai-pipeline && python verify_engagement.py
Exits non-zero on any failed assertion. Safe to delete after the feature ships.
"""
import os
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timezone

# Force UTF-8 stdio so the → / emoji in labels print on Windows (cp1252 default).
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

os.environ.setdefault("PREFS_ENABLED", "false")

from pipeline import news, preferences, engagement, digest, stateutil  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


# ── Test 1: quota/cap transforms are byte-identical at pref=0 and bounded ─────
print("\n[1] quota/cap transforms")
check("topic quota pref=0 → 8 (byte-identical)", news._quota_for_topic(0.0) == 8)
check("topic quota pref=+3 → 14 (ceil)", news._quota_for_topic(3.0) == 14, str(news._quota_for_topic(3.0)))
check("topic quota pref=-5 → 4 (floor)", news._quota_for_topic(-5.0) == 4, str(news._quota_for_topic(-5.0)))
check("topic quota pref=+1 → 10", news._quota_for_topic(1.0) == 10, str(news._quota_for_topic(1.0)))
check("shared quota base=8 pref=0 → 8", news._quota_for_shared(8, 0.0) == 8)
check("shared quota HF base=3 pref=0 → 3", news._quota_for_shared(3, 0.0) == 3)
check("shared quota base=8 pref=-3 → 6 (floor base-2)", news._quota_for_shared(8, -3.0) == 6, str(news._quota_for_shared(8, -3.0)))
check("cap pref=0 → 3 (byte-identical)", news._cap_for_topic(0.0) == 3)
check("cap pref=+2 → 4 (ceil)", news._cap_for_topic(2.0) == 4, str(news._cap_for_topic(2.0)))
check("cap pref=-3 → 1 (floor)", news._cap_for_topic(-3.0) == 1, str(news._cap_for_topic(-3.0)))

# ── Test 2: reward math ───────────────────────────────────────────────────────
print("\n[2] reward + reaction parsing")
r = engagement._reward({"👍": 2, "🔥": 1, "👎": 0, "other": 1}, 1, 10)
# 2*2 + 3*1 + (-2)*0 + 1*1 + 4*1 = 4+3+0+1+4 = 12 ; /10 = 1.2
check("reward(2👍,1🔥,1other,1reply,baseline10) == 1.2", abs(r - 1.2) < 1e-6, str(r))
rhi = engagement._reward({"🔥": 10}, 0, 10)  # 3*10/10 = 3.0
check("reward(10🔥) == 3.0", abs(rhi - 3.0) < 1e-6, str(rhi))
rmeh = engagement._reward({"👎": 5}, 0, 10)  # -2*5/10 = -1.0
check("reward(5👎) == -1.0 (meh weight via new emoji)", abs(rmeh - (-1.0)) < 1e-6, str(rmeh))
rlo = engagement._reward({"👎": 20}, 0, 10)  # -2*20/10 = -4.0 → clamp -3
check("reward(20👎) clamps to -3.0", abs(rlo - (-3.0)) < 1e-6, str(rlo))

# reaction parsing: bot seed (me=True) subtracted; member reactions counted
msg = {"reactions": [
    {"emoji": {"name": "👍"}, "count": 3, "me": True},    # 1 bot + 2 members
    {"emoji": {"name": "🔥"}, "count": 1, "me": True},    # bot only → 0
    {"emoji": {"name": "👎"}, "count": 2, "me": True},    # 1 bot + 1 member
    {"emoji": {"name": "🎉", "id": None}, "count": 2, "me": False},  # 2 members, other
]}
parsed = engagement._reactions(msg)
check("reactions subtract bot seed (👍 3,me → 2)", parsed["👍"] == 2, str(parsed))
check("reactions 🔥 bot-only → 0", parsed["🔥"] == 0, str(parsed))
check("reactions 👎 2,me → 1", parsed["👎"] == 1, str(parsed))
check("reactions other bucket = 2", parsed["other"] == 2, str(parsed))

# ── Test 3: _post ?wait=true URL building + 200/204 handling ──────────────────
print("\n[3] _post wait=true")
import requests as _real_requests  # noqa: E402

calls = []


class _FakeResp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload
        self.text = ""

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


def _fake_post(url, json=None, timeout=None, **kw):
    calls.append(url)
    if "nojson" in url:
        return _FakeResp(200)  # 200 but json() raises ValueError → _post returns None
    if "/204" in url:
        return _FakeResp(204)
    return _FakeResp(200, {"id": "1", "channel_id": "2"})


news.requests.post = _fake_post
msg = news._post("https://discord.com/api/webhooks/123/abc", {})
check("_post returns message dict on 200", msg and msg.get("id") == "1", str(msg))
check("_post appends ?wait=true", calls[-1].endswith("?wait=true"), calls[-1])
msg2 = news._post("https://discord.com/api/webhooks/123/abc?thread_id=x", {})
check("_post uses &wait=true when URL has query", "thread_id=x&wait=true" in calls[-1], calls[-1])
msg3 = news._post("https://discord.com/api/webhooks/123/204", {})
check("_post returns None on 204", msg3 is None, str(msg3))
msg4 = news._post("https://discord.com/api/webhooks/123/nojson", {})
check("_post returns None on 200-without-json", msg4 is None, str(msg4))
news.requests.post = _real_requests.post

# ── Test 4: preferences compute + EMA + guards (synthetic state in temp dir) ──
print("\n[4] preferences model (EMA + guards)")
tmp = Path(tempfile.mkdtemp(prefix="engtest_"))
paths = {
    "POSTED_LOG": tmp / "posted_log.jsonl",
    "ENGAGEMENT_LOG": tmp / "engagement.jsonl",
    "PREFERENCES": tmp / "preferences.json",
    "ACTIVITY_BASELINE": tmp / "activity_baseline.json",
}
for mod in (preferences, engagement, digest, news, stateutil):
    for k, v in paths.items():
        setattr(mod, k, v)


def _iso(): return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def emit(topic, source, category, reward, n, mid_start):
    for i in range(n):
        mid = f"{mid_start}{i}"
        stateutil.append_jsonl(paths["POSTED_LOG"], {
            "message_id": mid, "channel_id": "1", "channel": "#x", "topic": topic,
            "category": category, "source": source, "source_url": "u", "headline": f"{topic} {i}",
            "score": 100, "star_velocity": 0, "posted_at": _iso(),
        })
        stateutil.append_jsonl(paths["ENGAGEMENT_LOG"], {
            "message_id": mid, "sweep_at": _iso(), "age_h": 26,
            "reactions": {"👍": 0, "🔥": 0, "😐": 0, "other": 0}, "reply_count": 0, "reward": reward,
        })


emit("coding", "github", "LAUNCH", 2.0, 30, "c")
emit("research_study", "official", "UPDATE", -1.0, 30, "r")

os.environ["PREFS_ENABLED"] = "false"
prefs_off = preferences.compute()
check("compute: n_events == 60", prefs_off.n_events == 60, str(prefs_off.n_events))
check("compute: coding score > research score",
      prefs_off.score("topic", "coding") > prefs_off.score("topic", "research_study"),
      f"coding={prefs_off.score('topic','coding')} research={prefs_off.score('topic','research_study')}")
check("compute: coding score positive", prefs_off.score("topic", "coding") > 0, str(prefs_off.score("topic", "coding")))
check("compute: research score negative", prefs_off.score("topic", "research_study") < 0, str(prefs_off.score("topic", "research_study")))
check("PREFS_ENABLED=false → disabled", preferences.load_preferences().enabled is False)

os.environ["PREFS_ENABLED"] = "true"
prefs_on = preferences.load_preferences()
check("PREFS_ENABLED=true + 60 events → enabled", prefs_on.enabled is True)
check("profile_section non-empty when enabled", len(prefs_on.profile_section) > 0)
check("profile keeps 'heat still wins' invariant", "heat still wins" in prefs_on.profile_section)
check("profile is additive (mentions COMMUNITY PREFERENCE PROFILE)", "COMMUNITY PREFERENCE PROFILE" in prefs_on.profile_section)

# Guard: < MIN_EVENTS stays dormant even with PREFS_ENABLED=true
paths["ENGAGEMENT_LOG"].unlink()
paths["POSTED_LOG"].unlink()
emit("coding", "github", "LAUNCH", 2.0, 5, "z")  # only 5 events
preferences.compute()
check("< MIN_EVENTS → disabled even if PREFS_ENABLED=true", preferences.load_preferences().enabled is False)

# ── Test 5: digest builds without crashing on empty + populated state ─────────
print("\n[5] digest formatting")
os.environ["PREFS_ENABLED"] = "false"
# repopulate for digest
paths["ENGAGEMENT_LOG"].unlink()
paths["POSTED_LOG"].unlink()
emit("coding", "github", "LAUNCH", 3.5, 8, "d")
emit("creative_video", "HN", "UPDATE", -1.5, 4, "v")
preferences.compute()
text = digest.build_text()
check("digest text is non-empty", len(text) > 100)
check("digest mentions Top posts", "Top" in text)
check("digest mentions quota allocation", "quota" in text.lower())
DIGEST_OUT = digest.build_text()
print("\n----- digest sample -----\n" + DIGEST_OUT + "\n-------------------------")

# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\n{'='*48}")
if FAILURES:
    print(f"❌ {len(FAILURES)} check(s) FAILED: {FAILURES}")
    sys.exit(1)
print("✅ all engagement-loop checks passed")
sys.exit(0)

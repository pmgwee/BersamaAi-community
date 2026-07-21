"""Pipeline entrypoint.

Usage:
  python -m pipeline.main --mode url --url <YOUTUBE_URL>            # on-demand
  python -m pipeline.main --mode scheduled                          # daily playlist scan
  python -m pipeline.main --mode url --url <URL> --dry-run          # print, don't post
  python -m pipeline.main --mode url --url <URL> --stub-summary     # LOCAL TEST (no API key)

Flow per video: fetch -> summarize -> quality gate -> publish -> bundle -> mark state.
Failures are routed to content/_review or content/_skipped and DM'd to the maintainer.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()  # local .env; no-op in CI where env comes from secrets
except ImportError:
    pass

# Force UTF-8 stdio so Chinese characters print correctly on Windows (cp1252 default).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 — reconfigure unavailable on some builds
        pass

from . import fetch, publish, state, bundle
from .summarize import summarize, stub_summary, SummarizeError, Summary
from .news import run_news, post_url_as_news

# Guards
MAX_PER_RUN = 5                 # cap scheduled backlog so we never blow the 30-min job
TRANSCRIPT_FLOOR_CHARS = 1500   # below this a >10min video looks like a bad auto-caption
SHORT_VIDEO_SEC = 600           # 10 min
RECENCY_DAYS = 3                # creator-watch: only summarize uploads from the last N days


def cfg(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def llm_creds() -> dict:
    """Resolve LLM credentials, preferring the ZAI_* / GLM_MODEL names and
    falling back to GLM_API_KEY / GLM_BASE_URL / SUMMARY_MODEL. Defaults match
    the maintainer's Z.ai coding-plan setup (glm-5.2)."""
    base = cfg("ZAI_BASE_URL") or cfg("GLM_BASE_URL", "https://api.z.ai/api/coding/paas/v4/")
    return {
        "api_key": cfg("ZAI_API_KEY") or cfg("GLM_API_KEY"),
        "model": cfg("GLM_MODEL") or cfg("SUMMARY_MODEL", "glm-5.2"),
        "base_url": base.rstrip("/") + "/",   # OpenAI SDK expects a trailing slash
    }


def alert(msg: str, dry_run: bool) -> None:
    publish.alert(
        cfg("TELEGRAM_BOT_TOKEN"), cfg("TELEGRAM_DM_CHAT_ID"),
        f"⚠️ BersamaAi pipeline: {msg}", dry_run=dry_run,
    )


def _mark(vid: str, title: str, path: str, status: str, dry_run: bool) -> None:
    """Record dedup state — but NEVER in a dry-run (a dry-run didn't really finish it)."""
    if dry_run:
        return
    state.mark_processed(vid, title, path, status=status)


# ── per-video processing ────────────────────────────────────────────────────

def process_video(url: str, *, dry_run: bool, stub: bool) -> str:
    """Process one video. Returns a one-line status string."""
    print(f"\n=== processing {url} ===")

    # 1. metadata
    try:
        meta = fetch.get_video_info(url)
    except fetch.FetchError as e:
        alert(f"fetch failed for {url}: {e}", dry_run)
        return f"FETCH_FAILED {url}"

    vid = meta.get("id") or url
    title = meta.get("title", "(untitled)")
    duration = int(meta.get("duration") or 0)
    max_min = int(cfg("MAX_DURATION_MIN", "60") or 60)

    # 2. duration cap
    if duration and duration > max_min * 60:
        f = bundle.write_skipped(meta, f"too long ({duration // 60}min > {max_min}min cap)")
        alert(f"skipped (too long): {title} — {duration // 60}min", dry_run)
        _mark(vid, title, str(f), "skipped_long", dry_run)
        return f"SKIPPED_LONG {vid}"

    # 3. dedup
    if state.is_processed(vid):
        print(f"already processed: {vid} — skipping")
        return f"DEDUPED {vid}"

    # 4. transcript
    transcript, lang_hint = fetch.get_transcript(meta)
    if not transcript:
        f = bundle.write_skipped(meta, "no captions available (Whisper fallback is v1.1)")
        alert(f"skipped (no captions): {title}", dry_run)
        _mark(vid, title, str(f), "skipped_nocaption", dry_run)
        return f"SKIPPED_NOCAPTION {vid}"

    # 5. summarize
    try:
        summary: Summary = (
            stub_summary(meta) if stub
            else summarize(meta, transcript, **llm_creds())
        )
    except SummarizeError as e:
        d = bundle.write_review(None, meta, f"summarize failed: {e}", transcript)
        alert(f"summary failed for {title}: {e}", dry_run)
        _mark(vid, title, str(d), "review_summarize", dry_run)
        return f"REVIEW_SUMMARIZE {vid}"

    # 6. quality gate — short transcript on a long video = likely bad auto-caption
    if duration > SHORT_VIDEO_SEC and len(transcript) < TRANSCRIPT_FLOOR_CHARS:
        d = bundle.write_review(summary, meta,
                                f"transcript suspiciously short ({len(transcript)} chars "
                                f"for a {duration // 60}min video)", transcript)
        alert(f"quality gate (short transcript): {title}", dry_run)
        _mark(vid, title, str(d), "review_short", dry_run)
        return f"REVIEW_SHORT {vid}"

    # 7. stage the manual bundle (always — even in dry-run, so you can inspect)
    bdir = bundle.write_bundle(summary, meta, transcript)
    print(f"bundle written: {bdir}")

    # 8. publish
    try:
        if cfg("DISCORD_WEBHOOK_URL"):
            publish.send_discord(
                cfg("DISCORD_WEBHOOK_URL"),
                publish.build_discord_payload(summary, meta),
                dry_run=dry_run,
            )
        publish.send_telegram(
            cfg("TELEGRAM_BOT_TOKEN"), cfg("TELEGRAM_CHANNEL_ID"),
            summary, dry_run=dry_run,
        )
    except Exception as e:  # noqa: BLE001 — publish failure shouldn't lose the bundle
        alert(f"publish failed for {title}: {e} (bundle saved to {bdir}; will retry next run)", dry_run)
        # Intentionally NOT marking processed: a transient 5xx/429 should retry on the
        # next run, not be silently dropped from ever auto-posting. Re-summarize cost is ~$0.01.
        return f"PUBLISH_FAILED {vid}"

    # 9. mark processed
    _mark(vid, title, str(bdir), "published", dry_run)
    return f"PUBLISHED {vid}"


# ── scheduled scan (creator-watch) ──────────────────────────────────────────

def _is_recent(upload_date: str, days: int = RECENCY_DAYS) -> bool:
    """Creator-watch: only process videos uploaded in the last `days` days (yt-dlp
    gives upload_date as YYYYMMDD). Unknown/unparseable => allow (never block on it)."""
    if not upload_date or len(upload_date) < 8:
        return True
    try:
        from datetime import datetime, timezone, timedelta
        d = datetime.strptime(upload_date[:8], "%Y%m%d").replace(tzinfo=timezone.utc)
        return d >= datetime.now(timezone.utc) - timedelta(days=days)
    except Exception:  # noqa: BLE001
        return True


def read_playlists() -> list[str]:
    p = Path(__file__).resolve().parent.parent / "playlists.txt"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def run_scheduled(*, dry_run: bool, stub: bool) -> list[str]:
    playlists = read_playlists()
    if not playlists:
        print("no playlists configured in playlists.txt — nothing to scan.")
        return []
    results = []
    done = state.processed_ids()
    processed_this_run = 0
    for pl in playlists:
        print(f"\n--- playlist: {pl}")
        try:
            entries = fetch.list_playlist_entries(pl)
        except fetch.FetchError as e:
            alert(f"could not list playlist {pl}: {e}", dry_run)
            results.append(f"PLAYLIST_FAIL {pl}")
            continue
        print(f"{len(entries)} entries; {len(done)} already processed globally")
        for e in entries:
            if e["id"] in done:
                continue
            if not _is_recent(e.get("upload_date")):
                continue   # creator-watch: skip the backlog; only NEW uploads
            if processed_this_run >= MAX_PER_RUN:
                print(f"hit MAX_PER_RUN={MAX_PER_RUN}; remaining entries wait for next run")
                break
            results.append(process_video(e["url"], dry_run=dry_run, stub=stub))
            processed_this_run += 1
        if processed_this_run >= MAX_PER_RUN:
            break
    return results


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="BersamaAi summarization pipeline")
    ap.add_argument("--mode", required=True, choices=["scheduled", "url", "news", "share"])
    ap.add_argument("--url", help="a single video URL (mode=url)")
    ap.add_argument("--dry-run", action="store_true", help="print payloads, don't post")
    ap.add_argument("--stub-summary", action="store_true",
                    help="LOCAL TEST: use a canned summary, skip the LLM call")
    ap.add_argument("--stub-news", action="store_true",
                    help="LOCAL TEST (mode=news): canned news item, skip the LLM call")
    args = ap.parse_args(argv)

    if args.mode in ("url", "share") and not args.url:
        ap.error("--url is required when --mode=url")

    try:
        if args.mode == "url":
            results = [process_video(args.url, dry_run=args.dry_run, stub=args.stub_summary)]
        elif args.mode == "news":
            creds = llm_creds()
            results = run_news(
                dry_run=args.dry_run, stub=args.stub_news,
                api_key=creds["api_key"], model=creds["model"], base_url=creds["base_url"],
                webhook_url=cfg("DISCORD_NEWS_WEBHOOK_URL") or cfg("DISCORD_WEBHOOK_URL"),
                alert_fn=alert,
            )
        elif args.mode == "share":
            creds = llm_creds()
            results = [post_url_as_news(
                args.url, api_key=creds["api_key"], model=creds["model"],
                base_url=creds["base_url"], dry_run=args.dry_run, alert_fn=alert,
            )]
        else:
            results = run_scheduled(dry_run=args.dry_run, stub=args.stub_summary)
    except state.StateCorruptError as e:
        # Never silently proceed — a corrupt state file would re-publish the whole backlog.
        alert(f"STATE CORRUPT — halting to avoid re-publishing backlog: {e}", args.dry_run)
        print(f"\n!!! STATE CORRUPT — HALTING: {e}", file=sys.stderr)
        return 2

    print("\n=== SUMMARY ===")
    for r in results:
        print(" -", r)

    # Red the Actions run only if we attempted videos and NONE produced a good outcome
    # (a single transient failure should not fail the whole daily run).
    OK = {"PUBLISHED", "SKIPPED_LONG", "SKIPPED_NOCAPTION", "REVIEW_SHORT", "NEWS_POSTED"}
    SKIP = ("DEDUPED", "NEWS_DEDUPED", "NEWS_NOTHING_TO_POST", "NEWS_NO_CANDIDATES")
    attempted = [r.split(maxsplit=1)[0] for r in results if not r.startswith(SKIP)]
    if attempted and not any(s in OK for s in attempted):
        print("\n!!! run produced no successful outcomes", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

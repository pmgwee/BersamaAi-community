"""Tests for the caption-less-video audio fallback chain (rungs 2 and 3).

Run from bersama-ai-pipeline/:   python -m unittest discover -s tests -v
No network: every HTTP call is served by a fake `requests` module.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import ytaudio  # noqa: E402
from pipeline.fetch import ydl_network_opts, video_id_from_url  # noqa: E402


class _Resp:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, *, payload=None, chunks=None, headers=None, status=200):
        self._payload, self._chunks = payload, chunks or []
        self.headers = headers or {"Content-Type": "audio/webm"}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=0):
        return iter(self._chunks)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeRequests:
    """Routes GETs by URL substring; records every call."""

    def __init__(self, routes: dict):
        self.routes, self.calls = routes, []

    def get(self, url, **kw):
        self.calls.append(url)
        for frag, resp in self.routes.items():
            if frag in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise RuntimeError(f"unrouted URL: {url}")


class _patched:
    def __init__(self, module, name, value):
        self.module, self.name, self.value = module, name, value

    def __enter__(self):
        self.old = getattr(self.module, self.name)
        setattr(self.module, self.name, self.value)
        return self.value

    def __exit__(self, *exc):
        setattr(self.module, self.name, self.old)
        return False


class _env:
    """Set/clear env vars for one block."""

    def __init__(self, **kw):
        self.kw = kw

    def __enter__(self):
        self.old = {k: os.environ.get(k) for k in self.kw}
        for k, v in self.kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, *exc):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


PIPED_PAYLOAD = {"audioStreams": [
    {"url": "https://pipedproxy.test/hi", "bitrate": 160000, "format": "WEBMA_OPUS"},
    {"url": "https://pipedproxy.test/lo", "bitrate": 48000, "format": "WEBMA_OPUS"},
    {"url": "", "bitrate": 1},                     # unusable: no URL
]}

INVIDIOUS_PAYLOAD = {"adaptiveFormats": [
    {"itag": "137", "type": "video/mp4", "bitrate": "1000"},        # not audio
    {"itag": "251", "type": "audio/webm; codecs=opus", "bitrate": "130000"},
    {"itag": "139", "type": "audio/mp4; codecs=mp4a", "bitrate": "48000"},
]}


class TestMirrorList(unittest.TestCase):
    def test_defaults_parse_into_kind_and_host(self):
        # YT_AUDIO_DISCOVER=0 keeps this offline — discovery is covered below.
        with _env(YT_AUDIO_MIRRORS=None, YT_AUDIO_DISCOVER="0"):
            hosts = ytaudio.mirrors()
        self.assertTrue(hosts)
        self.assertTrue(all(k in ("piped", "invidious") for k, _ in hosts))
        self.assertTrue(all(u.startswith("https://") and not u.endswith("/") for _, u in hosts))

    def test_env_override_replaces_the_list_and_keeps_order(self):
        with _env(YT_AUDIO_MIRRORS="piped:https://a.test,invidious:https://b.test/"):
            self.assertEqual(ytaudio.mirrors(),
                             [("piped", "https://a.test"), ("invidious", "https://b.test")])

    def test_kind_is_inferred_when_the_prefix_is_omitted(self):
        with _env(YT_AUDIO_MIRRORS="https://pipedapi.c.test,https://inv.d.test"):
            self.assertEqual(ytaudio.mirrors(),
                             [("piped", "https://pipedapi.c.test"),
                              ("invidious", "https://inv.d.test")])

    def test_junk_entries_are_dropped(self):
        with _env(YT_AUDIO_MIRRORS="not-a-url,,piped:https://ok.test"):
            self.assertEqual(ytaudio.mirrors(), [("piped", "https://ok.test")])

    def test_pinned_list_skips_discovery_entirely(self):
        def boom():
            raise AssertionError("discovery must not run when YT_AUDIO_MIRRORS is set")
        with _env(YT_AUDIO_MIRRORS="piped:https://a.test"), \
                _patched(ytaudio, "discover_mirrors", boom):
            self.assertEqual(ytaudio.mirrors(), [("piped", "https://a.test")])


PIPED_DIR_PAYLOAD = [
    {"name": "one", "api_url": "https://pipedapi.one.test/"},
    {"name": "two", "api_url": "https://pipedapi.two.test"},
    {"name": "broken"},                                   # no api_url -> dropped
]

INVIDIOUS_DIR_PAYLOAD = [
    ["a.test", {"uri": "https://a.test", "type": "https", "api": True}],
    ["b.test", {"uri": "http://b.test", "type": "http", "api": True}],   # not https
    ["c.test", {"uri": "https://c.test", "type": "https", "api": False}],  # no API
    "malformed-row",
]


class TestDiscovery(unittest.TestCase):
    def test_directories_are_parsed_into_hosts(self):
        fake = _FakeRequests({
            "piped-instances": _Resp(payload=PIPED_DIR_PAYLOAD),
            "api.invidious.io": _Resp(payload=INVIDIOUS_DIR_PAYLOAD),
        })
        with _patched(ytaudio, "requests", fake):
            found = ytaudio.discover_mirrors()
        self.assertEqual(found, [
            ("piped", "https://pipedapi.one.test"),
            ("piped", "https://pipedapi.two.test"),
            ("invidious", "https://a.test"),      # only the https + api:true row
        ])

    def test_a_dead_directory_is_not_fatal(self):
        fake = _FakeRequests({
            "piped-instances": RuntimeError("dns failure"),
            "raw.githubusercontent.com": RuntimeError("dns failure"),
            "api.invidious.io": _Resp(payload=INVIDIOUS_DIR_PAYLOAD),
        })
        with _patched(ytaudio, "requests", fake):
            self.assertEqual(ytaudio.discover_mirrors(), [("invidious", "https://a.test")])

    def test_both_directories_dead_returns_empty(self):
        fake = _FakeRequests({"": RuntimeError("offline")})
        with _patched(ytaudio, "requests", fake):
            self.assertEqual(ytaudio.discover_mirrors(), [])

    def test_discovered_hosts_extend_the_seeds_without_duplicates(self):
        seeded = ytaudio.DEFAULT_MIRRORS[0].split(":", 1)[1]
        with _env(YT_AUDIO_MIRRORS=None, YT_AUDIO_DISCOVER=None, YT_AUDIO_MAX_HOSTS="20"), \
                _patched(ytaudio, "discover_mirrors",
                         lambda: [("piped", seeded), ("piped", "https://new.test")]):
            hosts = ytaudio.mirrors()
        self.assertEqual(len([u for _, u in hosts if u == seeded]), 1)   # not duplicated
        self.assertIn(("piped", "https://new.test"), hosts)

    def test_host_count_is_capped(self):
        with _env(YT_AUDIO_MIRRORS=None, YT_AUDIO_DISCOVER=None, YT_AUDIO_MAX_HOSTS="3"), \
                _patched(ytaudio, "discover_mirrors",
                         lambda: [("piped", f"https://h{i}.test") for i in range(50)]):
            self.assertEqual(len(ytaudio.mirrors()), 3)


class TestStreamSelection(unittest.TestCase):
    def test_piped_picks_the_lowest_bitrate_and_uses_the_url_as_is(self):
        fake = _FakeRequests({"/streams/": _Resp(payload=PIPED_PAYLOAD)})
        with _patched(ytaudio, "requests", fake):
            url, ext = ytaudio._piped_stream("https://a.test", "vid123")
        self.assertEqual(url, "https://pipedproxy.test/lo")   # 48k beats 160k
        self.assertEqual(ext, "webm")

    def test_invidious_builds_a_local_proxy_url(self):
        fake = _FakeRequests({"/api/v1/videos/": _Resp(payload=INVIDIOUS_PAYLOAD)})
        with _patched(ytaudio, "requests", fake):
            url, ext = ytaudio._invidious_stream("https://b.test", "vid123")
        # local=true is load-bearing: a bare googlevideo URL is bound to the
        # INSTANCE's IP and would 403 for us exactly like a direct download.
        self.assertEqual(url, "https://b.test/latest_version?id=vid123&itag=139&local=true")
        self.assertEqual(ext, "m4a")

    def test_no_audio_stream_returns_none(self):
        fake = _FakeRequests({"/streams/": _Resp(payload={"audioStreams": []})})
        with _patched(ytaudio, "requests", fake):
            self.assertIsNone(ytaudio._piped_stream("https://a.test", "v"))


class TestDownloadGuards(unittest.TestCase):
    def test_html_error_page_is_refused(self):
        fake = _FakeRequests({"media": _Resp(chunks=[b"<html>nope</html>"],
                                             headers={"Content-Type": "text/html"})})
        with _patched(ytaudio, "requests", fake), tempfile.TemporaryDirectory() as td:
            self.assertIsNone(ytaudio._stream_to_file("https://x.test/media", td, "webm"))

    def test_tiny_file_is_discarded(self):
        fake = _FakeRequests({"media": _Resp(chunks=[b"x" * 100])})
        with _patched(ytaudio, "requests", fake), tempfile.TemporaryDirectory() as td:
            self.assertIsNone(ytaudio._stream_to_file("https://x.test/media", td, "webm"))

    def test_size_cap_aborts_a_runaway_stream(self):
        fake = _FakeRequests({"media": _Resp(chunks=[b"a" * 200_000] * 20)})
        with _env(YT_AUDIO_MAX_MB="1"), _patched(ytaudio, "requests", fake), \
                tempfile.TemporaryDirectory() as td:
            self.assertIsNone(ytaudio._stream_to_file("https://x.test/media", td, "webm"))

    def test_normal_audio_is_written(self):
        fake = _FakeRequests({"media": _Resp(chunks=[b"a" * 40_000, b"b" * 40_000])})
        with _patched(ytaudio, "requests", fake), tempfile.TemporaryDirectory() as td:
            out = ytaudio._stream_to_file("https://x.test/media", td, "webm")
            self.assertIsNotNone(out)
            self.assertEqual(out.stat().st_size, 80_000)


class TestMirrorFallthrough(unittest.TestCase):
    def test_a_dead_host_is_skipped_for_a_live_one(self):
        fake = _FakeRequests({
            "dead.test": RuntimeError("connection refused"),
            "live.test/streams/": _Resp(payload=PIPED_PAYLOAD),
            "pipedproxy.test/lo": _Resp(chunks=[b"a" * 50_000]),
        })
        with _env(YT_AUDIO_MIRRORS="piped:https://dead.test,piped:https://live.test"), \
                _patched(ytaudio, "requests", fake), tempfile.TemporaryDirectory() as td:
            got = ytaudio.download_via_mirror("vid123", td)
        self.assertIsNotNone(got)
        self.assertEqual(ytaudio.last_status, "ok")

    def test_all_dead_returns_none_without_raising(self):
        fake = _FakeRequests({"": RuntimeError("down")})
        with _env(YT_AUDIO_MIRRORS="piped:https://a.test,invidious:https://b.test"), \
                _patched(ytaudio, "requests", fake), tempfile.TemporaryDirectory() as td:
            self.assertIsNone(ytaudio.download_via_mirror("vid123", td))
        self.assertEqual(ytaudio.last_status, "all_failed")

    def test_empty_mirror_list_is_not_an_error(self):
        with _env(YT_AUDIO_MIRRORS=" "), tempfile.TemporaryDirectory() as td:
            # a whitespace-only override falls back to the built-in list, so the
            # only way to have zero hosts is junk-only input
            with _env(YT_AUDIO_MIRRORS="garbage"):
                self.assertIsNone(ytaudio.download_via_mirror("vid123", td))
        self.assertEqual(ytaudio.last_status, "no_mirrors")


class TestNetworkOpts(unittest.TestCase):
    def test_unset_means_no_overrides(self):
        with _env(YTDLP_PROXY=None, YTDLP_COOKIES_FILE=None):
            self.assertEqual(ydl_network_opts(), {})

    def test_proxy_is_passed_through(self):
        with _env(YTDLP_PROXY="socks5://127.0.0.1:9050", YTDLP_COOKIES_FILE=None):
            self.assertEqual(ydl_network_opts(), {"proxy": "socks5://127.0.0.1:9050"})

    def test_missing_cookie_file_is_ignored_not_fatal(self):
        with _env(YTDLP_PROXY=None, YTDLP_COOKIES_FILE="/no/such/cookies.txt"):
            self.assertEqual(ydl_network_opts(), {})

    def test_existing_cookie_file_is_used(self):
        with tempfile.TemporaryDirectory() as td:
            jar = Path(td) / "cookies.txt"
            jar.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
            with _env(YTDLP_PROXY=None, YTDLP_COOKIES_FILE=str(jar)):
                self.assertEqual(ydl_network_opts(), {"cookiefile": str(jar)})


class TestAsrRungOrder(unittest.TestCase):
    def test_mirror_rung_runs_only_after_ytdlp_fails(self):
        from pipeline import asr
        seen = []

        def fake_ytdlp(url, td, *, extra=None):
            seen.append(("ytdlp", extra))
            return None

        def fake_mirror(vid, td):
            seen.append(("mirror", vid))
            return Path(td) / "mirror_audio.webm"

        with _env(YTDLP_PROXY=None, YTDLP_COOKIES_FILE=None), \
                _patched(asr, "_ytdlp_audio", fake_ytdlp), \
                _patched(asr.ytaudio, "download_via_mirror", fake_mirror), \
                tempfile.TemporaryDirectory() as td:
            out = asr._download_audio("https://www.youtube.com/watch?v=abc123xyz", td)
        self.assertIsNotNone(out)
        # rung 2 skipped (nothing configured), so exactly one yt-dlp sweep then the mirror
        self.assertEqual(seen, [("ytdlp", None), ("mirror", "abc123xyz")])

    def test_ytdlp_success_short_circuits_the_chain(self):
        from pipeline import asr

        def fake_ytdlp(url, td, *, extra=None):
            return Path(td) / "audio.webm"

        def boom(vid, td):
            raise AssertionError("mirrors must not run when yt-dlp succeeded")

        with _patched(asr, "_ytdlp_audio", fake_ytdlp), \
                _patched(asr.ytaudio, "download_via_mirror", boom), \
                tempfile.TemporaryDirectory() as td:
            self.assertIsNotNone(asr._download_audio("https://youtu.be/abc123xyz", td))

    def test_proxy_rung_is_attempted_when_configured(self):
        from pipeline import asr
        seen = []

        def fake_ytdlp(url, td, *, extra=None):
            seen.append(extra)
            return None

        with _env(YTDLP_PROXY="http://p.test:8080", YTDLP_COOKIES_FILE=None), \
                _patched(asr, "_ytdlp_audio", fake_ytdlp), \
                _patched(asr.ytaudio, "download_via_mirror", lambda vid, td: None), \
                tempfile.TemporaryDirectory() as td:
            self.assertIsNone(asr._download_audio("https://youtu.be/abc123xyz", td))
        self.assertEqual(seen, [None, {"proxy": "http://p.test:8080"}])


class TestVideoId(unittest.TestCase):
    def test_common_url_shapes(self):
        for url, want in [
            ("https://www.youtube.com/watch?v=qfkTudStmZs", "qfkTudStmZs"),
            ("https://youtu.be/qfkTudStmZs", "qfkTudStmZs"),
            ("https://www.youtube.com/shorts/qfkTudStmZs", "qfkTudStmZs"),
            ("https://example.com/nope", ""),
        ]:
            self.assertEqual(video_id_from_url(url), want)


if __name__ == "__main__":
    unittest.main()

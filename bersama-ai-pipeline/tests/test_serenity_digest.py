from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import serenity_digest as sd  # noqa: E402


class TestFxTwitterTimelineParser(unittest.TestCase):
    def test_normalizes_full_text_tickers_media_and_skips_reposts(self):
        payload = {
            "results": [
                {
                    "type": "status",
                    "id": "2098697148103631117",
                    "url": "https://x.com/aleabitoreddit/status/2098697148103631117",
                    "text": "Full $SIVE thesis\nwith a second line.",
                    "created_at": "Sat Sep 12 08:56:06 +0000 2026",
                    "reposted_by": None,
                    "raw_text": {"facets": [
                        {"type": "symbol", "original": "SIVE"},
                    ]},
                    "media": {"photos": [
                        {"url": "https://pbs.twimg.com/media/example.jpg?name=orig"},
                    ]},
                },
                {
                    "type": "status",
                    "id": "2098000000000000000",
                    "url": "https://x.com/someone/status/2098000000000000000",
                    "text": "Someone else's repost",
                    "created_at": "Fri Sep 11 08:56:06 +0000 2026",
                    "reposted_by": {"screen_name": "aleabitoreddit"},
                },
            ]
        }

        posts = sd._parse_serenity_posts(payload)

        self.assertEqual([p["id"] for p in posts], ["2098697148103631117"])
        self.assertEqual(posts[0]["body"], "Full $SIVE thesis\nwith a second line.")
        self.assertEqual(posts[0]["cashtags"], ["SIVE"])
        self.assertEqual(posts[0]["image"],
                         "https://pbs.twimg.com/media/example.jpg?name=orig")
        self.assertFalse(posts[0]["source_cut"])
        self.assertEqual(posts[0]["created_at"].isoformat(), "2026-09-12T08:56:06+00:00")


if __name__ == "__main__":
    unittest.main()

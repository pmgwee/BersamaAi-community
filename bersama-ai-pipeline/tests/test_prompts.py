import unittest

from pipeline.prompts import build_user_message


class TestVideoSourceUrl(unittest.TestCase):
    def test_youtube_page_wins_over_temporary_media_url(self):
        message = build_user_message(
            {
                "title": "Example",
                "webpage_url": "https://www.youtube.com/watch?v=abc123",
                "url": "https://rr.example.googlevideo.com/videoplayback?expire=1",
            },
            "transcript",
        )

        self.assertIn(
            "Source URL: https://www.youtube.com/watch?v=abc123",
            message,
        )
        self.assertNotIn("googlevideo.com", message)


if __name__ == "__main__":
    unittest.main()

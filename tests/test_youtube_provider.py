from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from media_downloader.providers import YTDLP_VIDEO_FORMAT, YtDlpStrategy, yt_dlp_options


class YouTubeProviderTests(unittest.TestCase):
    def test_options_support_split_and_combined_streams(self) -> None:
        options = yt_dlp_options()
        self.assertEqual(options["format"], YTDLP_VIDEO_FORMAT)
        self.assertIn("+ba", str(options["format"]))
        self.assertIn("/b[ext=mp4]", str(options["format"]))
        self.assertEqual(options["js_runtimes"], {"deno": {}, "node": {}})

    def test_split_stream_preview_uses_thumbnail_without_fake_media_url(self) -> None:
        captured: dict[str, object] = {}

        class FakeYoutubeDL:
            def __init__(self, options: dict[str, object]) -> None:
                captured.update(options)

            def __enter__(self) -> "FakeYoutubeDL":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def extract_info(self, url: str, download: bool) -> dict[str, object]:
                captured["url"] = url
                captured["download"] = download
                return {
                    "title": "Public video",
                    "thumbnail": "https://example.test/thumb.jpg",
                    "duration": 19,
                    "duration_string": "0:19",
                    "width": 320,
                    "height": 240,
                    "requested_formats": [
                        {"url": "https://example.test/video.mp4", "vcodec": "av01", "acodec": "none"},
                        {"url": "https://example.test/audio.m4a", "vcodec": "none", "acodec": "mp4a"},
                    ],
                }

        fake_module = ModuleType("yt_dlp")
        fake_module.YoutubeDL = FakeYoutubeDL  # type: ignore[attr-defined]
        with patch.dict(sys.modules, {"yt_dlp": fake_module}):
            candidate = YtDlpStrategy(Path(".")).resolve("https://youtu.be/example")

        self.assertEqual(captured["format"], YTDLP_VIDEO_FORMAT)
        self.assertEqual(captured["url"], "https://youtu.be/example")
        self.assertFalse(captured["download"])
        self.assertEqual(candidate.source_url, "")
        self.assertEqual(candidate.thumbnail_url, "https://example.test/thumb.jpg")
        self.assertEqual(candidate.resolution, "320×240")


if __name__ == "__main__":
    unittest.main()

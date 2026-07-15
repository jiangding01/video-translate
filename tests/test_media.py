import json
import tempfile
import unittest
from pathlib import Path

from xvt.download import inspect_remote, validate_remote_url
from xvt.media import probe_media
from xvt.process import CommandResult


class MediaTest(unittest.TestCase):
    def test_probe_media_parses_video_and_audio_streams(self) -> None:
        payload = {
            "format": {"duration": "12.345", "size": "4096", "format_name": "mov,mp4"},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                    "avg_frame_rate": "30000/1001",
                },
                {"codec_type": "audio", "codec_name": "aac"},
            ],
        }

        with tempfile.TemporaryDirectory() as directory:
            media_path = Path(directory) / "video.mp4"
            media_path.write_bytes(b"media")

            def runner(args):
                return CommandResult(tuple(args), 0, json.dumps(payload), "")

            info = probe_media(media_path, runner)

        self.assertEqual(info.duration_ms, 12345)
        self.assertEqual((info.width, info.height), (1920, 1080))
        self.assertAlmostEqual(info.frame_rate or 0, 29.970, places=3)
        self.assertEqual((info.video_codec, info.audio_codec), ("h264", "aac"))

    def test_inspect_remote_parses_subtitle_languages(self) -> None:
        payload = {
            "id": "abc",
            "title": "Example",
            "duration": 5.5,
            "webpage_url": "https://www.youtube.com/watch?v=abc",
            "subtitles": {"en": [{}]},
            "automatic_captions": {"zh-Hans": [{}], "ja": [{}]},
        }

        def runner(args):
            return CommandResult(tuple(args), 0, json.dumps(payload), "")

        info = inspect_remote(payload["webpage_url"], runner)

        self.assertEqual(info.duration_ms, 5500)
        self.assertEqual(info.subtitle_languages, ("en",))
        self.assertEqual(info.automatic_subtitle_languages, ("ja", "zh-Hans"))

    def test_remote_url_rejects_non_http_schemes(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTP"):
            validate_remote_url("file:///tmp/video.mp4")


if __name__ == "__main__":
    unittest.main()

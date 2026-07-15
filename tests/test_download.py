import tempfile
import unittest
from pathlib import Path

from xvt.download import (
    download_remote_media,
    download_remote_subtitles,
    select_subtitle_language,
)
from xvt.process import CommandResult


class DownloadTest(unittest.TestCase):
    def test_selects_exact_or_regional_subtitle_language(self) -> None:
        self.assertEqual(select_subtitle_language("en", ("fr", "en-US")), "en-US")
        self.assertEqual(select_subtitle_language("zh-CN", ("zh-CN", "zh-TW")), "zh-CN")
        self.assertIsNone(select_subtitle_language("ja", ("en", "zh-CN")))

    def test_downloads_remote_subtitles_without_media(self) -> None:
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def runner(args):
                calls.append(args)
                (root / "source.en.srt").write_text("subtitle", encoding="utf-8")
                return CommandResult(tuple(args), 0, "", "")

            result = download_remote_subtitles(
                "https://youtu.be/abc",
                root,
                language="en",
                runner=runner,
            )

        self.assertEqual(result.name, "source.en.srt")
        self.assertIn("--skip-download", calls[0])
        self.assertIn("--write-subs", calls[0])
        self.assertNotIn("--write-auto-subs", calls[0])

    def test_downloads_audio_without_using_browser_cookies_by_default(self) -> None:
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloaded = root / "source.m4a"

            def runner(args):
                calls.append(args)
                downloaded.write_bytes(b"audio")
                return CommandResult(tuple(args), 0, f"{downloaded}\n", "")

            result = download_remote_media(
                "https://www.youtube.com/watch?v=abc",
                root,
                audio_only=True,
                runner=runner,
            )

        self.assertEqual(result, downloaded.resolve())
        self.assertIn("bestaudio/best", calls[0])
        self.assertNotIn("--cookies-from-browser", calls[0])

    def test_cookies_and_proxy_require_explicit_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloaded = root / "source.webm"

            def runner(args):
                downloaded.write_bytes(b"video")
                self.assertIn("--cookies-from-browser", args)
                self.assertIn("chrome", args)
                self.assertIn("--proxy", args)
                return CommandResult(tuple(args), 0, f"{downloaded}\n", "")

            download_remote_media(
                "https://youtu.be/abc",
                root,
                cookies_from_browser="chrome",
                proxy="http://127.0.0.1:7890",
                runner=runner,
            )

    def test_download_emits_structured_progress(self) -> None:
        events = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloaded = root / "source.mp4"

            def runner(args, **kwargs):
                downloaded.write_bytes(b"video")
                kwargs["line_callback"](
                    "stderr",
                    'XVT_PROGRESS:{"downloaded_bytes":50,"total_bytes":100,"eta":2}',
                )
                return CommandResult(tuple(args), 0, f"{downloaded}\n", "")

            download_remote_media(
                "https://youtu.be/abc",
                root,
                event_sink=events.append,
                runner=runner,
            )

        self.assertEqual(events[0].type, "download_progress")
        self.assertEqual(events[0].data["percent"], 50.0)

    def test_download_ignores_unknown_progress_totals(self) -> None:
        events = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloaded = root / "source.mp4"

            def runner(args, **kwargs):
                downloaded.write_bytes(b"video")
                kwargs["line_callback"](
                    "stderr",
                    'XVT_PROGRESS:{"downloaded_bytes":"NA","total_bytes":"NA"}',
                )
                return CommandResult(tuple(args), 0, f"{downloaded}\n", "")

            download_remote_media(
                "https://youtu.be/abc",
                root,
                event_sink=events.append,
                runner=runner,
            )

        self.assertEqual(events[0].data["downloaded_bytes"], 0)
        self.assertIsNone(events[0].data["percent"])

    def test_rejects_reported_files_outside_job_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root.parent / "outside.mp4"
            outside.write_bytes(b"video")

            def runner(args):
                return CommandResult(tuple(args), 0, f"{outside}\n", "")

            with self.assertRaisesRegex(RuntimeError, "outside the job directory"):
                download_remote_media("https://youtu.be/abc", root, runner=runner)
            outside.unlink()


if __name__ == "__main__":
    unittest.main()

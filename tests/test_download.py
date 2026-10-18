import tempfile
import unittest
from pathlib import Path

from xvt.download import (
    VIDEO_FORMAT_SELECTOR,
    download_remote_media,
    download_remote_subtitles,
    inspect_remote,
    select_subtitle_language,
)
from xvt.errors import DependencyError
from xvt.process import CommandError, CommandResult


class DownloadTest(unittest.TestCase):
    def test_anonymous_inspection_ignores_config_and_cookie_but_keeps_proxy(self) -> None:
        def runner(args):
            self.assertIn("--ignore-config", args)
            self.assertNotIn("--cookies-from-browser", args)
            self.assertIn("--proxy", args)
            return CommandResult(tuple(args), 0, '{"id":"abc"}', "")

        inspect_remote(
            "https://youtu.be/abc",
            cookies_from_browser="chrome",
            proxy="http://127.0.0.1:7890",
            anonymous=True,
            runner=runner,
        )

    def test_anonymous_subtitle_download_ignores_config_and_cookie_but_keeps_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def runner(args):
                self.assertIn("--ignore-config", args)
                self.assertNotIn("--cookies-from-browser", args)
                self.assertIn("--proxy", args)
                self.assertIn("--write-subs", args)
                (root / "source.en.srt").write_text("subtitle", encoding="utf-8")
                return CommandResult(tuple(args), 0, "", "")

            download_remote_subtitles(
                "https://youtu.be/abc",
                root,
                language="en",
                cookies_from_browser="chrome",
                proxy="http://127.0.0.1:7890",
                anonymous=True,
                runner=runner,
            )

    def test_remote_inspection_forwards_explicit_access_options(self) -> None:
        def runner(args):
            self.assertIn("--cookies-from-browser", args)
            self.assertIn("chrome", args)
            self.assertIn("--proxy", args)
            return CommandResult(
                tuple(args),
                0,
                '{"id":"abc","title":"Example","duration":5}',
                "",
            )

        info = inspect_remote(
            "https://youtu.be/abc",
            cookies_from_browser="chrome",
            proxy="http://127.0.0.1:7890",
            runner=runner,
        )

        self.assertEqual(info.id, "abc")

    def test_selects_exact_or_regional_subtitle_language(self) -> None:
        self.assertEqual(select_subtitle_language("en", ("fr", "en-US")), "en-US")
        self.assertEqual(select_subtitle_language("zh-CN", ("zh-CN", "zh-TW")), "zh-CN")
        self.assertIsNone(select_subtitle_language("ja", ("en", "zh-CN")))

    def test_output_template_escapes_literal_percent_in_the_job_directory(self) -> None:
        """yt-dlp 会展开 `-o` 值里**目录部分**的模板字段，字面量 `%` 必须写成 `%%`。

        实测（yt-dlp 2026.08.19，离线 `--load-info-json ... --print filename`）：
        `-o '/Users/x/My %(title)s videos/source.%(ext)s'` 打印
        `/Users/x/My PWNED TITLE videos/source.mp4`——文件落到 Job 目录之外；
        `'/Users/x/100%% done/source.%(ext)s'` 打印 `/Users/x/100% done/source.mp4`，
        转义后回到用户真正的目录名。
        """

        for name, download in (
            (
                "download_remote_subtitles",
                lambda root, runner: download_remote_subtitles(
                    "https://youtu.be/abc", root, language="en", runner=runner
                ),
            ),
            (
                "download_remote_media",
                lambda root, runner: download_remote_media(
                    "https://youtu.be/abc", root, audio_only=True, runner=runner
                ),
            ),
        ):
            with self.subTest(download=name):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory) / "My %(title)s videos"
                    root.mkdir()
                    captured: list[tuple[str, ...]] = []

                    def runner(args, root=root, captured=captured):
                        captured.append(tuple(args))
                        (root / "source.en.srt").write_text("subtitle", encoding="utf-8")
                        (root / "source.m4a").write_bytes(b"audio")
                        return CommandResult(tuple(args), 0, str(root / "source.m4a"), "")

                    download(root, runner)

                template = captured[0][captured[0].index("-o") + 1]
                self.assertEqual(template, f"{root}".replace("%", "%%") + "/source.%(ext)s")
                self.assertIn("%%(title)s", template)
                self.assertTrue(template.endswith("/source.%(ext)s"))

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

    def test_picks_requested_language_when_several_subtitle_files_exist(self) -> None:
        cases = (
            # (写出的文件, 请求语言, 期望文件)
            (("source.de.srt", "source.en.srt", "source.zh-Hans.srt"), "en", "source.en.srt"),
            (
                ("source.de.srt", "source.en.srt", "source.zh-Hans.srt"),
                "zh-Hans",
                "source.zh-Hans.srt",
            ),
            (("source.de.srt", "source.zh-Hans.srt"), "zh", "source.zh-Hans.srt"),
            # yt-dlp 的 -orig 变体没有精确命中，仍应按语言前缀选中。
            (("source.de.srt", "source.en-orig.srt"), "en", "source.en-orig.srt"),
            # 大小写不敏感。
            (("source.de.srt", "source.EN.srt"), "en", "source.EN.srt"),
            # 无命中时退回既有 sorted glob 行为。
            (("source.de.srt", "source.fr.srt"), "en", "source.de.srt"),
        )
        for written, language, expected in cases:
            with self.subTest(language=language, written=written):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)

                    def runner(args, files=written):
                        for name in files:
                            (root / name).write_text("subtitle", encoding="utf-8")
                        return CommandResult(tuple(args), 0, "", "")

                    result = download_remote_subtitles(
                        "https://youtu.be/abc",
                        root,
                        language=language,
                        runner=runner,
                    )

                self.assertEqual(result.name, expected)

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

    def test_video_download_uses_format_fallback_chain(self) -> None:
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloaded = root / "source.mp4"

            def runner(args):
                calls.append(args)
                downloaded.write_bytes(b"video")
                return CommandResult(tuple(args), 0, f"{downloaded}\n", "")

            download_remote_media("https://youtu.be/abc", root, runner=runner)

        self.assertEqual(VIDEO_FORMAT_SELECTOR, "bv*+ba/b")
        format_index = calls[0].index("-f")
        self.assertEqual(calls[0][format_index + 1], VIDEO_FORMAT_SELECTOR)
        self.assertIn("--merge-output-format", calls[0])
        self.assertIn("mp4", calls[0])

    def test_http_403_guides_continue_and_cookies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def runner(args):
                raise CommandError(
                    CommandResult(
                        tuple(args),
                        1,
                        "",
                        "ERROR: unable to download video data: HTTP Error 403: Forbidden",
                    )
                )

            with self.assertRaisesRegex(DependencyError, "HTTP 403") as raised:
                download_remote_media("https://youtu.be/abc", root, runner=runner)

        message = str(raised.exception)
        self.assertIn("continue", message)
        self.assertIn("--cookies-from-browser", message)
        self.assertIn("yt-dlp", message)

    def test_non_403_command_errors_are_not_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def runner(args):
                raise CommandError(
                    CommandResult(
                        tuple(args),
                        1,
                        "",
                        "ERROR: Unsupported URL: https://example.invalid",
                    )
                )

            with self.assertRaises(CommandError) as raised:
                download_remote_media("https://youtu.be/abc", root, runner=runner)

        self.assertIn("Unsupported URL", str(raised.exception))
        self.assertNotIsInstance(raised.exception, DependencyError)

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
                audio_only=True,
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

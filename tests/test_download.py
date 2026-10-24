import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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

            def runner(args, **kwargs):
                calls.append(args)
                downloaded.write_bytes(b"video")
                return CommandResult(tuple(args), 0, f"{downloaded}\n", "")

            download_remote_media("https://youtu.be/abc", root, runner=runner)

        self.assertEqual(VIDEO_FORMAT_SELECTOR, "bv*+ba/b")
        # calls[0] 是容量预检的探测（也带 `-f`，用同一个 selector），下载是 calls[1]。
        probe, download = calls
        self.assertIn("--dump-single-json", probe)
        self.assertEqual(probe[probe.index("-f") + 1], VIDEO_FORMAT_SELECTOR)
        format_index = download.index("-f")
        self.assertEqual(download[format_index + 1], VIDEO_FORMAT_SELECTOR)
        self.assertIn("--merge-output-format", download)
        self.assertIn("mp4", download)

    def test_http_403_guides_continue_and_cookies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def runner(args, **kwargs):
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

            def runner(args, **kwargs):
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
                if "--dump-single-json" in args:
                    # 容量预检的探测：本用例只验进度事件，返回空元数据让预检跳过。
                    return CommandResult(tuple(args), 0, "{}", "")
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
                if "--dump-single-json" in args:
                    # 容量预检的探测：本用例只验进度事件，返回空元数据让预检跳过。
                    return CommandResult(tuple(args), 0, "{}", "")
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

            def runner(args, **kwargs):
                return CommandResult(tuple(args), 0, f"{outside}\n", "")

            with self.assertRaisesRegex(RuntimeError, "outside the job directory"):
                download_remote_media("https://youtu.be/abc", root, runner=runner)
            outside.unlink()


class DirectDownloadSpacePrecheckTest(unittest.TestCase):
    """无 Cookie / 非 YouTube 完整视频下载的容量预检。

    这条路径没有 ``VideoSelection``（那是 YouTube 画质比较的产物），所以自己探一次。
    探测与下载必须看到同一组格式，否则量的不是要下的；探测失败一律跳过预检，不让某个
    站点的元数据形状把整条下载弄挂。
    """

    GIB = 2**30

    def _run(self, *, free_bytes, probe_stdout=None, probe_error=None, audio_only=False):
        """返回 (命令列表, 事件列表, 抛出的异常或 None)。"""

        calls, events = [], []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloaded = root / "source.mp4"

            def runner(args, **kwargs):
                calls.append(args)
                if "--dump-single-json" in args:
                    if probe_error is not None:
                        raise probe_error
                    return CommandResult(tuple(args), 0, probe_stdout or "{}", "")
                downloaded.write_bytes(b"video")
                return CommandResult(tuple(args), 0, f"{downloaded}\n", "")

            raised = None
            with patch(
                "xvt.download.shutil.disk_usage",
                return_value=SimpleNamespace(free=free_bytes),
            ):
                try:
                    download_remote_media(
                        "https://example.com/video",
                        root,
                        audio_only=audio_only,
                        runner=runner,
                        event_sink=events.append,
                    )
                except Exception as error:  # noqa: BLE001 - 用例自己断言类型
                    raised = error
        return calls, events, raised

    @staticmethod
    def _split_probe(video_size, audio_size):
        return json.dumps(
            {
                "format_id": "137+140",
                "requested_formats": [
                    {"vcodec": "h264", "acodec": "none", "filesize": video_size},
                    {"vcodec": "none", "acodec": "aac", "filesize": audio_size},
                ],
            }
        )

    def test_insufficient_space_fails_before_downloading(self) -> None:
        calls, events, raised = self._run(
            free_bytes=self.GIB // 2,
            probe_stdout=self._split_probe(2 * self.GIB, self.GIB),
        )

        self.assertIsInstance(raised, DependencyError)
        self.assertIn("磁盘空间不足", str(raised))
        # 只起了探测，没起下载——一个字节都没下。
        self.assertEqual(len(calls), 1)
        self.assertIn("--dump-single-json", calls[0])
        (event,) = events
        self.assertEqual(event.type, "download_space_insufficient")
        self.assertEqual(event.data["required_bytes"], 6 * self.GIB)
        self.assertEqual(event.data["format_id"], "137+140")
        self.assertEqual(event.data["part_count"], 2)

    def test_sufficient_space_downloads(self) -> None:
        calls, events, raised = self._run(
            free_bytes=100 * self.GIB,
            probe_stdout=self._split_probe(2 * self.GIB, self.GIB),
        )

        self.assertIsNone(raised)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("--dump-single-json", calls[1])
        self.assertEqual(events, [])

    def test_probe_failure_skips_the_precheck(self) -> None:
        # 站点元数据拿不到不该让下载失活：探测失败就跳过预检，照常下载。
        calls, events, raised = self._run(
            free_bytes=1,
            probe_error=CommandError(CommandResult((), 1, "", "ERROR: Unsupported URL")),
        )

        self.assertIsNone(raised)
        self.assertEqual(len(calls), 2)
        self.assertEqual(events, [])

    def test_unparsable_probe_output_skips_the_precheck(self) -> None:
        calls, events, raised = self._run(free_bytes=1, probe_stdout="not json at all")

        self.assertIsNone(raised)
        self.assertEqual(len(calls), 2)
        self.assertEqual(events, [])

    def test_probe_without_sizes_skips_the_precheck(self) -> None:
        # 「不该红时不红」：元数据有效但没报大小，按未知处理而不是当成 0。
        calls, events, raised = self._run(
            free_bytes=1,
            probe_stdout=json.dumps({"format_id": "18", "vcodec": "h264", "acodec": "aac"}),
        )

        self.assertIsNone(raised)
        self.assertEqual(len(calls), 2)
        self.assertEqual(events, [])

    def test_audio_only_is_not_probed(self) -> None:
        # bestaudio/best 通常是几十 MB，不值一次网络往返。
        calls, events, raised = self._run(free_bytes=1, audio_only=True)

        self.assertIsNone(raised)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("--dump-single-json", calls[0])
        self.assertEqual(events, [])

    def test_probe_uses_the_same_access_flags_as_the_download(self) -> None:
        # 探测与下载必须看到同一组格式，否则量的不是要下的。
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloaded = root / "source.mp4"

            def runner(args, **kwargs):
                calls.append(args)
                if "--dump-single-json" in args:
                    return CommandResult(tuple(args), 0, "{}", "")
                downloaded.write_bytes(b"video")
                return CommandResult(tuple(args), 0, f"{downloaded}\n", "")

            download_remote_media(
                "https://example.com/video",
                root,
                cookies_from_browser="chrome",
                proxy="http://127.0.0.1:7890",
                runner=runner,
            )

        probe, download = calls
        for flag in ("--cookies-from-browser", "chrome", "--proxy", "http://127.0.0.1:7890"):
            self.assertIn(flag, probe, flag)
            self.assertIn(flag, download, flag)
        self.assertEqual(probe[probe.index("-f") + 1], download[download.index("-f") + 1])


if __name__ == "__main__":
    unittest.main()

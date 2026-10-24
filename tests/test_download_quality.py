import json
import tempfile
import unittest
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from xvt.artifacts import ArtifactKind, downstream_of
from xvt.checksums import file_checksum
from xvt.download import download_remote_media
from xvt.errors import DependencyError
from xvt.job_store import create_job
from xvt.pipeline.common import add_artifact, record_media_download_selection
from xvt.process import CommandError, CommandResult


@dataclass(frozen=True)
class _Usage:
    """``shutil.disk_usage`` 的替身：容量预检只读 ``free``。"""

    free: int


def selection(
    height, *, video_id="abc", language="en", audio=True, duration=10, size=None, approx=None
):
    """单文件格式的探测 JSON（没有 ``requested_formats``）。

    这一份同时带 ``vcodec`` 与 ``acodec``，所以 ``_probe_video`` 里的 ``video`` 与
    ``audio`` 指向**同一个** dict——容量预检必须按 ``parts`` 求和，按 video+audio
    相加会翻倍。``test_single_file_format_size_is_not_doubled`` 钉住这一点。
    """

    data = {
        "id": video_id,
        "format_id": "18" if height == 360 else "137+140",
        "width": height * 16 // 9,
        "height": height,
        "duration": duration,
        "vcodec": "h264",
        "acodec": "aac" if audio else "none",
        "language": language,
        "language_preference": 10,
    }
    if size is not None:
        data["filesize"] = size
    if approx is not None:
        data["filesize_approx"] = approx
    return data


def _size_keys(size):
    return {} if size is None else {"filesize": size}


def split_selection(height, *, video_size, audio_size, duration=10):
    """video / audio 分离的探测 JSON：下载后要 mux，容量门槛按两份算。"""

    return {
        **selection(height, duration=duration),
        "requested_formats": [
            # width / height 必须落在 video part 上：_probe_video 取的是
            # video["width"]，不是顶层 data 的。
            {
                "vcodec": "h264",
                "acodec": "none",
                "width": height * 16 // 9,
                "height": height,
                **_size_keys(video_size),
            },
            {
                "vcodec": "none",
                "acodec": "aac",
                "language": "en",
                "language_preference": 10,
                **_size_keys(audio_size),
            },
        ],
    }


class DownloadQualityTest(unittest.TestCase):
    def execute(
        self,
        configured=None,
        anonymous=None,
        *,
        warning="",
        actual_height=None,
        existing=False,
        fail_download=False,
        anonymous_error=False,
        anonymous_warning="",
        race=False,
        free_bytes=None,
        collect_events=None,
    ):
        configured = configured if configured is not None else selection(360)
        anonymous = anonymous if anonymous is not None else selection(1080)
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            if existing:
                (root / "source.mp4").write_bytes(b"old video")

            def runner(args, **kwargs):
                calls.append(args)
                if "--dump-single-json" in args:
                    self.assertEqual(kwargs["timeout"], 60)
                    cookie = "--cookies-from-browser" in args
                    if not cookie:
                        self.assertIn("--ignore-config", args)
                        if anonymous_error:
                            raise CommandError(CommandResult(tuple(args), 1, "", "secret"))
                    data = configured if cookie else anonymous
                    diagnostic = warning if cookie else anonymous_warning
                    return CommandResult(
                        tuple(args),
                        0,
                        json.dumps(data),
                        "WARNING: " + diagnostic if diagnostic else "",
                    )
                if args[0] == "ffprobe":
                    chosen = configured if "--cookies-from-browser" in calls[-2] else anonymous
                    height = actual_height or chosen["height"]
                    streams = [
                        {
                            "codec_type": "video",
                            "codec_name": "h264",
                            "width": height * 16 // 9,
                            "height": height,
                        }
                    ]
                    if chosen.get("acodec") != "none":
                        streams.append({"codec_type": "audio", "codec_name": "aac"})
                    return CommandResult(
                        tuple(args),
                        0,
                        json.dumps(
                            {
                                "streams": streams,
                                "format": {"duration": chosen["duration"], "size": 5},
                            }
                        ),
                        "",
                    )
                if fail_download:
                    raise CommandError(CommandResult(tuple(args), 1, "", "HTTP Error 403"))
                path = Path(args[args.index("-o") + 1].replace("%(ext)s", "mp4"))
                path.write_bytes(b"video")
                return CommandResult(tuple(args), 0, str(path) + "\n", "")

            try:

                def hash_media(path):
                    if race:
                        (root / "source.mp4").write_bytes(b"old video")
                    return file_checksum(path)

                space = (
                    patch("xvt.download.shutil.disk_usage", return_value=_Usage(free_bytes))
                    if free_bytes is not None
                    else nullcontext()
                )
                with patch("xvt.download.file_checksum", side_effect=hash_media), space:
                    result = download_remote_media(
                        "https://youtu.be/abc",
                        root,
                        cookies_from_browser="chrome",
                        proxy="http://127.0.0.1:7890",
                        runner=runner,
                        event_sink=(collect_events.append if collect_events is not None else None),
                    )
                self.assertEqual(sum("--dump-single-json" in args for args in calls), 2)
                report = json.loads(result.with_suffix(".download.json").read_text())
                return calls, report
            finally:
                self.assertFalse(list(root.glob(".xvt-download-*")))
                if existing or race:
                    self.assertEqual((root / "source.mp4").read_bytes(), b"old video")

    def test_sufficient_space_downloads_and_emits_nothing(self):
        """「不该红时不红」：空间充足时完整流程照旧，且不发预检事件。"""

        events = []
        calls, report = self.execute(
            configured=selection(1080, size=2**30),
            anonymous=selection(1080, size=2**30),
            free_bytes=100 * 2**30,
            collect_events=events,
        )

        self.assertEqual(report["access"], "configured")
        self.assertEqual(sum("--dump-single-json" in args for args in calls), 2)
        self.assertEqual(
            [event for event in events if event.type == "download_space_insufficient"], []
        )

    def test_cookie_sabr_falls_back_to_anonymous_hd(self):
        calls, report = self.execute(
            warning="Some web formats have been skipped as they are missing a URL"
        )
        self.assertEqual(report["access"], "anonymous")
        self.assertEqual(report["height"], 1080)
        self.assertNotIn("--cookies-from-browser", calls[2])
        self.assertEqual(calls[2][calls[2].index("-f") + 1], "137+140")
        self.assertTrue(all("--proxy" in args for args in calls[:3]))

    def test_cookie_sabr_tie_falls_back_to_the_clean_anonymous_probe(self):
        """两侧画质相同、只有 Cookie 侧提取不完整时，改走干净的匿名侧，而不是整单失败。

        真实踩坑（2026-10-23）：YouTube 对该账号开了 SABR 实验，Cookie 侧必然报
        ``web_embedded ... formats have been skipped``，而匿名侧零警告、选中的是同一个
        1080p 格式。此前「严格更高画质才换」让平局停在脏的 Cookie 侧，于是这台机器上
        任何 YouTube 视频都卡在「格式提取不完整」。换路仍受原有的音轨闸约束。
        """

        calls, report = self.execute(
            configured=selection(1080),
            anonymous=selection(1080),
            warning=(
                "Some web_embedded client https formats have been skipped as they are missing a URL"
            ),
        )
        self.assertEqual(report["access"], "anonymous")
        self.assertEqual(report["height"], 1080)
        self.assertEqual(report["reason"], "anonymous_clean_extraction")
        self.assertNotIn("--cookies-from-browser", calls[2])

    def test_cookie_sabr_tie_still_fails_when_the_anonymous_probe_is_also_dirty(self):
        """平局换路的前提是另一侧**干净**；两侧都脏时没有可信参照，照旧 fail closed。"""

        with self.assertRaises(DependencyError):
            self.execute(
                configured=selection(1080),
                anonymous=selection(1080),
                warning="formats have been skipped as they are missing a URL",
                anonymous_warning="formats have been skipped as they are missing a URL",
            )

    def test_cookie_sabr_tie_still_refuses_an_anonymous_video_without_audio(self):
        """平局换路不得绕过既有的音轨闸：匿名侧没有音轨就拒绝替换。"""

        with self.assertRaises(DependencyError):
            self.execute(
                configured=selection(1080),
                anonymous=selection(1080, audio=False),
                warning="formats have been skipped as they are missing a URL",
            )

    def test_4k_and_8k_have_no_resolution_cap(self):
        for height in [2160, 4320]:
            with self.subTest(height=height):
                _, report = self.execute(anonymous=selection(height))
                self.assertEqual(report["height"], height)

    def test_diagnostic_registration_and_corruption_do_not_invalidate_media(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("https://youtu.be/abc", root, {})
            media = root / "source.mp4"
            media.write_bytes(b"media")
            add_artifact(job, "source_video", media, None)
            report = media.with_suffix(".download.json")
            for content in ["{", "[]", '{"media_checksum":"wrong"}']:
                report.write_text(content)
                record_media_download_selection(job, media, None)
                self.assertEqual([a.kind for a in job.artifacts], ["source_video"])
            report.write_text(json.dumps({"version": 1, "media_checksum": file_checksum(media)}))
            events = []
            record_media_download_selection(job, media, events.append)
            self.assertIn("media_download_selection", [a.kind for a in job.artifacts])
            self.assertEqual(events[-1].type, "media_download_selection")
            self.assertIn(
                ArtifactKind.MEDIA_DOWNLOAD_SELECTION, downstream_of(ArtifactKind.SOURCE_VIDEO)
            )
            self.assertNotIn(
                ArtifactKind.SOURCE_VIDEO, downstream_of(ArtifactKind.MEDIA_DOWNLOAD_SELECTION)
            )

    def test_configured_higher_or_tied_keeps_cookie(self):
        for height in [1080, 2160]:
            _, report = self.execute(configured=selection(height))
            self.assertEqual(report["access"], "configured")

    def test_confirmed_available_low_resolution_is_allowed(self):
        _, report = self.execute(anonymous=selection(360))
        self.assertEqual(report["height"], 360)

    def test_anonymous_original_audio_replaces_cookie_dub(self):
        dubbed = selection(360, language="bn")
        dubbed["language_preference"] = -1
        _, report = self.execute(configured=dubbed, anonymous=selection(1080, language="en-US"))
        self.assertEqual(report["audio_language"], "en-US")
        self.assertTrue(report["original_audio"])

    def test_unused_silent_anonymous_video_keeps_configured_original(self):
        _, report = self.execute(configured=selection(1080), anonymous=selection(360, audio=False))
        self.assertEqual(report["access"], "configured")

    def test_fail_closed_boundaries(self):
        cases = [
            {"anonymous": selection(1080, video_id="other")},
            {"anonymous": selection(1080, duration=11)},
            {"anonymous": selection(1080, language="ja")},
            {"configured": selection(2160), "warning": "formats have been skipped"},
            {"configured": selection(2160), "anonymous_warning": "formats have been skipped"},
            {"anonymous_error": True},
            {"actual_height": 360},
            {"existing": True},
            {"race": True},
            {"fail_download": True},
            {"anonymous": {}},
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises((DependencyError, RuntimeError)):
                self.execute(**case)


class DownloadSpacePrecheckTest(unittest.TestCase):
    """下载前容量预检（B2 第一层）。

    只做证伪：拿不到所选格式的登记大小就跳过，不猜。``_probe_runner`` 对任何非探测
    命令直接断言失败，所以「预检生效时一个字节都不下」由桩本身钉住，不靠事后数命令
    条数；反过来，预检**该放行**时这条断言会以 AssertionError 冒出来，正是放行的证据。
    """

    GIB = 2**30

    def _probe_runner(self, calls, probe, stderr=""):
        def runner(args, **kwargs):
            calls.append(args)
            if "--dump-single-json" not in args:
                raise AssertionError(f"预检应早于下载失败，不该起这条命令：{args}")
            return CommandResult(tuple(args), 0, json.dumps(probe), stderr)

        return runner

    def _attempt(self, probe, free_bytes, *, stderr=""):
        """跑到预检为止；返回 (异常, 事件列表)。"""

        calls, events = [], []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("xvt.download.shutil.disk_usage", return_value=_Usage(free_bytes)),
                self.assertRaises(Exception) as caught,
            ):
                download_remote_media(
                    "https://youtu.be/abc",
                    root,
                    cookies_from_browser="chrome",
                    runner=self._probe_runner(calls, probe, stderr),
                    event_sink=events.append,
                )
            self.assertFalse(list(root.iterdir()), "预检失败不该留下任何产物")
        return caught.exception, events

    def _assert_passed_precheck(self, probe, free_bytes):
        error, events = self._attempt(probe, free_bytes)
        self.assertIsInstance(error, AssertionError)
        self.assertIn("不该起这条命令", str(error))
        self.assertEqual(events, [])

    def test_insufficient_space_fails_before_downloading(self):
        probe = split_selection(1080, video_size=2 * self.GIB, audio_size=self.GIB)
        error, events = self._attempt(probe, free_bytes=self.GIB // 2)

        self.assertIsInstance(error, DependencyError)
        self.assertIn("磁盘空间不足", str(error))
        (event,) = events
        self.assertEqual(event.type, "download_space_insufficient")
        # 分离格式按 mux 峰值算两份：(2 + 1) GiB × 2。
        self.assertEqual(event.data["required_bytes"], 6 * self.GIB)
        self.assertEqual(event.data["available_bytes"], self.GIB // 2)
        self.assertEqual(event.data["part_count"], 2)
        self.assertEqual(event.data["format_id"], "137+140")
        self.assertNotIn("path", event.data)

    def test_split_format_threshold_is_double(self):
        probe = split_selection(1080, video_size=self.GIB, audio_size=self.GIB)
        error, _ = self._attempt(probe, free_bytes=4 * self.GIB - 1)
        self.assertIsInstance(error, DependencyError)
        # 刚好两份就放行。
        self._assert_passed_precheck(probe, free_bytes=4 * self.GIB)

    def test_single_file_format_size_is_not_doubled(self):
        # 单文件格式下 video 与 audio 是同一个 dict：按 video+audio 相加会算成 4 GiB
        # 再乘 2，2.5 GiB 可用就被误拦。按 parts 求和只算 2 GiB、门槛一份。
        self._assert_passed_precheck(selection(360, size=2 * self.GIB), 5 * self.GIB // 2)

    def test_missing_size_skips_the_precheck(self):
        # 「不该红时不红」：拿不到大小就跳过，不把未知当成 0，也不拦。
        self._assert_passed_precheck(selection(360), free_bytes=1)

    def test_partial_size_information_skips_the_precheck(self):
        # 分离格式只有一路报了大小：总量不可知，整条跳过而不是拿半份当全份。
        probe = split_selection(1080, video_size=2 * self.GIB, audio_size=None)
        self._assert_passed_precheck(probe, free_bytes=1)

    def test_filesize_approx_is_used(self):
        error, events = self._attempt(selection(360, approx=4 * self.GIB), self.GIB)

        self.assertIsInstance(error, DependencyError)
        self.assertEqual(events[0].data["required_bytes"], 4 * self.GIB)
        self.assertEqual(events[0].data["part_count"], 1)

    def test_filesize_wins_over_approx(self):
        error, events = self._attempt(
            selection(360, size=3 * self.GIB, approx=99 * self.GIB), self.GIB
        )

        self.assertIsInstance(error, DependencyError)
        self.assertEqual(events[0].data["required_bytes"], 3 * self.GIB)

    def test_incomplete_extraction_still_wins_over_space(self):
        """既有失败顺序不变：提取不完整时无论空间多少都报那一条。"""

        probe = split_selection(1080, video_size=4 * self.GIB, audio_size=self.GIB)
        error, events = self._attempt(
            probe,
            free_bytes=1,
            stderr="WARNING: Some web formats have been skipped as they are missing a URL",
        )

        self.assertIsInstance(error, DependencyError)
        self.assertIn("视频格式提取不完整", str(error))
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()

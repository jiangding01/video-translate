import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xvt.artifacts import ArtifactKind, downstream_of
from xvt.checksums import file_checksum
from xvt.download import download_remote_media
from xvt.errors import DependencyError
from xvt.job_store import create_job
from xvt.pipeline.common import add_artifact, record_media_download_selection
from xvt.process import CommandError, CommandResult


def selection(height, *, video_id="abc", language="en", audio=True, duration=10):
    return {
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

                with patch("xvt.download.file_checksum", side_effect=hash_media):
                    result = download_remote_media(
                        "https://youtu.be/abc",
                        root,
                        cookies_from_browser="chrome",
                        proxy="http://127.0.0.1:7890",
                        runner=runner,
                    )
                self.assertEqual(sum("--dump-single-json" in args for args in calls), 2)
                report = json.loads(result.with_suffix(".download.json").read_text())
                return calls, report
            finally:
                self.assertFalse(list(root.glob(".xvt-download-*")))
                if existing or race:
                    self.assertEqual((root / "source.mp4").read_bytes(), b"old video")

    def test_cookie_sabr_falls_back_to_anonymous_hd(self):
        calls, report = self.execute(
            warning="Some web formats have been skipped as they are missing a URL"
        )
        self.assertEqual(report["access"], "anonymous")
        self.assertEqual(report["height"], 1080)
        self.assertNotIn("--cookies-from-browser", calls[2])
        self.assertEqual(calls[2][calls[2].index("-f") + 1], "137+140")
        self.assertTrue(all("--proxy" in args for args in calls[:3]))

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


if __name__ == "__main__":
    unittest.main()

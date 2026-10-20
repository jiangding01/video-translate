import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from xvt.download import RemoteMediaInfo
from xvt.errors import DependencyError
from xvt.job_store import create_job, get_job_directory
from xvt.models import StageName, StageStatus
from xvt.pipeline.common import add_artifact, media_info_from_remote_metadata
from xvt.pipeline.transcription import run_remote_transcription
from xvt.pipeline.transcription.remote import _acquire_remote_job
from xvt.recovery import valid_artifact_path


class RemoteSubtitleFallbackTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.url = "https://youtu.be/abc"
        self.job = create_job(
            self.url,
            self.root,
            {"youtube_subtitles": True, "subtitle_language": "en", "source_strategy": "auto"},
        )
        self.authenticated = RemoteMediaInfo("abc", "Original title", 5000, self.url, (), ("en",))
        self.anonymous = RemoteMediaInfo("abc", "Other title", 6000, self.url, ("en-US",), ())

    def download(self, _url, output_directory, **_kwargs):
        path = Path(output_directory) / "source.en-US.srt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello.\n", encoding="utf-8")
        return path

    def acquire(self, **kwargs):
        return _acquire_remote_job(
            self.job,
            cookies_from_browser="chrome",
            proxy="http://127.0.0.1:7890",
            event_sink=None,
            **kwargs,
        )

    def test_manual_subtitle_fallback_uses_anonymous_download_and_preserves_metadata(self):
        events = []
        with (
            patch(
                "xvt.pipeline.transcription.remote.inspect_remote",
                side_effect=[self.authenticated, self.anonymous],
            ) as inspect,
            patch(
                "xvt.pipeline.transcription.remote.download_remote_subtitles",
                side_effect=self.download,
            ) as download,
            patch("xvt.pipeline.transcription.remote.download_remote_media") as media,
        ):
            _, _, candidate = _acquire_remote_job(
                self.job,
                cookies_from_browser="chrome",
                proxy="http://127.0.0.1:7890",
                event_sink=events.append,
            )
        self.assertEqual(candidate.kind.value, "manual")
        self.assertEqual(inspect.call_count, 2)
        self.assertEqual(
            inspect.call_args.kwargs, {"proxy": "http://127.0.0.1:7890", "anonymous": True}
        )
        self.assertTrue(download.call_args.kwargs["anonymous"])
        self.assertFalse(download.call_args.kwargs["automatic"])
        media.assert_not_called()
        metadata = json.loads(valid_artifact_path(self.job, "remote_info").read_text())
        self.assertEqual(metadata["title"], "Original title")
        self.assertEqual(metadata["duration_ms"], 5000)
        self.assertEqual(metadata["subtitle_languages"], ["en-US"])
        self.assertTrue(metadata["subtitles_anonymous"])
        selected = next(event for event in events if event.type == "remote_subtitles_selected")
        self.assertTrue(selected.data["anonymous"])

    def test_retry_after_subtitle_download_failure_keeps_anonymous_access(self):
        with (
            patch(
                "xvt.pipeline.transcription.remote.inspect_remote",
                side_effect=[self.authenticated, self.anonymous],
            ),
            patch(
                "xvt.pipeline.transcription.remote.download_remote_subtitles",
                side_effect=RuntimeError("interrupted"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self.acquire()
        with (
            patch("xvt.pipeline.transcription.remote.inspect_remote") as inspect,
            patch(
                "xvt.pipeline.transcription.remote.download_remote_subtitles",
                side_effect=self.download,
            ) as download,
        ):
            _, _, candidate = self.acquire()
        inspect.assert_not_called()
        self.assertEqual(candidate.kind.value, "manual")
        self.assertTrue(download.call_args.kwargs["anonymous"])

    def test_probe_failure_does_not_silently_start_asr_and_redacts_credentials(self):
        with (
            patch(
                "xvt.pipeline.transcription.remote.inspect_remote",
                side_effect=[self.authenticated, RuntimeError("https://example.com/?token=secret")],
            ),
            patch("xvt.pipeline.transcription.remote.download_remote_media") as media,
        ):
            with self.assertRaisesRegex(DependencyError, "匿名补查失败") as raised:
                self.acquire()
        self.assertNotIn("secret", str(raised.exception))
        media.assert_not_called()
        self.assertEqual(self.job.stages[StageName.INSPECT].status, StageStatus.RUNNING)

    def test_different_video_is_rejected(self):
        other = RemoteMediaInfo("other", "Other", 5000, self.url, ("en",), ())
        with patch(
            "xvt.pipeline.transcription.remote.inspect_remote",
            side_effect=[self.authenticated, other],
        ):
            with self.assertRaisesRegex(ValueError, "不同的视频"):
                self.acquire()

    def test_cached_legacy_metadata_defaults_to_configured_access(self):
        metadata = asdict(self.anonymous)
        metadata.pop("subtitles_anonymous", None)
        path = get_job_directory(self.job) / "artifacts" / "remote.json"
        self.assertNotIn("subtitles_anonymous", metadata)
        with (
            patch("xvt.pipeline.transcription.remote.inspect_remote", return_value=self.anonymous),
            patch(
                "xvt.pipeline.transcription.remote.download_remote_subtitles",
                side_effect=RuntimeError("stop"),
            ),
        ):
            with self.assertRaises(RuntimeError):
                self.acquire()
        path.write_text(json.dumps(metadata), encoding="utf-8")
        add_artifact(self.job, "remote_info", path, None)
        with (
            patch("xvt.pipeline.transcription.remote.inspect_remote") as inspect,
            patch(
                "xvt.pipeline.transcription.remote.download_remote_subtitles",
                side_effect=self.download,
            ) as download,
        ):
            self.acquire()
        inspect.assert_not_called()
        self.assertNotIn("anonymous", download.call_args.kwargs)

    def test_fallback_is_not_used_without_cookie_or_explicit_request_or_on_other_hosts(self):
        cases = (
            (self.url, True, None),
            (self.url, False, "chrome"),
            ("https://example.com/watch?v=abc", True, "chrome"),
        )
        for url, requested, cookies in cases:
            with self.subTest(url=url, requested=requested, cookies=cookies):
                job = create_job(
                    url, self.root, {**self.job.config, "youtube_subtitles": requested}
                )
                media_path = get_job_directory(job) / "input" / "audio.webm"
                media_path.parent.mkdir(parents=True, exist_ok=True)
                media_path.write_bytes(b"media")
                with (
                    patch(
                        "xvt.pipeline.transcription.remote.inspect_remote",
                        return_value=self.authenticated,
                    ) as inspect,
                    patch(
                        "xvt.pipeline.transcription.remote.download_remote_media",
                        return_value=media_path,
                    ),
                    patch(
                        "xvt.pipeline.transcription.remote.probe_media",
                        return_value=media_info_from_remote_metadata(url, 5000),
                    ),
                ):
                    _acquire_remote_job(
                        job, cookies_from_browser=cookies, proxy=None, event_sink=None
                    )
                self.assertEqual(inspect.call_count, 1)

    def test_no_matching_anonymous_manual_subtitle_preserves_original_asr_choice(self):
        media_path = get_job_directory(self.job) / "input" / "audio.webm"
        media_path.parent.mkdir(parents=True, exist_ok=True)
        media_path.write_bytes(b"media")
        unrelated = RemoteMediaInfo("abc", "Other", 5000, self.url, ("fr",), ("en",))
        with (
            patch(
                "xvt.pipeline.transcription.remote.inspect_remote",
                side_effect=[self.authenticated, unrelated],
            ) as inspect,
            patch("xvt.pipeline.transcription.remote.download_remote_subtitles") as subtitles,
            patch(
                "xvt.pipeline.transcription.remote.download_remote_media", return_value=media_path
            ) as media,
            patch(
                "xvt.pipeline.transcription.remote.probe_media",
                return_value=media_info_from_remote_metadata(self.url, 5000),
            ),
        ):
            _, _, candidate = self.acquire()
        self.assertIsNone(candidate)
        self.assertEqual(inspect.call_count, 2)
        subtitles.assert_not_called()
        self.assertEqual(media.call_args.kwargs["cookies_from_browser"], "chrome")
        metadata = json.loads(valid_artifact_path(self.job, "remote_info").read_text())
        self.assertFalse(metadata["subtitles_anonymous"])

    def test_existing_automatic_subtitle_is_not_reclassified_by_anonymous_probe(self):
        directory = get_job_directory(self.job)
        metadata_path = directory / "artifacts" / "remote.json"
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(json.dumps(asdict(self.authenticated)), encoding="utf-8")
        add_artifact(self.job, "remote_info", metadata_path, None)
        subtitle_path = self.download(self.url, directory / "input" / "subtitles")
        add_artifact(self.job, "downloaded_subtitles", subtitle_path, None)
        media_path = directory / "input" / "audio.webm"
        media_path.write_bytes(b"media")
        self.job.config["workflow"] = {"target_language": "zh-CN"}
        with (
            patch("xvt.pipeline.transcription.remote.inspect_remote") as inspect,
            patch(
                "xvt.pipeline.transcription.remote.download_remote_media", return_value=media_path
            ),
            patch(
                "xvt.pipeline.transcription.remote.probe_media",
                return_value=media_info_from_remote_metadata(self.url, 5000),
            ),
        ):
            _, _, candidate = self.acquire()
        inspect.assert_not_called()
        self.assertEqual(candidate.kind.value, "automatic")
        metadata = json.loads(valid_artifact_path(self.job, "remote_info").read_text())
        self.assertEqual(metadata["subtitle_languages"], [])

    def test_polished_workflow_uses_manual_source_without_transcription(self):
        with (
            patch(
                "xvt.pipeline.transcription.remote.inspect_remote",
                side_effect=[self.authenticated, self.anonymous],
            ),
            patch(
                "xvt.pipeline.transcription.remote.download_remote_subtitles",
                side_effect=self.download,
            ),
            patch("xvt.pipeline.transcription.local.transcribe_audio") as transcribe,
            patch(
                "xvt.pipeline.transcription.remote.download_remote_media",
                side_effect=AssertionError("人工字幕可用时不应下载媒体以进行 ASR"),
            ),
        ):
            job = run_remote_transcription(
                self.url,
                self.root,
                youtube_subtitles=True,
                subtitle_language="en",
                cookies_from_browser="chrome",
                workflow_config={"target_language": "zh-CN", "translation_quality": "polished"},
            )
        transcribe.assert_not_called()
        selection = json.loads(valid_artifact_path(job, "source_selection").read_text())
        self.assertEqual(selection["selected_kind"], "subtitles")
        self.assertEqual(selection["candidate_kind"], "manual")
        cues = json.loads(valid_artifact_path(job, "source_cues").read_text())
        self.assertEqual((cues[0]["id"], cues[0]["start_ms"], cues[0]["end_ms"]), (1, 1000, 2000))

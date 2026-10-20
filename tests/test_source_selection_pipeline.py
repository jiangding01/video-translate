"""Q2 source-selection behavior through the transcription pipeline."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xvt.align import AlignmentResult
from xvt.download import RemoteMediaInfo
from xvt.events import Event
from xvt.job_store import create_job, save_job
from xvt.media import MediaInfo
from xvt.models import StageName, StageStatus, Transcript, Word
from xvt.pipeline import (
    resume_job,
    resume_remote_transcription,
    run_local_transcription,
    run_remote_transcription,
    run_subtitle_transcription,
)
from xvt.process import CommandResult
from xvt.recovery import file_checksum, valid_artifact_path
from xvt.workflow import continue_workflow


class SourceSelectionPipelineTest(unittest.TestCase):
    def _media_info(self, path: Path) -> MediaInfo:
        return MediaInfo(
            path=str(path),
            duration_ms=5_000,
            size_bytes=path.stat().st_size if path.exists() else 0,
            format_name="mp4",
            width=1920,
            height=1080,
            frame_rate=30.0,
            video_codec="h264",
            audio_codec="aac",
            has_video=True,
            has_audio=True,
        )

    def _transcript(self) -> Transcript:
        return Transcript(
            language="en",
            engine="mlx",
            words=(Word("Hello.", 0, 900, 0.9),),
        )

    def _write_normalized_audio(self, args, **_kwargs) -> CommandResult:
        Path(args[-1]).write_bytes(b"audio")
        return CommandResult(tuple(args), 0, "", "")

    def _selection(self, job) -> dict[str, object]:
        artifact = next(item for item in job.artifacts if item.kind == "source_selection")
        return json.loads(Path(artifact.path).read_text(encoding="utf-8"))

    def test_local_media_without_subtitles_auto_uses_asr_and_writes_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            with (
                patch(
                    "xvt.pipeline.transcription.probe_media",
                    return_value=self._media_info(media),
                ),
                patch(
                    "xvt.pipeline.transcription.run_command",
                    side_effect=self._write_normalized_audio,
                ),
                patch(
                    "xvt.pipeline.transcription.transcribe_audio",
                    return_value=self._transcript(),
                ) as transcribe,
            ):
                job = run_local_transcription(media, root)
                selection = self._selection(job)

        transcribe.assert_called_once()
        self.assertEqual(selection["strategy"], "auto")
        self.assertEqual(selection["candidate_kind"], "none")
        self.assertEqual(selection["selected_kind"], "asr")
        self.assertEqual(selection["reason"], "platform_subtitles_missing")

    def test_user_subtitles_auto_preserves_timeline_without_asr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            subtitles = root / "source.srt"
            subtitles.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nHello.\n",
                encoding="utf-8",
            )
            with (
                patch(
                    "xvt.pipeline.transcription.probe_media",
                    return_value=self._media_info(media),
                ),
                patch("xvt.pipeline.transcription.run_command") as run_command,
                patch("xvt.pipeline.transcription.transcribe_audio") as transcribe,
            ):
                job = run_local_transcription(media, root, subtitles=subtitles)
                selection = self._selection(job)
                source_cues = next(item for item in job.artifacts if item.kind == "source_cues")
                source_start_ms = json.loads(Path(source_cues.path).read_text(encoding="utf-8"))[0][
                    "start_ms"
                ]

        run_command.assert_not_called()
        transcribe.assert_not_called()
        self.assertEqual(selection["candidate_kind"], "user")
        self.assertEqual(selection["selected_kind"], "subtitles")
        self.assertEqual(selection["reason"], "user_subtitles_preserved")
        self.assertEqual(source_start_ms, 1_000)
        self.assertIn("source_candidate_qa", {item.kind for item in job.artifacts})

    def test_manual_candidate_errors_fall_back_to_asr_after_low_confidence_alignment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote_info = RemoteMediaInfo(
                "abc", "Example", 5_000, "https://youtu.be/abc", ("en",), ()
            )

            def download_subtitles(_url, output_directory, **_kwargs):
                path = Path(output_directory) / "source.en.srt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    "1\n00:00:00,000 --> 00:00:01,000\nFirst.\n\n"
                    "2\n00:00:00,900 --> 00:00:02,000\nSecond.\n",
                    encoding="utf-8",
                )
                return path

            def download_media(_url, output_directory, **_kwargs):
                path = Path(output_directory) / "audio.m4a"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"audio")
                return path

            def low_confidence_alignment(cues, _transcript):
                return AlignmentResult(tuple(cues), (), 0.0, 1.0, 0.1, ("low confidence",))

            with (
                patch("xvt.pipeline.transcription.inspect_remote", return_value=remote_info),
                patch(
                    "xvt.pipeline.transcription.download_remote_subtitles",
                    side_effect=download_subtitles,
                ),
                patch(
                    "xvt.pipeline.transcription.download_remote_media",
                    side_effect=download_media,
                ),
                patch(
                    "xvt.pipeline.transcription.probe_media",
                    return_value=self._media_info(root / "audio.m4a"),
                ),
                patch(
                    "xvt.pipeline.transcription.run_command",
                    side_effect=self._write_normalized_audio,
                ),
                patch(
                    "xvt.pipeline.transcription.transcribe_audio",
                    return_value=self._transcript(),
                ) as transcribe,
                patch(
                    "xvt.pipeline.transcription.align_source_cues",
                    side_effect=low_confidence_alignment,
                ) as align,
            ):
                job = run_remote_transcription(
                    "https://youtu.be/abc",
                    root,
                    youtube_subtitles=True,
                    subtitle_language="en",
                )
                selection = self._selection(job)

        transcribe.assert_called_once()
        align.assert_called_once()
        self.assertEqual(selection["candidate_kind"], "manual")
        self.assertEqual(selection["selected_kind"], "asr")
        self.assertEqual(selection["reason"], "alignment_below_threshold")
        self.assertFalse(selection["reference_allowed"])
        self.assertEqual(job.stages[StageName.ALIGN].status, StageStatus.SUCCEEDED)
        self.assertEqual(job.stages[StageName.SEGMENT].status, StageStatus.SUCCEEDED)
        self.assertEqual(job.stages[StageName.SUBTITLE_QA].status, StageStatus.SUCCEEDED)

    def _stage_event_sequence(self, events: list[Event], stage: str) -> list[str]:
        return [
            f"{event.type}:{event.data['status']}"
            for event in events
            if event.type.startswith("stage_") and event.data.get("stage") == stage
        ]

    def test_alignment_fallback_records_align_before_handing_over_to_segment(self) -> None:
        """回落 ASR 的那一支：ALIGN 的终态在对齐决策做完时落下，早于 SEGMENT 开工。

        对齐这一步确实**完成了**（算出 alignment、落盘两份产物、做出「不采用」判定），
        只是结果没被采用。终态落在决策完成那一刻，后续回落分段再失败也不会让 ALIGN
        永久停在 `running`。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events: list[Event] = []
            job = self._run_alignment_fallback(root, events=events)

        self.assertEqual(job.stages[StageName.ALIGN].status, StageStatus.SUCCEEDED)
        self.assertEqual(job.stages[StageName.SEGMENT].status, StageStatus.SUCCEEDED)
        self.assertEqual(
            self._stage_event_sequence(events, "align"),
            ["stage_started:running", "stage_completed:succeeded"],
        )
        align_done = next(
            index
            for index, event in enumerate(events)
            if event.type == "stage_completed" and event.data.get("stage") == "align"
        )
        segment_started = next(
            index
            for index, event in enumerate(events)
            if event.type == "stage_started" and event.data.get("stage") == "segment"
        )
        self.assertLess(align_done, segment_started)

    def test_alignment_fallback_segmentation_failure_still_closes_align(self) -> None:
        """回落 ASR 后分段失败时，ALIGN 不得停在 `running`——Job 要能解释自己的状态。

        `segment_transcript` 在这里真的抛出：ASR 词级时间戳全为零时长，过滤后无词可
        分段，于是 `not cues` 触发 `RuntimeError`。此时失败归属仍只属于 SEGMENT。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events: list[Event] = []
            with self.assertRaisesRegex(RuntimeError, "produced no subtitle cues"):
                self._run_alignment_fallback(root, events=events, zero_duration_words=True)
            job_file = next((root / "jobs").glob("*/job.json"))
            data = json.loads(job_file.read_text(encoding="utf-8"))

        self.assertEqual(data["stages"]["align"]["status"], "succeeded")
        self.assertIsNotNone(data["stages"]["align"]["completed_at"])
        self.assertIsNone(data["stages"]["align"]["error"])
        # 失败归属不变：只有 SEGMENT 记 FAILED，且带原始原因。
        self.assertEqual(data["stages"]["segment"]["status"], "failed")
        self.assertEqual(
            data["stages"]["segment"]["error"],
            "transcription produced no subtitle cues",
        )
        self.assertEqual(
            self._stage_event_sequence(events, "align"),
            ["stage_started:running", "stage_completed:succeeded"],
        )
        self.assertEqual(
            self._stage_event_sequence(events, "segment"),
            ["stage_started:running", "stage_failed:failed"],
        )

    def _run_alignment_fallback(
        self,
        root: Path,
        *,
        events: list[Event],
        zero_duration_words: bool = False,
    ):
        """自动策略下「平台人工字幕 QA 失败 → 对齐 → 对齐未被采用 → 回落 ASR」。

        `zero_duration_words` 让 ASR 词级时间戳全为零时长：对齐照样算得出来（文本能
        匹配），但 `segment_transcript` 过滤零时长词后无词可分段而抛出。
        """

        remote_info = RemoteMediaInfo("abc", "Example", 3_000, "https://youtu.be/abc", ("en",), ())

        def download_subtitles(_url, output_directory, **_kwargs):
            path = Path(output_directory) / "source.en.srt"
            path.parent.mkdir(parents=True, exist_ok=True)
            # 第 3 条越出媒体时长：候选 QA 为 error，auto 策略因此选择对齐。
            path.write_text(
                "1\n00:00:00,000 --> 00:00:00,800\nFirst.\n\n"
                "2\n00:00:00,900 --> 00:00:01,700\nSecond.\n\n"
                "3\n00:00:02,600 --> 00:00:04,000\nThird.\n",
                encoding="utf-8",
            )
            return path

        def download_media(_url, output_directory, **_kwargs):
            path = Path(output_directory) / "audio.m4a"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"audio")
            return path

        media_info = MediaInfo(
            path=str(root / "audio.m4a"),
            duration_ms=3_000,
            size_bytes=5,
            format_name="m4a",
            width=None,
            height=None,
            frame_rate=None,
            video_codec=None,
            audio_codec="aac",
            has_video=False,
            has_audio=True,
        )
        if zero_duration_words:
            words = (Word("First.", 200, 200, 0.9), Word("Second.", 2000, 2000, 0.9))
        else:
            words = (Word("First.", 200, 900, 0.9), Word("Second.", 2000, 2700, 0.9))
        transcript = Transcript(language="en", engine="mlx", words=words)

        with (
            patch("xvt.pipeline.transcription.inspect_remote", return_value=remote_info),
            patch(
                "xvt.pipeline.transcription.download_remote_subtitles",
                side_effect=download_subtitles,
            ),
            patch(
                "xvt.pipeline.transcription.download_remote_media",
                side_effect=download_media,
            ),
            patch("xvt.pipeline.transcription.probe_media", return_value=media_info),
            patch(
                "xvt.pipeline.transcription.run_command",
                side_effect=self._write_normalized_audio,
            ),
            patch("xvt.pipeline.transcription.transcribe_audio", return_value=transcript),
        ):
            return run_remote_transcription(
                "https://youtu.be/abc",
                root,
                youtube_subtitles=True,
                subtitle_language="en",
                event_sink=events.append,
            )

    def test_post_qa_alignment_fallback_persists_aligned_subtitle_qa_report(self) -> None:
        """自动回落 ASR 的那一支同样登记报告：`selection_reason` 之外要有 issue 明细。

        这里走真实的 `align_source_cues`：前两条 cue 匹配到词级时间戳，第三条匹配不上
        而被按拟合出的 scale 投影到媒体之外，于是对齐置信度够但对齐后 QA 有 error，
        `alignment_accepted` 为假、源选择回落 ASR。此时 `subtitle_qa` 讲的是 ASR cue，
        对齐候选为什么被拒只有这份报告说得清。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote_info = RemoteMediaInfo(
                "abc", "Example", 3_000, "https://youtu.be/abc", ("en",), ()
            )

            def download_subtitles(_url, output_directory, **_kwargs):
                path = Path(output_directory) / "source.en.srt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    "1\n00:00:00,000 --> 00:00:00,800\nFirst.\n\n"
                    "2\n00:00:00,900 --> 00:00:01,700\nSecond.\n\n"
                    # 超出媒体时长：候选 QA 为 error，自动策略因此选择对齐。
                    "3\n00:00:02,600 --> 00:00:04,000\nThird.\n",
                    encoding="utf-8",
                )
                return path

            def download_media(_url, output_directory, **_kwargs):
                path = Path(output_directory) / "audio.m4a"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"audio")
                return path

            media_info = MediaInfo(
                path=str(root / "audio.m4a"),
                duration_ms=3_000,
                size_bytes=5,
                format_name="m4a",
                width=None,
                height=None,
                frame_rate=None,
                video_codec=None,
                audio_codec="aac",
                has_video=False,
                has_audio=True,
            )
            transcript = Transcript(
                language="en",
                engine="mlx",
                words=(Word("First.", 200, 900, 0.9), Word("Second.", 2000, 2700, 0.9)),
            )

            with (
                patch("xvt.pipeline.transcription.inspect_remote", return_value=remote_info),
                patch(
                    "xvt.pipeline.transcription.download_remote_subtitles",
                    side_effect=download_subtitles,
                ),
                patch(
                    "xvt.pipeline.transcription.download_remote_media",
                    side_effect=download_media,
                ),
                patch("xvt.pipeline.transcription.probe_media", return_value=media_info),
                patch(
                    "xvt.pipeline.transcription.run_command",
                    side_effect=self._write_normalized_audio,
                ),
                patch(
                    "xvt.pipeline.transcription.transcribe_audio",
                    return_value=transcript,
                ),
            ):
                job = run_remote_transcription(
                    "https://youtu.be/abc",
                    root,
                    youtube_subtitles=True,
                    subtitle_language="en",
                )
                selection = self._selection(job)

                self.assertEqual(selection["selected_kind"], "asr")
                self.assertEqual(selection["reason"], "alignment_post_qa_failed")

                report_path = valid_artifact_path(job, "aligned_subtitle_qa")
                self.assertIsNotNone(report_path)
                assert report_path is not None
                artifact = next(
                    item for item in job.artifacts if item.kind == "aligned_subtitle_qa"
                )
                self.assertEqual(artifact.checksum, file_checksum(report_path))
                report = json.loads(report_path.read_text(encoding="utf-8"))
                self.assertEqual(report["status"], "failed")
                errors = [issue for issue in report["issues"] if issue["severity"] == "error"]
                self.assertEqual(
                    [(issue["code"], issue["cue_id"]) for issue in errors],
                    [("outside_media", 3)],
                )
                # 源腿的 qa.json 讲的是最终采用的 ASR cue，不是被拒的对齐候选。
                source_report_path = valid_artifact_path(job, "subtitle_qa")
                self.assertIsNotNone(source_report_path)
                assert source_report_path is not None
                self.assertNotEqual(
                    json.loads(source_report_path.read_text(encoding="utf-8")),
                    report,
                )

    def test_valid_manual_candidate_keeps_remote_fast_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote_info = RemoteMediaInfo(
                "abc", "Example", 5_000, "https://youtu.be/abc", ("en",), ()
            )

            def download_subtitles(_url, output_directory, **_kwargs):
                path = Path(output_directory) / "source.en.srt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    "1\n00:00:00,000 --> 00:00:01,000\nHello.\n",
                    encoding="utf-8",
                )
                return path

            with (
                patch("xvt.pipeline.transcription.inspect_remote", return_value=remote_info),
                patch(
                    "xvt.pipeline.transcription.download_remote_subtitles",
                    side_effect=download_subtitles,
                ),
                patch("xvt.pipeline.transcription.download_remote_media") as download_media,
                patch("xvt.pipeline.transcription.transcribe_audio") as transcribe,
            ):
                job = run_remote_transcription(
                    "https://youtu.be/abc",
                    root,
                    youtube_subtitles=True,
                    subtitle_language="en",
                )
                selection = self._selection(job)

        download_media.assert_not_called()
        transcribe.assert_not_called()
        self.assertEqual(selection["selected_kind"], "subtitles")
        self.assertEqual(selection["reason"], "manual_subtitles_passed")

    def test_platform_candidate_report_survives_media_download_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote_info = RemoteMediaInfo(
                "abc", "Example", 5_000, "https://youtu.be/abc", ("en",), ()
            )

            def download_subtitles(_url, output_directory, **_kwargs):
                path = Path(output_directory) / "source.en.srt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    "1\n00:00:00,000 --> 00:00:01,000\nFirst.\n\n"
                    "2\n00:00:00,900 --> 00:00:02,000\nSecond.\n",
                    encoding="utf-8",
                )
                return path

            with (
                patch("xvt.pipeline.transcription.inspect_remote", return_value=remote_info),
                patch(
                    "xvt.pipeline.transcription.download_remote_subtitles",
                    side_effect=download_subtitles,
                ),
                patch(
                    "xvt.pipeline.transcription.download_remote_media",
                    side_effect=RuntimeError("download unavailable"),
                ),
                self.assertRaisesRegex(RuntimeError, "download unavailable"),
            ):
                run_remote_transcription(
                    "https://youtu.be/abc",
                    root,
                    youtube_subtitles=True,
                    subtitle_language="en",
                )

            job_file = next((root / "jobs").glob("*/job.json"))
            persisted = json.loads(job_file.read_text(encoding="utf-8"))
            artifact_kinds = {item["kind"] for item in persisted["artifacts"]}

        self.assertIn("source_candidate_qa", artifact_kinds)
        self.assertEqual(persisted["stages"]["acquire"]["status"], "failed")

    def test_empty_platform_candidate_falls_back_to_asr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote_info = RemoteMediaInfo(
                "abc", "Example", 5_000, "https://youtu.be/abc", ("en",), ()
            )

            def download_subtitles(_url, output_directory, **_kwargs):
                path = Path(output_directory) / "source.en.srt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("", encoding="utf-8")
                return path

            def download_media(_url, output_directory, **_kwargs):
                path = Path(output_directory) / "audio.m4a"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"audio")
                return path

            with (
                patch("xvt.pipeline.transcription.inspect_remote", return_value=remote_info),
                patch(
                    "xvt.pipeline.transcription.download_remote_subtitles",
                    side_effect=download_subtitles,
                ),
                patch(
                    "xvt.pipeline.transcription.download_remote_media",
                    side_effect=download_media,
                ),
                patch(
                    "xvt.pipeline.transcription.probe_media",
                    return_value=self._media_info(root / "audio.m4a"),
                ),
                patch(
                    "xvt.pipeline.transcription.run_command",
                    side_effect=self._write_normalized_audio,
                ),
                patch(
                    "xvt.pipeline.transcription.transcribe_audio",
                    return_value=self._transcript(),
                ),
            ):
                job = run_remote_transcription(
                    "https://youtu.be/abc",
                    root,
                    youtube_subtitles=True,
                    subtitle_language="en",
                )
                selection = self._selection(job)
                candidate_qa = next(
                    item for item in job.artifacts if item.kind == "source_candidate_qa"
                )
                report = json.loads(Path(candidate_qa.path).read_text(encoding="utf-8"))

        self.assertEqual(selection["selected_kind"], "asr")
        self.assertEqual(report["report"]["issues"][0]["code"], "invalid_candidate")

    def test_automatic_candidate_with_translation_goal_uses_asr_and_allows_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote_info = RemoteMediaInfo(
                "abc", "Example", 5_000, "https://youtu.be/abc", (), ("en",)
            )

            def download_subtitles(_url, output_directory, **_kwargs):
                path = Path(output_directory) / "source.en.srt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    "1\n00:00:00,000 --> 00:00:01,000\nHello.\n",
                    encoding="utf-8",
                )
                return path

            def download_media(_url, output_directory, **_kwargs):
                path = Path(output_directory) / "audio.m4a"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"audio")
                return path

            with (
                patch("xvt.pipeline.transcription.inspect_remote", return_value=remote_info),
                patch(
                    "xvt.pipeline.transcription.download_remote_subtitles",
                    side_effect=download_subtitles,
                ),
                patch(
                    "xvt.pipeline.transcription.download_remote_media",
                    side_effect=download_media,
                ),
                patch(
                    "xvt.pipeline.transcription.probe_media",
                    return_value=self._media_info(root / "audio.m4a"),
                ),
                patch(
                    "xvt.pipeline.transcription.run_command",
                    side_effect=self._write_normalized_audio,
                ),
                patch(
                    "xvt.pipeline.transcription.transcribe_audio",
                    return_value=self._transcript(),
                ) as transcribe,
            ):
                job = run_remote_transcription(
                    "https://youtu.be/abc",
                    root,
                    youtube_subtitles=True,
                    subtitle_language="en",
                    allow_auto_subtitles=True,
                    workflow_config={"target_language": "zh-CN"},
                )
                selection = self._selection(job)

        transcribe.assert_called_once()
        self.assertEqual(selection["candidate_kind"], "automatic")
        self.assertEqual(selection["selected_kind"], "asr")
        self.assertEqual(selection["reason"], "automatic_subtitles_reference_only")
        self.assertTrue(selection["reference_allowed"])

    def test_candidate_report_is_persisted_before_asr_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            subtitles = root / "source.srt"
            subtitles.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello.\n",
                encoding="utf-8",
            )
            with (
                patch(
                    "xvt.pipeline.transcription.probe_media",
                    return_value=self._media_info(media),
                ),
                patch(
                    "xvt.pipeline.transcription.run_command",
                    side_effect=self._write_normalized_audio,
                ),
                patch(
                    "xvt.pipeline.transcription.transcribe_audio",
                    side_effect=RuntimeError("ASR unavailable"),
                ),
                self.assertRaisesRegex(RuntimeError, "ASR unavailable"),
            ):
                run_local_transcription(media, root, subtitles=subtitles, source_strategy="asr")

            job_file = next((root / "jobs").glob("*/job.json"))
            persisted = json.loads(job_file.read_text(encoding="utf-8"))
            artifact_paths = {item["kind"]: Path(item["path"]) for item in persisted["artifacts"]}
            report = json.loads(artifact_paths["source_candidate_qa"].read_text(encoding="utf-8"))

        self.assertEqual(report["candidate_kind"], "user")
        self.assertNotIn("source_selection", artifact_paths)

    def test_invalid_user_candidate_is_persisted_before_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            subtitles = root / "source.srt"
            subtitles.write_text("not an srt", encoding="utf-8")
            with (
                patch(
                    "xvt.pipeline.transcription.probe_media",
                    return_value=self._media_info(media),
                ),
                self.assertRaises(ValueError),
            ):
                run_local_transcription(media, root, subtitles=subtitles)

            job_file = next((root / "jobs").glob("*/job.json"))
            persisted = json.loads(job_file.read_text(encoding="utf-8"))
            report_path = next(
                Path(item["path"])
                for item in persisted["artifacts"]
                if item["kind"] == "source_candidate_qa"
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(report["candidate_kind"], "user")
        self.assertEqual(report["report"]["issues"][0]["code"], "invalid_candidate")

    def test_remote_user_candidate_report_survives_media_download_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subtitles = root / "source.srt"
            subtitles.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello.\n",
                encoding="utf-8",
            )
            remote_info = RemoteMediaInfo("abc", "Example", 5_000, "https://youtu.be/abc", (), ())
            with (
                patch("xvt.pipeline.transcription.inspect_remote", return_value=remote_info),
                patch(
                    "xvt.pipeline.transcription.download_remote_media",
                    side_effect=RuntimeError("download unavailable"),
                ),
                self.assertRaisesRegex(RuntimeError, "download unavailable"),
            ):
                run_remote_transcription(
                    "https://youtu.be/abc",
                    root,
                    subtitles=subtitles,
                    source_strategy="asr",
                )

            job_file = next((root / "jobs").glob("*/job.json"))
            persisted = json.loads(job_file.read_text(encoding="utf-8"))
            artifact_kinds = {item["kind"] for item in persisted["artifacts"]}

        self.assertIn("source_candidate_qa", artifact_kinds)
        self.assertEqual(persisted["stages"]["acquire"]["status"], "failed")

    def test_forced_asr_ignores_unusable_user_subtitle_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            subtitles = root / "source.srt"
            subtitles.write_text("", encoding="utf-8")
            with (
                patch(
                    "xvt.pipeline.transcription.probe_media",
                    return_value=self._media_info(media),
                ),
                patch(
                    "xvt.pipeline.transcription.run_command",
                    side_effect=self._write_normalized_audio,
                ),
                patch(
                    "xvt.pipeline.transcription.transcribe_audio",
                    return_value=self._transcript(),
                ),
            ):
                job = run_local_transcription(
                    media,
                    root,
                    subtitles=subtitles,
                    source_strategy="asr",
                )
                selection = self._selection(job)
                candidate_qa = next(
                    item for item in job.artifacts if item.kind == "source_candidate_qa"
                )
                report = json.loads(Path(candidate_qa.path).read_text(encoding="utf-8"))

        self.assertEqual(selection["selected_kind"], "asr")
        self.assertFalse(selection["reference_allowed"])
        self.assertEqual(report["report"]["issues"][0]["code"], "invalid_candidate")

    def test_non_utf8_candidate_is_downgraded_instead_of_failing_the_job(self) -> None:
        """候选字幕的编码错误仍走「降级为 invalid_candidate」，不能让 Job 失败。

        降级消息里的路径必须是用户传入的原始字幕，不是 Job 内部副本。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            subtitles = root / "source.srt"
            subtitles.write_bytes("1\n00:00:01,000 --> 00:00:02,500\n你好世界\n".encode("gbk"))
            with (
                patch(
                    "xvt.pipeline.transcription.probe_media",
                    return_value=self._media_info(media),
                ),
                patch(
                    "xvt.pipeline.transcription.run_command",
                    side_effect=self._write_normalized_audio,
                ),
                patch(
                    "xvt.pipeline.transcription.transcribe_audio",
                    return_value=self._transcript(),
                ),
            ):
                job = run_local_transcription(
                    media,
                    root,
                    subtitles=subtitles,
                    source_strategy="asr",
                )
                selection = self._selection(job)
                candidate_qa = next(
                    item for item in job.artifacts if item.kind == "source_candidate_qa"
                )
                report = json.loads(Path(candidate_qa.path).read_text(encoding="utf-8"))

        self.assertEqual(selection["selected_kind"], "asr")
        self.assertEqual(job.stages[StageName.IMPORT_SUBTITLES].status, StageStatus.SUCCEEDED)
        issue = report["report"]["issues"][0]
        self.assertEqual(issue["code"], "invalid_candidate")
        self.assertIn("not UTF-8 encoded", issue["message"])
        self.assertIn(str(subtitles), issue["message"])
        self.assertNotIn("input-subtitles", issue["message"])

    def test_subtitle_input_writes_selection_without_candidate_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello.\n",
                encoding="utf-8",
            )
            job = run_subtitle_transcription(source, root)
            selection = self._selection(job)

        self.assertEqual(selection["strategy"], "auto")
        self.assertEqual(selection["candidate_kind"], "user")
        self.assertEqual(selection["reason"], "user_subtitles_preserved")
        self.assertNotIn("source_candidate_qa", {item.kind for item in job.artifacts})

    def test_subtitle_input_rejects_asr_strategy_at_pipeline_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello.\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "requires media input"):
                run_subtitle_transcription(source, root, source_strategy="asr")

    def test_continue_recovers_missing_source_selection_without_reselecting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            subtitles = root / "source.srt"
            subtitles.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nHello.\n",
                encoding="utf-8",
            )
            with patch(
                "xvt.pipeline.transcription.probe_media",
                return_value=self._media_info(media),
            ):
                job = run_local_transcription(
                    media,
                    root,
                    subtitles=subtitles,
                    workflow_config={"target_language": None},
                )
            selection_artifact = next(
                item for item in job.artifacts if item.kind == "source_selection"
            )
            Path(selection_artifact.path).unlink()

            result = continue_workflow(root / "jobs" / job.id / "job.json")
            persisted = json.loads(result.job_file.read_text(encoding="utf-8"))
            recovered_path = next(
                Path(item["path"])
                for item in persisted["artifacts"]
                if item["kind"] == "source_selection"
            )
            recovered = json.loads(recovered_path.read_text(encoding="utf-8"))

        self.assertEqual(recovered["reason"], "selection_artifact_recovered")
        self.assertEqual(recovered["selected_kind"], "subtitles")

    def test_continue_rebuilds_corrupt_candidate_qa(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            subtitles = root / "source.srt"
            subtitles.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nHello.\n",
                encoding="utf-8",
            )
            with patch(
                "xvt.pipeline.transcription.probe_media",
                return_value=self._media_info(media),
            ):
                job = run_local_transcription(
                    media,
                    root,
                    subtitles=subtitles,
                    workflow_config={"target_language": None},
                )
            candidate_qa = next(
                item for item in job.artifacts if item.kind == "source_candidate_qa"
            )
            Path(candidate_qa.path).write_text("corrupt", encoding="utf-8")

            result = continue_workflow(root / "jobs" / job.id / "job.json")
            persisted = json.loads(result.job_file.read_text(encoding="utf-8"))
            rebuilt_path = next(
                Path(item["path"])
                for item in persisted["artifacts"]
                if item["kind"] == "source_candidate_qa"
            )
            rebuilt = json.loads(rebuilt_path.read_text(encoding="utf-8"))

        self.assertEqual(result.status, "completed")
        self.assertEqual(rebuilt["candidate_kind"], "user")
        self.assertEqual(rebuilt["report"]["status"], "pass")

    def test_resume_remote_checkpoint_preserves_automatic_candidate_kind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote_info = RemoteMediaInfo(
                "abc", "Example", 5_000, "https://youtu.be/abc", (), ("en",)
            )

            def download_subtitles(_url, output_directory, **_kwargs):
                path = Path(output_directory) / "source.en.srt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    "1\n00:00:00,000 --> 00:00:01,000\nHello.\n",
                    encoding="utf-8",
                )
                return path

            def download_media(_url, output_directory, **_kwargs):
                path = Path(output_directory) / "audio.m4a"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"audio")
                return path

            with (
                patch("xvt.pipeline.transcription.inspect_remote", return_value=remote_info),
                patch(
                    "xvt.pipeline.transcription.download_remote_subtitles",
                    side_effect=download_subtitles,
                ),
                patch(
                    "xvt.pipeline.transcription.download_remote_media",
                    side_effect=download_media,
                ),
                patch(
                    "xvt.pipeline.transcription.probe_media",
                    return_value=self._media_info(root / "audio.m4a"),
                ),
                patch(
                    "xvt.pipeline.transcription.run_command",
                    side_effect=self._write_normalized_audio,
                ),
                patch(
                    "xvt.pipeline.transcription.transcribe_audio",
                    return_value=self._transcript(),
                ),
                patch(
                    "xvt.pipeline.transcription.segment_transcript",
                    side_effect=RuntimeError("segment interrupted"),
                ),
                self.assertRaisesRegex(RuntimeError, "segment interrupted"),
            ):
                run_remote_transcription(
                    "https://youtu.be/abc",
                    root,
                    youtube_subtitles=True,
                    subtitle_language="en",
                    allow_auto_subtitles=True,
                    workflow_config={"target_language": "zh-CN"},
                )

            job_file = next((root / "jobs").glob("*/job.json"))
            resumed = resume_job(job_file)
            selection = self._selection(resumed)

        self.assertEqual(selection["candidate_kind"], "automatic")
        self.assertEqual(selection["selected_kind"], "asr")
        self.assertEqual(selection["reason"], "automatic_subtitles_reference_only")

    def test_continue_rebuilds_missing_source_srt_without_remote_reselection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote_info = RemoteMediaInfo(
                "abc", "Example", 5_000, "https://youtu.be/abc", ("en",), ()
            )

            def download_subtitles(_url, output_directory, **_kwargs):
                path = Path(output_directory) / "source.en.srt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    "1\n00:00:00,000 --> 00:00:01,000\nHello.\n",
                    encoding="utf-8",
                )
                return path

            with (
                patch("xvt.pipeline.transcription.inspect_remote", return_value=remote_info),
                patch(
                    "xvt.pipeline.transcription.download_remote_subtitles",
                    side_effect=download_subtitles,
                ),
            ):
                job = run_remote_transcription(
                    "https://youtu.be/abc",
                    root,
                    youtube_subtitles=True,
                    subtitle_language="en",
                    workflow_config={"target_language": None},
                )
            source_srt = next(item for item in job.artifacts if item.kind == "source_srt")
            Path(source_srt.path).unlink()

            with (
                patch("xvt.pipeline.transcription.inspect_remote") as inspect_remote,
                patch(
                    "xvt.pipeline.transcription.download_remote_subtitles"
                ) as download_remote_subtitles,
            ):
                result = continue_workflow(root / "jobs" / job.id / "job.json")

            persisted = json.loads(result.job_file.read_text(encoding="utf-8"))
            rebuilt_source_srt = next(
                Path(item["path"])
                for item in persisted["artifacts"]
                if item["kind"] == "source_srt"
            )
            rebuilt_exists = rebuilt_source_srt.is_file()

        inspect_remote.assert_not_called()
        download_remote_subtitles.assert_not_called()
        self.assertTrue(rebuilt_exists)

    def test_old_remote_job_without_source_strategy_uses_legacy_subtitle_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nHello.\n",
                encoding="utf-8",
            )
            job = create_job(
                "https://youtu.be/abc",
                root,
                {"engine": "mlx", "source_language": "en", "subtitles": str(source)},
            )
            job_file = save_job(job)
            remote_info = RemoteMediaInfo("abc", "Example", 5_000, "https://youtu.be/abc", (), ())
            with (
                patch("xvt.pipeline.transcription.inspect_remote", return_value=remote_info),
                patch("xvt.pipeline.transcription.transcribe_audio") as transcribe,
            ):
                resumed = resume_remote_transcription(job_file)
                source_cues = next(item for item in resumed.artifacts if item.kind == "source_cues")
                source_start_ms = json.loads(Path(source_cues.path).read_text(encoding="utf-8"))[0][
                    "start_ms"
                ]

        transcribe.assert_not_called()
        self.assertNotIn("source_selection", {item.kind for item in resumed.artifacts})
        self.assertEqual(source_start_ms, 1_000)


if __name__ == "__main__":
    unittest.main()

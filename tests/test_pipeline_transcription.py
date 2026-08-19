"""转写主链（pipeline.transcription）的流程测试。

覆盖本地媒体、纯字幕输入、远程获取、字幕对齐与 `resume` checkpoint 恢复。
"""

import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from xvt.download import RemoteMediaInfo
from xvt.job_store import create_job, get_job_file, load_job, save_job
from xvt.media import MediaInfo
from xvt.models import Artifact, Cue, StageName, StageStatus, Transcript, Word
from xvt.pipeline import (
    apply_subtitle_translation,
    burn_translated_job,
    preview_translated_job,
    resegment_translation,
    resume_job,
    resume_remote_transcription,
    run_local_transcription,
    run_remote_transcription,
    run_subtitle_transcription,
)
from xvt.process import CommandResult
from xvt.recovery import file_checksum
from xvt.segment import segment_transcript, segmentation_config_for_preset


class TranscriptionPipelineTest(unittest.TestCase):
    def test_subtitle_job_exports_srt_without_media_and_recovers_source_cues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,500\nHello world.\n",
                encoding="utf-8",
            )
            translated = root / "translated.srt"
            translated.write_text(
                "1\n00:00:00,000 --> 00:00:01,500\n你好，世界。\n",
                encoding="utf-8",
            )

            job = run_subtitle_transcription(source, root / "output", language="en")
            self.assertEqual(job.config["input_kind"], "subtitle")
            self.assertNotIn("media_info", {artifact.kind for artifact in job.artifacts})
            self.assertEqual(job.stages[StageName.IMPORT_SUBTITLES].status, StageStatus.SUCCEEDED)
            self.assertEqual(job.stages[StageName.TRANSCRIBE].status, StageStatus.SKIPPED)
            self.assertEqual(job.stages[StageName.SEGMENT].status, StageStatus.SKIPPED)
            self.assertEqual(job.stages[StageName.TRANSLATE].status, StageStatus.SKIPPED)

            paths = apply_subtitle_translation(
                get_job_file(job),
                translated,
                target_language="zh-CN",
            )
            self.assertEqual({path.name for path in paths}, {"translated.srt", "bilingual.srt"})

            source_srt = next(
                artifact for artifact in job.artifacts if artifact.kind == "source_srt"
            )
            Path(source_srt.path).unlink()
            resumed = resume_job(get_job_file(job))
            self.assertTrue(
                next(
                    Path(item.path) for item in resumed.artifacts if item.kind == "source_srt"
                ).is_file()
            )

    def test_subtitle_job_rejects_media_only_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,500\nHello world.\n",
                encoding="utf-8",
            )
            job = run_subtitle_transcription(source, root / "output", language="en")
            job_file = get_job_file(job)

            with self.assertRaisesRegex(ValueError, "preview requires media input"):
                preview_translated_job(job_file)
            with self.assertRaisesRegex(ValueError, "burn-in requires media input"):
                burn_translated_job(job_file)
            with self.assertRaisesRegex(ValueError, "subtitle-only jobs are unsupported"):
                resegment_translation(job_file)

    def test_subtitle_resume_rechecks_failed_qa(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,500\nFirst.\n\n"
                "2\n00:00:01,000 --> 00:00:02,000\nSecond.\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "subtitle QA failed"):
                run_subtitle_transcription(source, root / "output", language="en")
            job_file = next((root / "output" / "jobs").glob("*/job.json"))
            with self.assertRaisesRegex(RuntimeError, "subtitle QA failed"):
                resume_job(job_file)

            self.assertEqual(
                load_job(job_file).stages[StageName.SUBTITLE_QA].status,
                StageStatus.FAILED,
            )

    def test_run_local_transcription_persists_artifacts_and_stage_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            events = []

            def run_ffmpeg(args, **_kwargs):
                Path(args[-1]).write_bytes(b"audio")
                return CommandResult(tuple(args), 0, "", "")

            media_info = MediaInfo(
                path=str(media),
                duration_ms=5000,
                size_bytes=5,
                format_name="mp4",
                width=1920,
                height=1080,
                frame_rate=30.0,
                video_codec="h264",
                audio_codec="aac",
                has_video=True,
                has_audio=True,
            )
            transcript = Transcript(
                language="en",
                engine="mlx",
                words=(Word("Hello.", 1000, 1800, 0.9),),
            )
            with (
                patch("xvt.pipeline.transcription.probe_media", return_value=media_info),
                patch("xvt.pipeline.transcription.run_command", side_effect=run_ffmpeg),
                patch(
                    "xvt.pipeline.transcription.transcribe_audio", return_value=transcript
                ) as transcribe,
                patch(
                    "xvt.pipeline.transcription.segment_transcript", wraps=segment_transcript
                ) as segment,
            ):
                job = run_local_transcription(
                    media,
                    root,
                    segmentation_preset="compact",
                    workflow_config={"target_language": "zh-CN", "burn": True},
                    event_sink=events.append,
                )

            artifact_kinds = {artifact.kind for artifact in job.artifacts}
            self.assertEqual(job.stages[StageName.RENDER].status, StageStatus.SUCCEEDED)
            self.assertEqual(job.stages[StageName.TRANSLATE].status, StageStatus.SKIPPED)
            self.assertIn("source_srt", artifact_kinds)
            self.assertEqual(job.config["workflow"]["target_language"], "zh-CN")
            self.assertTrue(job.config["workflow"]["burn"])
            self.assertEqual(job.config["segmentation_preset"], "compact")
            qa_path = next(Path(item.path) for item in job.artifacts if item.kind == "subtitle_qa")
            self.assertEqual(json.loads(qa_path.read_text())["status"], "pass")
            started = next(event for event in events if event.type == "job_started")
            self.assertEqual(started.data["job_id"], job.id)
            self.assertEqual(started.data["job_file"], str(get_job_file(job)))
            artifact_created = next(event for event in events if event.type == "artifact_created")
            self.assertEqual(artifact_created.data["job_id"], job.id)
            self.assertEqual(artifact_created.data["job_file"], str(get_job_file(job)))
            self.assertEqual(transcribe.call_args.kwargs["total_ms"], 5000)
            self.assertTrue(callable(transcribe.call_args.kwargs["event_sink"]))
            self.assertEqual(segment.call_args.args[1], segmentation_config_for_preset("compact"))

    def test_pipeline_failure_is_persisted_on_current_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            events = []
            with patch(
                "xvt.pipeline.transcription.probe_media", side_effect=ValueError("bad media")
            ):
                with self.assertRaisesRegex(ValueError, "bad media"):
                    run_local_transcription(media, root, event_sink=events.append)

            job_file = next((root / "jobs").glob("*/job.json"))
            data = json.loads(job_file.read_text())
            self.assertEqual(data["stages"]["inspect"]["status"], "failed")
            self.assertEqual(data["stages"]["inspect"]["error"], "bad media")
            stage_failed = next(event for event in events if event.type == "stage_failed")
            self.assertEqual(
                stage_failed.data["error"],
                {"category": "workflow", "message": "bad media"},
            )
            self.assertEqual(Path(stage_failed.data["job_file"]).resolve(), job_file.resolve())

    def test_qa_review_fails_job_instead_of_reporting_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")

            def run_ffmpeg(args, **_kwargs):
                Path(args[-1]).write_bytes(b"audio")
                return CommandResult(tuple(args), 0, "", "")

            media_info = MediaInfo(
                str(media), 5000, 5, "mp4", 1920, 1080, 30.0, "h264", "aac", True, True
            )
            # 硬显示超限触发 review（ASR 低 timing confidence 仅为 warning，不再阻断）。
            transcript = Transcript(
                language="en",
                engine="mlx",
                words=(Word(("Wide " * 20).strip() + ".", 0, 2000, 0.9),),
            )
            with (
                patch("xvt.pipeline.transcription.probe_media", return_value=media_info),
                patch("xvt.pipeline.transcription.run_command", side_effect=run_ffmpeg),
                patch("xvt.pipeline.transcription.transcribe_audio", return_value=transcript),
            ):
                job = run_local_transcription(media, root)

            job_file = get_job_file(job)
            data = json.loads(job_file.read_text())
            self.assertEqual(data["stages"]["subtitle_qa"]["status"], "awaiting_input")
            qa = json.loads((job_file.parent / "artifacts" / "qa.json").read_text())
            self.assertEqual(qa["status"], "needs_review")
            self.assertIn("line_too_wide", {issue["code"] for issue in qa["issues"]})

    def test_resume_reuses_normalized_audio_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root, {"engine": "mlx", "source_language": "en"})
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            audio_path = artifacts / "audio.wav"
            audio_path.write_bytes(b"audio")
            media_path = artifacts / "media.json"
            media_info = MediaInfo(
                "/tmp/input.mp4", 5000, 5, "mp4", 1920, 1080, 30.0, "h264", "aac", True, True
            )
            media_path.write_text(json.dumps(asdict(media_info)), encoding="utf-8")
            job.artifacts.extend(
                [
                    Artifact("normalized_audio", str(audio_path), file_checksum(audio_path)),
                    Artifact("media_info", str(media_path), file_checksum(media_path)),
                ]
            )
            job_file = save_job(job)
            transcript = Transcript(
                language="en",
                engine="mlx",
                words=(Word("Hello.", 1000, 1800, 0.9),),
            )

            job.config["segmentation_preset"] = "compact"
            job_file = save_job(job)
            with (
                patch(
                    "xvt.pipeline.transcription.transcribe_audio", return_value=transcript
                ) as transcribe,
                patch(
                    "xvt.pipeline.transcription.segment_transcript", wraps=segment_transcript
                ) as segment,
            ):
                resumed = resume_job(job_file)

            transcribe.assert_called_once_with(
                audio_path,
                engine="mlx",
                language="en",
                event_sink=None,
                total_ms=5000,
            )
            self.assertEqual(resumed.stages[StageName.RENDER].status, StageStatus.SUCCEEDED)
            self.assertIn("source_srt", {artifact.kind for artifact in resumed.artifacts})
            self.assertEqual(segment.call_args.args[1], segmentation_config_for_preset("compact"))

    def test_resume_reuses_source_cues_without_asr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            cues_path = artifacts / "source-cues.json"
            cues_path.write_text(json.dumps([asdict(Cue(1, 0, 1000, "Hello"))]), encoding="utf-8")
            media_path = artifacts / "media.json"
            media_info = MediaInfo(
                "/tmp/input.mp4", 2000, 5, "mp4", 1920, 1080, 30.0, "h264", "aac", True, True
            )
            media_path.write_text(json.dumps(asdict(media_info)), encoding="utf-8")
            job.artifacts.extend(
                [
                    Artifact("source_cues", str(cues_path), file_checksum(cues_path)),
                    Artifact("media_info", str(media_path), file_checksum(media_path)),
                ]
            )
            job_file = save_job(job)

            with patch("xvt.pipeline.transcription.transcribe_audio") as transcribe:
                resumed = resume_job(job_file)

            transcribe.assert_not_called()
            self.assertEqual(resumed.stages[StageName.RENDER].status, StageStatus.SUCCEEDED)

    def test_resume_persists_failed_stage_for_unknown_segmentation_preset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job(
                "/tmp/input.mp4",
                root,
                {"engine": "mlx", "source_language": "en", "segmentation_preset": "unknown"},
            )
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            audio_path = artifacts / "audio.wav"
            audio_path.write_bytes(b"audio")
            media_path = artifacts / "media.json"
            media_info = MediaInfo(
                "/tmp/input.mp4", 5000, 5, "mp4", 1920, 1080, 30.0, "h264", "aac", True, True
            )
            media_path.write_text(json.dumps(asdict(media_info)), encoding="utf-8")
            job.artifacts.extend(
                [
                    Artifact("normalized_audio", str(audio_path), file_checksum(audio_path)),
                    Artifact("media_info", str(media_path), file_checksum(media_path)),
                ]
            )
            job_file = save_job(job)
            transcript = Transcript(
                language="en",
                engine="mlx",
                words=(Word("Hello.", 1000, 1800, 0.9),),
            )

            with patch("xvt.pipeline.transcription.transcribe_audio", return_value=transcript):
                with self.assertRaisesRegex(ValueError, "unknown segmentation preset"):
                    resume_job(job_file)

            stage = load_job(job_file).stages[StageName.SEGMENT]
            self.assertEqual(stage.status, StageStatus.FAILED)
            self.assertEqual(stage.error, "unknown segmentation preset: unknown")

    def test_resume_rejects_job_without_valid_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)

            with self.assertRaisesRegex(ValueError, "media_info checkpoint"):
                resume_job(save_job(job))

    def test_source_subtitles_skip_asr_and_preserve_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            subtitles = root / "source.srt"
            subtitles.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nHello\n",
                encoding="utf-8",
            )
            media_info = MediaInfo(
                str(media), 5000, 5, "mp4", 1920, 1080, 30.0, "h264", "aac", True, True
            )
            with (
                patch("xvt.pipeline.transcription.probe_media", return_value=media_info),
                patch("xvt.pipeline.transcription.run_command") as run_command_mock,
                patch("xvt.pipeline.transcription.transcribe_audio") as transcribe_mock,
            ):
                job = run_local_transcription(media, root, subtitles=subtitles, language="en")

            run_command_mock.assert_not_called()
            transcribe_mock.assert_not_called()
            self.assertEqual(job.stages[StageName.IMPORT_SUBTITLES].status, StageStatus.SUCCEEDED)
            self.assertEqual(job.stages[StageName.TRANSCRIBE].status, StageStatus.SKIPPED)
            source_cues = next(item for item in job.artifacts if item.kind == "source_cues")
            cue = json.loads(Path(source_cues.path).read_text())[0]
            self.assertEqual((cue["start_ms"], cue["end_ms"]), (1000, 2000))
            self.assertEqual(cue["provenance"], "user_source_subtitle")

    def test_source_subtitles_can_be_aligned_to_asr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            subtitles = root / "source.srt"
            subtitles.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello\n",
                encoding="utf-8",
            )
            media_info = MediaInfo(
                str(media), 5000, 5, "mp4", 1920, 1080, 30.0, "h264", "aac", True, True
            )
            transcript = Transcript(
                language="en",
                engine="mlx",
                words=(Word("Hello", 1500, 2200, 0.9),),
            )

            def run_ffmpeg(args, **_kwargs):
                Path(args[-1]).write_bytes(b"audio")
                return CommandResult(tuple(args), 0, "", "")

            with (
                patch("xvt.pipeline.transcription.probe_media", return_value=media_info),
                patch("xvt.pipeline.transcription.run_command", side_effect=run_ffmpeg),
                patch("xvt.pipeline.transcription.transcribe_audio", return_value=transcript),
            ):
                job = run_local_transcription(
                    media,
                    root,
                    subtitles=subtitles,
                    align_subtitles=True,
                )

            self.assertEqual(job.stages[StageName.ALIGN].status, StageStatus.SUCCEEDED)
            self.assertIn("subtitle_alignment", {item.kind for item in job.artifacts})
            source_cues = next(item for item in job.artifacts if item.kind == "source_cues")
            cue = json.loads(Path(source_cues.path).read_text())[0]
            self.assertEqual((cue["start_ms"], cue["end_ms"]), (1500, 2200))

    def test_low_confidence_subtitle_alignment_fails_with_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            subtitles = root / "source.srt"
            subtitles.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello\n",
                encoding="utf-8",
            )
            media_info = MediaInfo(
                str(media), 5000, 5, "mp4", 1920, 1080, 30.0, "h264", "aac", True, True
            )
            transcript = Transcript(
                language="en",
                engine="mlx",
                words=(Word("Unrelated", 1500, 2200, 0.9),),
            )

            def run_ffmpeg(args, **_kwargs):
                Path(args[-1]).write_bytes(b"audio")
                return CommandResult(tuple(args), 0, "", "")

            with (
                patch("xvt.pipeline.transcription.probe_media", return_value=media_info),
                patch("xvt.pipeline.transcription.run_command", side_effect=run_ffmpeg),
                patch("xvt.pipeline.transcription.transcribe_audio", return_value=transcript),
            ):
                with self.assertRaisesRegex(ValueError, "confidence is below"):
                    run_local_transcription(
                        media,
                        root,
                        subtitles=subtitles,
                        align_subtitles=True,
                    )

            job_file = next((root / "jobs").glob("*/job.json"))
            data = json.loads(job_file.read_text())
            self.assertEqual(data["stages"]["align"]["status"], "failed")
            self.assertTrue((job_file.parent / "artifacts" / "alignment.json").exists())

    def test_remote_pipeline_downloads_audio_then_reuses_local_processing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote_info = RemoteMediaInfo("abc", "Example", 5000, "https://youtu.be/abc", (), ())
            media_info = MediaInfo(
                path="source.m4a",
                duration_ms=5000,
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
                words=(Word("Hello.", 1000, 1800, 0.9),),
            )

            def download(_url, output_directory, **_kwargs):
                path = Path(output_directory) / "source.m4a"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"audio")
                return path

            def run_ffmpeg(args, **_kwargs):
                Path(args[-1]).write_bytes(b"normalized")
                return CommandResult(tuple(args), 0, "", "")

            with (
                patch("xvt.pipeline.transcription.inspect_remote", return_value=remote_info),
                patch("xvt.pipeline.transcription.download_remote_media", side_effect=download),
                patch("xvt.pipeline.transcription.probe_media", return_value=media_info),
                patch("xvt.pipeline.transcription.run_command", side_effect=run_ffmpeg),
                patch("xvt.pipeline.transcription.transcribe_audio", return_value=transcript),
            ):
                job = run_remote_transcription("https://youtu.be/abc", root)

        self.assertEqual(job.stages[StageName.ACQUIRE].status, StageStatus.SUCCEEDED)
        self.assertIn("downloaded_media", {artifact.kind for artifact in job.artifacts})

    def test_remote_pipeline_retries_acquisition_in_the_same_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote_info = RemoteMediaInfo("abc", "Example", 5000, "https://youtu.be/abc", (), ())
            with (
                patch(
                    "xvt.pipeline.transcription.inspect_remote", return_value=remote_info
                ) as inspect,
                patch(
                    "xvt.pipeline.transcription.download_remote_media",
                    side_effect=RuntimeError("temporary download failure"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "temporary download failure"):
                    run_remote_transcription(
                        "https://youtu.be/abc",
                        root,
                        workflow_config={"target_language": None, "burn": False},
                    )

            job_file = next((root / "jobs").glob("*/job.json"))
            media_info = MediaInfo(
                "source.m4a", 5000, 5, "m4a", None, None, None, None, "aac", False, True
            )
            transcript = Transcript(
                language="en",
                engine="mlx",
                words=(Word("Hello.", 1000, 1800, 0.9),),
            )

            def download(_url, output_directory, **_kwargs):
                path = Path(output_directory) / "source.m4a"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"audio")
                return path

            def run_ffmpeg(args, **_kwargs):
                Path(args[-1]).write_bytes(b"normalized")
                return CommandResult(tuple(args), 0, "", "")

            with (
                patch("xvt.pipeline.transcription.inspect_remote") as retry_inspect,
                patch("xvt.pipeline.transcription.download_remote_media", side_effect=download),
                patch("xvt.pipeline.transcription.probe_media", return_value=media_info),
                patch("xvt.pipeline.transcription.run_command", side_effect=run_ffmpeg),
                patch("xvt.pipeline.transcription.transcribe_audio", return_value=transcript),
            ):
                resumed = resume_remote_transcription(job_file)

        inspect.assert_called_once()
        retry_inspect.assert_not_called()
        self.assertEqual(resumed.stages[StageName.ACQUIRE].status, StageStatus.SUCCEEDED)
        self.assertIn("source_cues", {artifact.kind for artifact in resumed.artifacts})

    def test_remote_acquisition_retry_persists_repeated_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job(
                "https://youtu.be/abc",
                root,
                {"workflow": {"target_language": None, "burn": False}},
            )
            job_file = save_job(job)
            with (
                patch(
                    "xvt.pipeline.transcription.inspect_remote",
                    side_effect=RuntimeError("inspection still unavailable"),
                ),
                self.assertRaisesRegex(RuntimeError, "inspection still unavailable"),
            ):
                resume_remote_transcription(job_file)

            persisted = json.loads(job_file.read_text(encoding="utf-8"))

        self.assertEqual(persisted["stages"]["inspect"]["status"], "failed")
        self.assertEqual(persisted["stages"]["inspect"]["error"], "inspection still unavailable")

    def test_remote_retry_reuses_copied_input_subtitles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.srt"
            original.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nHello.\n",
                encoding="utf-8",
            )
            remote_info = RemoteMediaInfo("abc", "Example", 5000, "https://youtu.be/abc", (), ())
            with (
                patch("xvt.pipeline.transcription.inspect_remote", return_value=remote_info),
                patch(
                    "xvt.pipeline.transcription.check_subtitles",
                    side_effect=RuntimeError("QA unavailable"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "QA unavailable"):
                    run_remote_transcription(
                        "https://youtu.be/abc",
                        root,
                        subtitles=original,
                        workflow_config={"target_language": None, "burn": False},
                    )

            job_file = next((root / "jobs").glob("*/job.json"))
            original.unlink()
            with (
                patch("xvt.pipeline.transcription.inspect_remote") as inspect,
                patch("xvt.pipeline.transcription.download_remote_media") as download,
            ):
                resumed = resume_remote_transcription(job_file)

        inspect.assert_not_called()
        download.assert_not_called()
        self.assertIn("source_cues", {artifact.kind for artifact in resumed.artifacts})

    def test_remote_source_subtitles_skip_unused_audio_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subtitles = root / "source.srt"
            subtitles.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nHello.\n",
                encoding="utf-8",
            )
            remote_info = RemoteMediaInfo(
                "abc",
                "Example",
                5000,
                "https://youtu.be/abc",
                (),
                (),
            )

            with (
                patch("xvt.pipeline.transcription.inspect_remote", return_value=remote_info),
                patch("xvt.pipeline.transcription.download_remote_media") as download,
                patch("xvt.pipeline.transcription.transcribe_audio") as transcribe,
            ):
                job = run_remote_transcription(
                    "https://youtu.be/abc",
                    root,
                    subtitles=subtitles,
                    language="en",
                )

            media_path = next(
                Path(artifact.path) for artifact in job.artifacts if artifact.kind == "media_info"
            )
            media_data = json.loads(media_path.read_text(encoding="utf-8"))

        download.assert_not_called()
        transcribe.assert_not_called()
        self.assertEqual(job.stages[StageName.ACQUIRE].status, StageStatus.SKIPPED)
        self.assertEqual(job.stages[StageName.TRANSCRIBE].status, StageStatus.SKIPPED)
        self.assertEqual(media_data["duration_ms"], 5000)
        self.assertNotIn("downloaded_media", {artifact.kind for artifact in job.artifacts})

    def test_remote_burn_workflow_downloads_full_video_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subtitles = root / "source.srt"
            subtitles.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nHello.\n",
                encoding="utf-8",
            )
            remote_info = RemoteMediaInfo("abc", "Example", 5000, "https://youtu.be/abc", (), ())
            media_info = MediaInfo(
                "source.mp4", 5000, 5, "mp4", 1920, 1080, 30.0, "h264", "aac", True, True
            )

            def download(_url, output_directory, **_kwargs):
                path = Path(output_directory) / "source.mp4"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"video")
                return path

            with (
                patch("xvt.pipeline.transcription.inspect_remote", return_value=remote_info),
                patch(
                    "xvt.pipeline.transcription.download_remote_media", side_effect=download
                ) as acquire,
                patch("xvt.pipeline.transcription.probe_media", return_value=media_info),
                patch("xvt.pipeline.transcription.transcribe_audio") as transcribe,
            ):
                job = run_remote_transcription(
                    "https://youtu.be/abc",
                    root,
                    subtitles=subtitles,
                    language="en",
                    workflow_config={"target_language": "zh-CN", "burn": True},
                )

        self.assertFalse(acquire.call_args.kwargs["audio_only"])
        transcribe.assert_not_called()
        self.assertIn("source_video", {artifact.kind for artifact in job.artifacts})

    def test_remote_pipeline_prefers_manual_youtube_subtitles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote_info = RemoteMediaInfo(
                "abc",
                "Example",
                5000,
                "https://youtu.be/abc",
                ("en-US",),
                ("en",),
            )

            def download_subtitles(_url, output_directory, **kwargs):
                path = Path(output_directory) / "source.en-US.srt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    "1\n00:00:01,000 --> 00:00:02,000\nHello.\n",
                    encoding="utf-8",
                )
                self.assertEqual(kwargs["language"], "en-US")
                self.assertFalse(kwargs["automatic"])
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
                    allow_auto_subtitles=True,
                )

        download_media.assert_not_called()
        transcribe.assert_not_called()
        self.assertEqual(job.stages[StageName.ACQUIRE].status, StageStatus.SUCCEEDED)
        artifact_kinds = {artifact.kind for artifact in job.artifacts}
        self.assertIn("downloaded_subtitles", artifact_kinds)
        self.assertIn("source_srt", artifact_kinds)


if __name__ == "__main__":
    unittest.main()

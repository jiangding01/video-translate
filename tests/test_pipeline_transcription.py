"""转写主链（pipeline.transcription）的流程测试。

覆盖本地媒体、纯字幕输入、远程获取、字幕对齐与 `resume` checkpoint 恢复。
"""

import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from xvt.align import AlignmentResult
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
from xvt.recovery import (
    file_checksum,
    subtitle_qa_failure_guidance,
    valid_artifact_path,
)
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

    def test_resume_keeps_the_translated_review_block_on_the_shared_qa_stage(self) -> None:
        """源腿重跑不得把译文腿尚未解决的 QA 阻断抹成「已通过」。

        `SUBTITLE_QA` 这个 stage 名由源腿与译文腿共用。译文 QA 停在 needs_review 时，
        唯一出口是 `continue --acknowledge-subtitle-review`；`xvt resume` 重跑源字幕 QA
        后若把这个 stage 写成 SUCCEEDED，`subtitle_qa_failure_guidance` 就读不到阻断，
        恢复导航会从「去确认」降级成一条必然回到等待态的 `continue`。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:02,000\nHello there.\n",
                encoding="utf-8",
            )
            translated = root / "translated.srt"
            translated.write_text(
                "1\n00:00:00,000 --> 00:00:02,000\n"
                "你好这一行译文被故意写得非常非常长用来触发显示宽度复核"
                "从而让译文 QA 停在人工确认上\n",
                encoding="utf-8",
            )

            job = run_subtitle_transcription(source, root / "output", language="en")
            job_file = get_job_file(job)
            apply_subtitle_translation(job_file, translated, target_language="zh")

            blocked = load_job(job_file)
            self.assertEqual(
                blocked.stages[StageName.SUBTITLE_QA].status, StageStatus.AWAITING_INPUT
            )
            self.assertIsNotNone(valid_artifact_path(blocked, "translated_subtitle_qa"))
            before = subtitle_qa_failure_guidance(blocked, job_file)
            self.assertIsNotNone(before)
            assert before is not None
            self.assertIn("--acknowledge-subtitle-review", before.primary_command or "")

            source_srt = next(item for item in blocked.artifacts if item.kind == "source_srt")
            Path(source_srt.path).unlink()
            resume_job(job_file)

            resumed = load_job(job_file)
            # 源腿照常把被删掉的 source.srt 补回来——守卫只拦 stage 降级，不拦恢复本职。
            self.assertTrue(
                next(
                    Path(item.path) for item in resumed.artifacts if item.kind == "source_srt"
                ).is_file()
            )
            self.assertEqual(
                resumed.stages[StageName.SUBTITLE_QA].status, StageStatus.AWAITING_INPUT
            )
            after = subtitle_qa_failure_guidance(resumed, job_file)
            self.assertIsNotNone(after)
            assert after is not None
            self.assertIn("--acknowledge-subtitle-review", after.primary_command or "")

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

    @staticmethod
    def _silent_ffmpeg(args, **_kwargs):
        Path(args[-1]).write_bytes(b"audio")
        return CommandResult(tuple(args), 0, "", "")

    @staticmethod
    def _media_info(path: Path) -> MediaInfo:
        return MediaInfo(str(path), 5000, 5, "mp4", 1920, 1080, 30.0, "h264", "aac", True, True)

    def test_asr_failure_is_attributed_to_transcribe_not_import_subtitles(self) -> None:
        """本地媒体转写失败必须记在 TRANSCRIBE 上，且不得留下 running 的 stage。

        自适应源选择路径调 `_normalize_audio_and_transcribe` 前不推进外层 current_stage，
        失败就会记到 IMPORT_SUBTITLES（本次运行根本没导入字幕），TRANSCRIBE 永远停在 running。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")

            with (
                patch(
                    "xvt.pipeline.transcription.probe_media",
                    return_value=self._media_info(media),
                ),
                patch("xvt.pipeline.transcription.run_command", side_effect=self._silent_ffmpeg),
                patch(
                    "xvt.pipeline.transcription.transcribe_audio",
                    side_effect=RuntimeError("mlx-whisper model download failed"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "model download failed"):
                    run_local_transcription(media, root, language="ja")

            data = json.loads(next((root / "jobs").glob("*/job.json")).read_text())
            self.assertEqual(data["stages"]["transcribe"]["status"], "failed")
            self.assertEqual(
                data["stages"]["transcribe"]["error"], "mlx-whisper model download failed"
            )
            self.assertEqual(data["stages"]["import_subtitles"]["status"], "skipped")
            self.assertIsNone(data["stages"]["import_subtitles"]["error"])
            self.assertNotIn("running", {record["status"] for record in data["stages"].values()})

    def test_normalize_audio_failure_is_attributed_to_normalize_audio(self) -> None:
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
                    side_effect=RuntimeError("ffmpeg exited with 1"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "ffmpeg exited with 1"):
                    run_local_transcription(media, root, language="ja")

            data = json.loads(next((root / "jobs").glob("*/job.json")).read_text())
            self.assertEqual(data["stages"]["normalize_audio"]["status"], "failed")
            self.assertEqual(data["stages"]["import_subtitles"]["status"], "skipped")
            self.assertNotIn("running", {record["status"] for record in data["stages"].values()})

    def test_engine_fallback_is_written_back_to_the_job_config(self) -> None:
        """`--engine faster` 实际落到 mlx 时，Job 的 config 不得继续声称 faster。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            transcript = Transcript(
                language="ja",
                engine="mlx",
                words=(Word("こんにちは。", 0, 1200, 0.9),),
            )

            with (
                patch(
                    "xvt.pipeline.transcription.probe_media",
                    return_value=self._media_info(media),
                ),
                patch("xvt.pipeline.transcription.run_command", side_effect=self._silent_ffmpeg),
                patch("xvt.pipeline.transcription.transcribe_audio", return_value=transcript),
            ):
                job = run_local_transcription(media, root, engine="faster", language="ja")

            self.assertEqual(job.config["engine"], "mlx")
            data = json.loads(get_job_file(job).read_text())
            self.assertEqual(data["config"]["engine"], "mlx")
            words = json.loads(
                next(
                    Path(item.path) for item in job.artifacts if item.kind == "word_transcript"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(words["engine"], "mlx")

    def test_reused_audio_checkpoint_keeps_the_original_stage_timestamps(self) -> None:
        """复用 normalized_audio checkpoint 不做任何事，不得刷新 completed_at。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            transcript = Transcript(
                language="ja", engine="mlx", words=(Word("こんにちは。", 0, 1200, 0.9),)
            )

            with (
                patch(
                    "xvt.pipeline.transcription.probe_media",
                    return_value=self._media_info(media),
                ),
                patch("xvt.pipeline.transcription.run_command", side_effect=self._silent_ffmpeg),
                patch(
                    "xvt.pipeline.transcription.transcribe_audio",
                    side_effect=RuntimeError("interrupted"),
                ),
            ):
                with self.assertRaises(RuntimeError):
                    run_local_transcription(media, root, language="ja")

            job_file = next((root / "jobs").glob("*/job.json"))
            before = json.loads(job_file.read_text())["stages"]["normalize_audio"]
            self.assertEqual(before["status"], "succeeded")

            with (
                patch("xvt.pipeline.transcription.run_command", side_effect=self._silent_ffmpeg),
                patch("xvt.pipeline.transcription.transcribe_audio", return_value=transcript),
            ):
                resume_job(job_file)

            after = json.loads(job_file.read_text())["stages"]["normalize_audio"]
            self.assertEqual(after["started_at"], before["started_at"])
            self.assertEqual(after["completed_at"], before["completed_at"])

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

    def test_resume_records_source_render_failure_on_the_render_stage(self) -> None:
        """渲染段的非 QA 异常必须落到 render 上，且不污染已通过的 subtitle_qa。

        走 `resume_job` 的 source_cues 复用入口（`_finish_resumed_source` 的第一个
        调用点）。缺这条归属时 render 永久停在 running，`job.json` 里没有任何失败
        记录，违反「Job 能解释当前状态」。
        """

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

            with patch(
                "xvt.pipeline.transcription.save_subtitles",
                side_effect=OSError("[Errno 28] No space left on device"),
            ):
                with self.assertRaisesRegex(OSError, "No space left on device"):
                    resume_job(job_file)

            reloaded = load_job(job_file)
            render = reloaded.stages[StageName.RENDER]
            self.assertEqual(render.status, StageStatus.FAILED)
            self.assertEqual(render.error, "[Errno 28] No space left on device")
            self.assertEqual(reloaded.stages[StageName.SUBTITLE_QA].status, StageStatus.SUCCEEDED)
            self.assertIsNone(reloaded.stages[StageName.SUBTITLE_QA].error)

    def test_resume_records_source_qa_segment_failure_on_the_qa_stage(self) -> None:
        """QA 段的非 QA 异常必须落到 subtitle_qa 上，而不是留下 running。

        走 `resume_job` 的词级转写 → 分段入口（`_finish_resumed_source` 的第二个
        调用点）。`_run_source_subtitle_qa` 在 `set_stage(SUBTITLE_QA, RUNNING)`
        之后才跑 `check_subtitles` / 写盘 / 登记 artifact，这中间任何非 QA 异常
        （磁盘满、权限错、测量崩溃）都属于这一段。
        """

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

            with (
                patch("xvt.pipeline.transcription.transcribe_audio", return_value=transcript),
                patch(
                    "xvt.pipeline.transcription.check_subtitles",
                    side_effect=OSError("[Errno 13] Permission denied"),
                ),
            ):
                with self.assertRaisesRegex(OSError, "Permission denied"):
                    resume_job(job_file)

            reloaded = load_job(job_file)
            qa_stage = reloaded.stages[StageName.SUBTITLE_QA]
            self.assertEqual(qa_stage.status, StageStatus.FAILED)
            self.assertEqual(qa_stage.error, "[Errno 13] Permission denied")
            self.assertEqual(reloaded.stages[StageName.SEGMENT].status, StageStatus.SUCCEEDED)
            self.assertEqual(reloaded.stages[StageName.RENDER].status, StageStatus.PENDING)

    def test_resume_alignment_confidence_failure_reports_the_measured_numbers(self) -> None:
        """resume 侧置信度门的 error 必须和自适应路径一样带上实测值与阈值。

        自适应路径（`_process_adaptive_media`）的同一失败经外层 except 边界写出
        `"... threshold (0.000 < 0.550)"`；resume 自己这份只写了不带数值的前半句，
        于是同一种失败在 `job.json` 与 `status --json` 里有两种详细程度。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job(
                "/tmp/input.mp4",
                root,
                {
                    "engine": "mlx",
                    "source_language": "en",
                    "align_subtitles": True,
                    "minimum_alignment_confidence": 0.55,
                },
            )
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            audio_path = artifacts / "audio.wav"
            audio_path.write_bytes(b"audio")
            subtitles_path = artifacts / "input.srt"
            subtitles_path.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nHello.\n", encoding="utf-8"
            )
            media_path = artifacts / "media.json"
            media_info = MediaInfo(
                "/tmp/input.mp4", 5000, 5, "mp4", 1920, 1080, 30.0, "h264", "aac", True, True
            )
            media_path.write_text(json.dumps(asdict(media_info)), encoding="utf-8")
            job.artifacts.extend(
                [
                    Artifact("normalized_audio", str(audio_path), file_checksum(audio_path)),
                    Artifact("media_info", str(media_path), file_checksum(media_path)),
                    Artifact("input_subtitles", str(subtitles_path), file_checksum(subtitles_path)),
                ]
            )
            job_file = save_job(job)
            transcript = Transcript(
                language="en",
                engine="mlx",
                words=(Word("Hello.", 1000, 1800, 0.9),),
            )
            alignment = AlignmentResult(
                cues=(Cue(1, 1000, 2000, "Hello."),),
                matches=(),
                offset_ms=0.0,
                scale=1.0,
                confidence=0.231,
                warnings=(),
            )

            with (
                patch("xvt.pipeline.transcription.transcribe_audio", return_value=transcript),
                patch(
                    "xvt.pipeline.transcription.align_source_cues",
                    return_value=alignment,
                ),
            ):
                with self.assertRaisesRegex(ValueError, "below the required threshold"):
                    resume_job(job_file)

            align_stage = load_job(job_file).stages[StageName.ALIGN]
            self.assertEqual(align_stage.status, StageStatus.FAILED)
            self.assertEqual(
                align_stage.error,
                "subtitle alignment confidence is below the required threshold (0.231 < 0.550)",
            )

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

    def _remote_media_info_only_job(self, root: Path):
        """构造远程 Job 的「media_info 有效 + source_cues 缺失 + 有 source_strategy」断点态。"""
        job = create_job(
            "https://youtu.be/abc",
            root,
            {"engine": "mlx", "source_language": "en", "source_strategy": "asr"},
        )
        artifacts = root / "jobs" / job.id / "artifacts"
        artifacts.mkdir()
        media_path = artifacts / "media.json"
        media_info = MediaInfo(
            "https://youtu.be/abc", 5000, 5, "mp4", 1920, 1080, 30.0, "h264", "aac", True, True
        )
        media_path.write_text(json.dumps(asdict(media_info)), encoding="utf-8")
        job.artifacts.append(Artifact("media_info", str(media_path), file_checksum(media_path)))
        return job, artifacts

    def test_resume_remote_job_uses_downloaded_media_instead_of_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job, artifacts = self._remote_media_info_only_job(root)
            downloaded = root / "jobs" / job.id / "input" / "video.mp4"
            downloaded.parent.mkdir(parents=True)
            downloaded.write_bytes(b"video")
            job.artifacts.append(
                Artifact("downloaded_media", str(downloaded), file_checksum(downloaded))
            )
            job_file = save_job(job)

            def run_ffmpeg(args, **_kwargs):
                Path(args[-1]).write_bytes(b"audio")
                return CommandResult(tuple(args), 0, "", "")

            transcript = Transcript(
                language="en",
                engine="mlx",
                words=(Word("Hello.", 1000, 1800, 0.9),),
            )
            with (
                patch(
                    "xvt.pipeline.transcription.run_command", side_effect=run_ffmpeg
                ) as run_command_mock,
                patch("xvt.pipeline.transcription.transcribe_audio", return_value=transcript),
            ):
                resume_job(job_file)

            ffmpeg_args = run_command_mock.call_args.args[0]
            self.assertIn(str(downloaded), ffmpeg_args)
            self.assertFalse(
                any(arg.startswith("https:/") for arg in ffmpeg_args),
                f"remote URL leaked into ffmpeg arguments: {ffmpeg_args}",
            )

    def test_resume_remote_job_without_local_media_delegates_to_remote_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job, _artifacts = self._remote_media_info_only_job(root)
            job_file = save_job(job)

            with (
                patch("xvt.pipeline.transcription.run_command") as run_command_mock,
                patch(
                    "xvt.pipeline.transcription.resume_remote_transcription", return_value=job
                ) as remote_resume,
            ):
                resume_job(job_file)

            run_command_mock.assert_not_called()
            remote_resume.assert_called_once_with(job_file, event_sink=None)

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

    def test_forced_alignment_allows_low_confidence_when_post_qa_is_clean(self) -> None:
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
                job = run_local_transcription(
                    media,
                    root,
                    subtitles=subtitles,
                    align_subtitles=True,
                    allow_low_confidence_alignment=True,
                )

            self.assertEqual(job.stages[StageName.ALIGN].status, StageStatus.SUCCEEDED)
            selection = next(item for item in job.artifacts if item.kind == "source_selection")
            self.assertEqual(
                json.loads(Path(selection.path).read_text())["selected_kind"],
                "aligned_subtitles",
            )

    def test_forced_alignment_rejects_error_level_post_alignment_qa(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            subtitles = root / "source.srt"
            subtitles.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello\n",
                encoding="utf-8",
            )
            # 媒体时长短于对齐后的 cue：对齐结果高置信但触发 outside_media error。
            media_info = MediaInfo(
                str(media), 1000, 5, "mp4", 1920, 1080, 30.0, "h264", "aac", True, True
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
                with self.assertRaisesRegex(ValueError, "error-level issues") as raised:
                    run_local_transcription(
                        media,
                        root,
                        subtitles=subtitles,
                        align_subtitles=True,
                    )

            # 去掉 --align-subtitles 后走的是「保留用户原时间轴」，不是 ASR 分段：
            # 文案不得再承诺 ASR 回退，须指出 --source-strategy asr 才是 ASR 路径。
            message = str(raised.exception)
            self.assertNotIn("fall back to ASR", message)
            self.assertIn("keep the source subtitle timeline as-is", message)
            self.assertIn("--source-strategy asr", message)

            job_file = next((root / "jobs").glob("*/job.json"))
            data = json.loads(job_file.read_text())
            self.assertEqual(data["stages"]["align"]["status"], "failed")

    def test_forced_alignment_failure_persists_aligned_subtitle_qa_report(self) -> None:
        """强制对齐被 QA error 门拒绝时，对齐后 QA 报告必须留下可校验的产物。

        约束 4 不受影响：落盘只是证据。失败仍然是失败——本测试同时钉住 `source_cues`、
        `source_srt`、`subtitle_qa` 一个都没有登记，`align` 仍是 FAILED。
        报告从磁盘上的 `job.json` 重新加载后仍在，证明登记发生在抛出之前。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            subtitles = root / "source.srt"
            subtitles.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello\n",
                encoding="utf-8",
            )
            # 媒体时长短于对齐后的 cue：对齐结果高置信但触发 outside_media error。
            media_info = MediaInfo(
                str(media), 1000, 5, "mp4", 1920, 1080, 30.0, "h264", "aac", True, True
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
                with self.assertRaisesRegex(ValueError, "error-level issues"):
                    run_local_transcription(
                        media,
                        root,
                        subtitles=subtitles,
                        align_subtitles=True,
                    )

            job_file = next((root / "jobs").glob("*/job.json"))
            job = load_job(job_file)
            self.assertEqual(job.stages[StageName.ALIGN].status, StageStatus.FAILED)
            kinds = {artifact.kind for artifact in job.artifacts}
            self.assertIn("aligned_subtitle_qa", kinds)
            self.assertFalse(kinds & {"source_cues", "source_srt", "subtitle_qa"})

            report_path = valid_artifact_path(job, "aligned_subtitle_qa")
            self.assertIsNotNone(report_path)
            assert report_path is not None
            self.assertEqual(report_path.name, "aligned-subtitle-qa.json")
            artifact = next(item for item in job.artifacts if item.kind == "aligned_subtitle_qa")
            self.assertEqual(artifact.checksum, file_checksum(report_path))

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["cue_count"], 1)
            errors = [issue for issue in report["issues"] if issue["severity"] == "error"]
            self.assertEqual(
                [(issue["code"], issue["cue_id"]) for issue in errors],
                [("outside_media", 1)],
            )

    def test_accepted_alignment_persists_report_without_changing_outcome(self) -> None:
        """对齐被接受时行为一字不变，报告只是多一份与 `qa.json` 同内容的证据。"""

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
                words=(Word("Hello", 500, 1400, 0.9),),
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
            self.assertEqual(job.stages[StageName.SEGMENT].status, StageStatus.SKIPPED)
            self.assertEqual(job.stages[StageName.SUBTITLE_QA].status, StageStatus.SUCCEEDED)
            self.assertEqual(job.stages[StageName.RENDER].status, StageStatus.SUCCEEDED)

            cues_path = valid_artifact_path(job, "source_cues")
            self.assertIsNotNone(cues_path)
            assert cues_path is not None
            cues = json.loads(cues_path.read_text(encoding="utf-8"))
            self.assertEqual([cue["start_ms"] for cue in cues], [500])
            self.assertEqual([cue["provenance"] for cue in cues], ["aligned"])

            aligned_report_path = valid_artifact_path(job, "aligned_subtitle_qa")
            source_report_path = valid_artifact_path(job, "subtitle_qa")
            self.assertIsNotNone(aligned_report_path)
            self.assertIsNotNone(source_report_path)
            assert aligned_report_path is not None and source_report_path is not None
            # 采用对齐结果时两份报告检查的是同一批 cue，内容必须逐字相同。
            self.assertEqual(
                json.loads(aligned_report_path.read_text(encoding="utf-8")),
                json.loads(source_report_path.read_text(encoding="utf-8")),
            )
            self.assertNotEqual(aligned_report_path, source_report_path)

    def test_subtitle_source_without_alignment_registers_no_aligned_report(self) -> None:
        """没跑对齐的 Job 不得凭空多出这份报告：它只在对齐真的跑过时存在。"""

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

            with patch("xvt.pipeline.transcription.probe_media", return_value=media_info):
                job = run_local_transcription(media, root, subtitles=subtitles)

            self.assertEqual(job.stages[StageName.ALIGN].status, StageStatus.SKIPPED)
            self.assertNotIn("aligned_subtitle_qa", {artifact.kind for artifact in job.artifacts})

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

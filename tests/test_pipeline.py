import json
import os
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
    apply_agent_translation,
    apply_cue_retranslation,
    apply_subtitle_translation,
    burn_translated_job,
    prepare_agent_translation,
    preview_translated_job,
    resegment_translation,
    resume_job,
    resume_remote_transcription,
    retranslate_cues,
    run_local_transcription,
    run_remote_transcription,
    run_subtitle_transcription,
)
from xvt.process import CommandResult
from xvt.qa import check_subtitles, display_width, display_width_limit
from xvt.recovery import file_checksum
from xvt.segment import SegmentationConfig, segment_transcript, segmentation_config_for_preset


class PipelineTest(unittest.TestCase):
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

    def test_segmenter_respects_language_display_width(self) -> None:
        tokens = (
            "This workflow keeps every generated bilingual subtitle safely inside the video frame."
        ).split()
        transcript = Transcript(
            language="en",
            engine="mlx",
            words=tuple(
                Word(("" if index == 0 else " ") + token, index * 300, index * 300 + 250, 0.9)
                for index, token in enumerate(tokens)
            ),
        )

        cues = segment_transcript(transcript)

        self.assertGreater(len(cues), 1)
        self.assertTrue(
            all(display_width(cue.source_text) <= display_width_limit("en") for cue in cues)
        )

        balanced = segment_transcript(
            Transcript(
                language="en",
                engine="mlx",
                words=tuple(
                    Word(
                        ("" if index == 0 else " ") + token,
                        index * 300,
                        index * 300 + 250,
                        0.9,
                    )
                    for index, token in enumerate(
                        "And now Codex handles this next one for me.".split()
                    )
                ),
            )
        )
        self.assertEqual(
            [cue.source_text for cue in balanced],
            ["And now Codex handles this next one for me."],
        )

        long_sentence = (
            "It remembers where our metadata lives, how the upload package is organized, "
            "and how we add captions, save, and verify each upload."
        )
        balanced_long = segment_transcript(
            Transcript(
                language="en",
                engine="mlx",
                words=tuple(
                    Word(
                        ("" if index == 0 else " ") + token,
                        index * 300,
                        index * 300 + 250,
                        0.9,
                    )
                    for index, token in enumerate(long_sentence.split())
                ),
            )
        )
        self.assertTrue(all(len(cue.source_text.split()) > 1 for cue in balanced_long))

    def test_segmenter_prefers_semantic_boundaries_when_splitting_by_width(self) -> None:
        text = (
            "We should preserve the meaning of each subtitle, but still keep every line readable."
        )
        transcript = Transcript(
            language="en",
            engine="mlx",
            words=tuple(
                Word(
                    ("" if index == 0 else " ") + token,
                    index * 300,
                    index * 300 + 250,
                    0.9,
                )
                for index, token in enumerate(text.split())
            ),
        )

        cues = segment_transcript(transcript, SegmentationConfig(max_characters=50))

        self.assertEqual(
            [cue.source_text for cue in cues],
            [
                "We should preserve the meaning of each subtitle,",
                "but still keep every line readable.",
            ],
        )
        self.assertEqual([(cue.start_ms, cue.end_ms) for cue in cues], [(0, 2400), (2400, 4300)])

    def test_segmenter_prefers_cjk_punctuation_when_splitting_by_width(self) -> None:
        text = "我们需要保留每个字幕的含义，但是也要保证每行都清晰。"
        transcript = Transcript(
            language="zh-CN",
            engine="mlx",
            words=tuple(
                Word(token, index * 300, index * 300 + 250, 0.9) for index, token in enumerate(text)
            ),
        )

        cues = segment_transcript(transcript, SegmentationConfig(max_characters=28))

        self.assertEqual(
            [cue.source_text for cue in cues],
            ["我们需要保留每个字幕的含义，", "但是也要保证每行都清晰。"],
        )
        self.assertEqual([(cue.start_ms, cue.end_ms) for cue in cues], [(0, 4200), (4200, 7900)])

    def test_bilingual_qa_requires_review_for_visual_overflow(self) -> None:
        cues = [
            Cue(
                1,
                0,
                5000,
                "Short source",
                target_text="这是一条明显超过单行显示宽度限制的中文字幕",
                source_language="en",
                target_language="zh-CN",
            )
        ]

        report = check_subtitles(cues, require_translation=True)

        self.assertEqual(report.status, "needs_review")
        self.assertIn("bilingual_overflow_risk", {issue.code for issue in report.issues})

    def test_resegment_translation_replaces_source_cues_and_invalidates_downstream_artifacts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            words = [
                Word(("" if index == 0 else " ") + token, index * 500, index * 500 + 400, 0.9)
                for index, token in enumerate(("This", "subtitle", "needs", "splitting"))
            ]
            source_path = artifacts / "source-cues.json"
            source_path.write_text(
                json.dumps([asdict(Cue(1, 0, 2000, "This subtitle needs splitting", words=words))]),
                encoding="utf-8",
            )
            translated_path = artifacts / "translated-cues.json"
            translated_path.write_text(
                json.dumps(
                    [
                        asdict(
                            Cue(
                                1,
                                0,
                                2000,
                                "This subtitle needs splitting",
                                target_text="长" * 19,
                                target_language="zh-CN",
                            )
                        )
                    ]
                ),
                encoding="utf-8",
            )
            media_path = artifacts / "media.json"
            media_path.write_text(
                json.dumps({"duration_ms": 2000, "width": 1920, "height": 1080}),
                encoding="utf-8",
            )
            progress_path = artifacts / "translation-progress.json"
            progress_path.write_text("{}", encoding="utf-8")
            imported_translation_path = artifacts / "input-translation.srt"
            imported_translation_path.write_text("old translation", encoding="utf-8")
            mapping_path = artifacts / "translation-mapping.json"
            mapping_path.write_text("{}", encoding="utf-8")
            job.artifacts.extend(
                [
                    Artifact("source_cues", str(source_path), file_checksum(source_path)),
                    Artifact(
                        "translated_cues", str(translated_path), file_checksum(translated_path)
                    ),
                    Artifact("media_info", str(media_path), file_checksum(media_path)),
                    Artifact(
                        "translation_progress", str(progress_path), file_checksum(progress_path)
                    ),
                    Artifact(
                        "input_translation_subtitles",
                        str(imported_translation_path),
                        file_checksum(imported_translation_path),
                    ),
                    Artifact("translation_mapping", str(mapping_path), file_checksum(mapping_path)),
                ]
            )
            job.stages[StageName.SUBTITLE_QA].status = StageStatus.FAILED
            job_file = save_job(job)
            original_job_data = json.loads(job_file.read_text(encoding="utf-8"))
            original_source = source_path.read_text(encoding="utf-8")

            result = resegment_translation(job_file)

            self.assertTrue(result.changed)
            updated = load_job(job_file)
            updated_cues = json.loads(source_path.read_text(encoding="utf-8"))
            self.assertEqual([cue["id"] for cue in updated_cues], [1, 2])
            self.assertTrue(all(cue["provenance"] == "asr_resegmented" for cue in updated_cues))
            artifact_kinds = {artifact.kind for artifact in updated.artifacts}
            self.assertIn("source_srt", artifact_kinds)
            self.assertNotIn("translated_cues", artifact_kinds)
            self.assertNotIn("translation_progress", artifact_kinds)
            self.assertNotIn("input_translation_subtitles", artifact_kinds)
            self.assertNotIn("translation_mapping", artifact_kinds)
            self.assertIn("translation_resegmentation", artifact_kinds)
            self.assertEqual(updated.stages[StageName.TRANSLATE].status, StageStatus.PENDING)
            self.assertEqual(updated.stages[StageName.SUBTITLE_QA].status, StageStatus.PENDING)

            job_file.write_text(
                json.dumps(original_job_data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            source_path.write_text(original_source, encoding="utf-8")
            with patch("xvt.pipeline.save_subtitles", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    resegment_translation(job_file)

            failed_job = load_job(job_file)
            failed_kinds = {artifact.kind for artifact in failed_job.artifacts}
            self.assertIn("translated_cues", failed_kinds)
            self.assertIn("translation_progress", failed_kinds)
            self.assertEqual(
                failed_job.stages[StageName.SUBTITLE_QA].status,
                StageStatus.FAILED,
            )

    def test_resegment_translation_rejects_imported_source_subtitles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            source_path = artifacts / "source-cues.json"
            source_path.write_text(
                json.dumps(
                    [asdict(Cue(1, 0, 2000, "Imported", provenance="user_source_subtitle"))]
                ),
                encoding="utf-8",
            )
            translated_path = artifacts / "translated-cues.json"
            translated_path.write_text(
                json.dumps(
                    [
                        asdict(
                            Cue(
                                1,
                                0,
                                2000,
                                "Imported",
                                target_text="长" * 19,
                                target_language="zh-CN",
                            )
                        )
                    ]
                ),
                encoding="utf-8",
            )
            media_path = artifacts / "media.json"
            media_path.write_text(
                json.dumps({"duration_ms": 2000, "width": 1920, "height": 1080}),
                encoding="utf-8",
            )
            job.artifacts.extend(
                [
                    Artifact("source_cues", str(source_path), file_checksum(source_path)),
                    Artifact(
                        "translated_cues", str(translated_path), file_checksum(translated_path)
                    ),
                    Artifact("media_info", str(media_path), file_checksum(media_path)),
                ]
            )
            job_file = save_job(job)

            with self.assertRaisesRegex(ValueError, "no ASR word timestamps"):
                resegment_translation(job_file)

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
                patch("xvt.pipeline.probe_media", return_value=media_info),
                patch("xvt.pipeline.run_command", side_effect=run_ffmpeg),
                patch("xvt.pipeline.transcribe_audio", return_value=transcript) as transcribe,
                patch("xvt.pipeline.segment_transcript", wraps=segment_transcript) as segment,
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
            with patch("xvt.pipeline.probe_media", side_effect=ValueError("bad media")):
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
            transcript = Transcript(
                language="en",
                engine="mlx",
                words=(Word("Uncertain.", 0, 1000, 0.1),),
            )
            with (
                patch("xvt.pipeline.probe_media", return_value=media_info),
                patch("xvt.pipeline.run_command", side_effect=run_ffmpeg),
                patch("xvt.pipeline.transcribe_audio", return_value=transcript),
            ):
                with self.assertRaisesRegex(RuntimeError, "subtitle QA needs_review"):
                    run_local_transcription(media, root)

            job_file = next((root / "jobs").glob("*/job.json"))
            data = json.loads(job_file.read_text())
            self.assertEqual(data["stages"]["subtitle_qa"]["status"], "failed")
            qa = json.loads((job_file.parent / "artifacts" / "qa.json").read_text())
            self.assertEqual(qa["status"], "needs_review")

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
                patch("xvt.pipeline.transcribe_audio", return_value=transcript) as transcribe,
                patch("xvt.pipeline.segment_transcript", wraps=segment_transcript) as segment,
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

            with patch("xvt.pipeline.transcribe_audio") as transcribe:
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

            with patch("xvt.pipeline.transcribe_audio", return_value=transcript):
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
                patch("xvt.pipeline.probe_media", return_value=media_info),
                patch("xvt.pipeline.run_command") as run_command_mock,
                patch("xvt.pipeline.transcribe_audio") as transcribe_mock,
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
                patch("xvt.pipeline.probe_media", return_value=media_info),
                patch("xvt.pipeline.run_command", side_effect=run_ffmpeg),
                patch("xvt.pipeline.transcribe_audio", return_value=transcript),
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
                patch("xvt.pipeline.probe_media", return_value=media_info),
                patch("xvt.pipeline.run_command", side_effect=run_ffmpeg),
                patch("xvt.pipeline.transcribe_audio", return_value=transcript),
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
                patch("xvt.pipeline.inspect_remote", return_value=remote_info),
                patch("xvt.pipeline.download_remote_media", side_effect=download),
                patch("xvt.pipeline.probe_media", return_value=media_info),
                patch("xvt.pipeline.run_command", side_effect=run_ffmpeg),
                patch("xvt.pipeline.transcribe_audio", return_value=transcript),
            ):
                job = run_remote_transcription("https://youtu.be/abc", root)

        self.assertEqual(job.stages[StageName.ACQUIRE].status, StageStatus.SUCCEEDED)
        self.assertIn("downloaded_media", {artifact.kind for artifact in job.artifacts})

    def test_remote_pipeline_retries_acquisition_in_the_same_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote_info = RemoteMediaInfo("abc", "Example", 5000, "https://youtu.be/abc", (), ())
            with (
                patch("xvt.pipeline.inspect_remote", return_value=remote_info) as inspect,
                patch(
                    "xvt.pipeline.download_remote_media",
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
                patch("xvt.pipeline.inspect_remote") as retry_inspect,
                patch("xvt.pipeline.download_remote_media", side_effect=download),
                patch("xvt.pipeline.probe_media", return_value=media_info),
                patch("xvt.pipeline.run_command", side_effect=run_ffmpeg),
                patch("xvt.pipeline.transcribe_audio", return_value=transcript),
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
                    "xvt.pipeline.inspect_remote",
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
                patch("xvt.pipeline.inspect_remote", return_value=remote_info),
                patch("xvt.pipeline.check_subtitles", side_effect=RuntimeError("QA unavailable")),
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
                patch("xvt.pipeline.inspect_remote") as inspect,
                patch("xvt.pipeline.download_remote_media") as download,
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
                patch("xvt.pipeline.inspect_remote", return_value=remote_info),
                patch("xvt.pipeline.download_remote_media") as download,
                patch("xvt.pipeline.transcribe_audio") as transcribe,
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
                patch("xvt.pipeline.inspect_remote", return_value=remote_info),
                patch("xvt.pipeline.download_remote_media", side_effect=download) as acquire,
                patch("xvt.pipeline.probe_media", return_value=media_info),
                patch("xvt.pipeline.transcribe_audio") as transcribe,
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
                patch("xvt.pipeline.inspect_remote", return_value=remote_info),
                patch("xvt.pipeline.download_remote_subtitles", side_effect=download_subtitles),
                patch("xvt.pipeline.download_remote_media") as download_media,
                patch("xvt.pipeline.transcribe_audio") as transcribe,
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

    def test_agent_translation_round_trip_exports_all_subtitle_formats(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")

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
                patch("xvt.pipeline.probe_media", return_value=media_info),
                patch("xvt.pipeline.run_command", side_effect=run_ffmpeg),
                patch("xvt.pipeline.transcribe_audio", return_value=transcript),
            ):
                job = run_local_transcription(media, root)

            glossary = root / "glossary.json"
            glossary.write_text(
                json.dumps({"terms": [{"source": "Hello", "target": "你好"}]}),
                encoding="utf-8",
            )
            batch_path = prepare_agent_translation(
                root / "jobs" / job.id / "job.json",
                source_language="en",
                target_language="zh-CN",
                glossary=glossary,
                translation_style="natural",
            )
            batch_data = json.loads(batch_path.read_text())
            item = batch_data["batches"][0]["items"][0]
            self.assertEqual(item["id"], 1)
            self.assertEqual(item["duration_ms"], 950)
            self.assertEqual(item["max_target_characters"], 8)
            self.assertEqual(
                batch_data["policy"],
                {
                    "style": "natural",
                    "terms": [{"source": "Hello", "target": "你好"}],
                },
            )

            response_path = root / "response.json"
            response_path.write_text(
                json.dumps({"items": [{"id": 1, "text": "你好"}]}),
                encoding="utf-8",
            )
            translated, bilingual, bilingual_ass = apply_agent_translation(
                root / "jobs" / job.id / "job.json",
                response_path,
                target_language="zh-CN",
            )

            self.assertTrue(translated.exists())
            self.assertIn("你好", bilingual.read_text())
            self.assertTrue(bilingual_ass.exists())
            persisted = json.loads((root / "jobs" / job.id / "job.json").read_text())
            self.assertEqual(persisted["stages"]["translate"]["status"], "succeeded")
            self.assertEqual(persisted["stages"]["subtitle_qa"]["status"], "succeeded")
            self.assertEqual(persisted["config"]["translation_policy"], batch_data["policy"])
            self.assertIn(
                "bilingual_ass", {artifact["kind"] for artifact in persisted["artifacts"]}
            )

    def test_agent_translation_rejects_incomplete_cue_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            job_directory = root / "jobs" / job.id
            artifacts = job_directory / "artifacts"
            artifacts.mkdir()
            cues_path = artifacts / "source-cues.json"
            cues_path.write_text(
                json.dumps([asdict(Cue(1, 0, 1000, "Hello"))]),
                encoding="utf-8",
            )
            media_path = artifacts / "media.json"
            media_path.write_text(json.dumps({"duration_ms": 2000}), encoding="utf-8")
            job.artifacts.extend(
                [
                    Artifact("source_cues", str(cues_path)),
                    Artifact("media_info", str(media_path)),
                ]
            )
            save_job(job)
            response_path = root / "response.json"
            response_path.write_text(json.dumps({"items": []}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "cover every cue"):
                apply_agent_translation(
                    job_directory / "job.json",
                    response_path,
                    target_language="zh-CN",
                )

    def test_agent_translation_blocks_glossary_violations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job(
                "/tmp/input.mp4",
                root,
                {
                    "translation_policy": {
                        "style": "faithful",
                        "terms": [{"source": "OpenAI", "target": "OpenAI"}],
                    }
                },
            )
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            cues_path = artifacts / "source-cues.json"
            cues_path.write_text(
                json.dumps([asdict(Cue(1, 0, 1000, "OpenAI is useful."))]),
                encoding="utf-8",
            )
            media_path = artifacts / "media.json"
            media_path.write_text(json.dumps({"duration_ms": 2000}), encoding="utf-8")
            job.artifacts.extend(
                [
                    Artifact("source_cues", str(cues_path)),
                    Artifact("media_info", str(media_path)),
                ]
            )
            job_file = save_job(job)
            response_path = root / "response.json"
            response_path.write_text(
                json.dumps({"items": [{"id": 1, "text": "开放人工智能很有用。"}]}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "subtitle QA failed"):
                apply_agent_translation(job_file, response_path, target_language="zh-CN")

            persisted = json.loads(job_file.read_text())
            report = json.loads((artifacts / "qa-translated.json").read_text())

        self.assertEqual(persisted["stages"]["subtitle_qa"]["status"], "failed")
        self.assertEqual(report["issues"][0]["code"], "glossary_violation")
        self.assertNotIn("bilingual_ass", {item["kind"] for item in persisted["artifacts"]})

    def test_cue_retranslation_updates_only_requested_cues_and_invalidates_burn_outputs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            translated_path = artifacts / "translated-cues.json"
            translated_path.write_text(
                json.dumps(
                    [
                        asdict(Cue(1, 0, 1000, "First", "旧译文一", target_language="zh-CN")),
                        asdict(Cue(2, 1000, 2000, "Second", "旧译文二", target_language="zh-CN")),
                    ]
                ),
                encoding="utf-8",
            )
            media_path = artifacts / "media.json"
            media_path.write_text(json.dumps({"duration_ms": 2000}), encoding="utf-8")
            stale_ass = artifacts / "bilingual.ass"
            stale_ass.write_text("stale", encoding="utf-8")
            stale_preview = artifacts / "preview.mp4"
            stale_preview.write_bytes(b"stale preview")
            job.artifacts.extend(
                [
                    Artifact("translated_cues", str(translated_path)),
                    Artifact("media_info", str(media_path)),
                    Artifact("bilingual_ass", str(stale_ass)),
                    Artifact("preview_video", str(stale_preview)),
                ]
            )
            job_file = save_job(job)
            response_path = root / "response.json"
            response_path.write_text(
                json.dumps({"items": [{"id": 1, "text": "新译文一"}]}),
                encoding="utf-8",
            )

            apply_cue_retranslation(
                job_file,
                response_path,
                cue_ids=(1,),
                target_language="zh-CN",
            )

            persisted = json.loads(job_file.read_text(encoding="utf-8"))
            repaired = json.loads(translated_path.read_text(encoding="utf-8"))

        self.assertEqual([cue["target_text"] for cue in repaired], ["新译文一", "旧译文二"])
        self.assertEqual(persisted["stages"]["subtitle_qa"]["status"], "succeeded")
        self.assertNotIn("bilingual_ass", {item["kind"] for item in persisted["artifacts"]})
        self.assertNotIn("preview_video", {item["kind"] for item in persisted["artifacts"]})

    def test_cue_retranslation_rejects_a_different_target_language(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            translated_path = artifacts / "translated-cues.json"
            translated_path.write_text(
                json.dumps([asdict(Cue(1, 0, 1000, "First", "一", target_language="zh-CN"))]),
                encoding="utf-8",
            )
            media_path = artifacts / "media.json"
            media_path.write_text(json.dumps({"duration_ms": 1000}), encoding="utf-8")
            job.artifacts.extend(
                [
                    Artifact("translated_cues", str(translated_path)),
                    Artifact("media_info", str(media_path)),
                ]
            )
            job_file = save_job(job)
            response_path = root / "response.json"
            response_path.write_text(
                json.dumps({"items": [{"id": 1, "text": "one"}]}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "target language must match"):
                apply_cue_retranslation(
                    job_file,
                    response_path,
                    cue_ids=(1,),
                    target_language="en",
                )

    def test_cue_retranslation_agent_writes_selected_cue_with_original_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            source_path = artifacts / "source-cues.json"
            source_path.write_text(
                json.dumps(
                    [
                        asdict(Cue(1, 0, 1000, "First", source_language="en")),
                        asdict(Cue(2, 1000, 2000, "Second", source_language="en")),
                        asdict(Cue(3, 2000, 3000, "Third", source_language="en")),
                    ]
                ),
                encoding="utf-8",
            )
            translated_path = artifacts / "translated-cues.json"
            translated_path.write_text(
                json.dumps(
                    [
                        asdict(Cue(1, 0, 1000, "First", "一", target_language="zh-CN")),
                        asdict(Cue(2, 1000, 2000, "Second", "二", target_language="zh-CN")),
                        asdict(Cue(3, 2000, 3000, "Third", "三", target_language="zh-CN")),
                    ]
                ),
                encoding="utf-8",
            )
            job.artifacts.extend(
                [
                    Artifact("source_cues", str(source_path)),
                    Artifact("translated_cues", str(translated_path)),
                ]
            )
            job_file = save_job(job)

            result = retranslate_cues(
                job_file,
                cue_ids=(2,),
                target_language="zh-CN",
                provider="agent",
            )

            batch_data = json.loads(result.paths[0].read_text(encoding="utf-8"))
            persisted = json.loads(job_file.read_text(encoding="utf-8"))

        batch = batch_data["batches"][0]
        self.assertEqual(result.mode, "agent")
        self.assertEqual([item["id"] for item in batch["items"]], [2])
        self.assertEqual([item["id"] for item in batch["context_before"]], [1])
        self.assertEqual([item["id"] for item in batch["context_after"]], [3])
        self.assertEqual(persisted["stages"]["translate"]["status"], "awaiting_input")

    def test_existing_translation_uses_source_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            source_path = artifacts / "source-cues.json"
            source_path.write_text(
                json.dumps([asdict(Cue(1, 1000, 2000, "Hello"))]),
                encoding="utf-8",
            )
            media_path = artifacts / "media.json"
            media_path.write_text(json.dumps({"duration_ms": 3000}), encoding="utf-8")
            job.artifacts.extend(
                [Artifact("source_cues", str(source_path)), Artifact("media_info", str(media_path))]
            )
            save_job(job)
            translation_path = root / "translation.srt"
            translation_path.write_text(
                "1\n00:00:00,000 --> 00:00:00,500\n你好\n",
                encoding="utf-8",
            )

            translated, bilingual = apply_subtitle_translation(
                artifacts.parent / "job.json",
                translation_path,
                target_language="zh-CN",
            )

            self.assertIn("00:00:01,000 --> 00:00:02,000", translated.read_text())
            self.assertIn("你好", bilingual.read_text())

    def test_existing_translation_rejects_cue_count_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            source_path = artifacts / "source-cues.json"
            source_path.write_text(
                json.dumps([asdict(Cue(1, 0, 1000, "One")), asdict(Cue(2, 1000, 2000, "Two"))]),
                encoding="utf-8",
            )
            job.artifacts.append(Artifact("source_cues", str(source_path)))
            save_job(job)
            translation_path = root / "translation.srt"
            translation_path.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n一个\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "do not cover source cues"):
                apply_subtitle_translation(
                    artifacts.parent / "job.json",
                    translation_path,
                    target_language="zh-CN",
                )

    def test_burn_translated_job_generates_ass_and_validates_media(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            job = create_job("https://youtu.be/abc", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            translated_path = artifacts / "translated-cues.json"
            translated_path.write_text(
                json.dumps(
                    [
                        asdict(
                            Cue(
                                1,
                                0,
                                1000,
                                "Hello",
                                target_text="你好",
                                source_language="en",
                                target_language="zh-CN",
                            )
                        )
                    ]
                ),
                encoding="utf-8",
            )
            job.artifacts.append(Artifact("translated_cues", str(translated_path)))
            job.artifacts.append(Artifact("source_video", str(media), file_checksum(media)))
            save_job(job)
            input_info = MediaInfo(
                str(media), 2000, 5, "mp4", 1920, 1080, 30.0, "h264", "aac", True, True
            )
            output_info = MediaInfo(
                str(artifacts / "burned.mp4"),
                2000,
                10,
                "mp4",
                1920,
                1080,
                30.0,
                "h264",
                "aac",
                True,
                True,
            )

            burn_options = {}

            def burn(_media, _subtitles, output, **kwargs):
                burn_options.update(kwargs)
                Path(output).write_bytes(b"burned")
                return Path(output)

            with (
                patch("xvt.pipeline.probe_media", side_effect=[input_info, output_info]),
                patch("xvt.pipeline.burn_subtitles", side_effect=burn),
                patch("xvt.pipeline.download_remote_media") as download,
            ):
                result = burn_translated_job(root / "jobs" / job.id / "job.json")

            self.assertTrue(result.exists())
            persisted = json.loads((root / "jobs" / job.id / "job.json").read_text())
            self.assertEqual(persisted["stages"]["media_qa"]["status"], "succeeded")
            self.assertTrue((artifacts / "bilingual.ass").exists())
            self.assertEqual(burn_options["video_bitrate"], "5M")
            download.assert_not_called()

    def test_preview_translated_job_renders_selected_cue_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            burn_media = root / "burn-input.mp4"
            burn_media.write_bytes(b"burn video")
            job = create_job(str(media), root)
            job.config["workflow"] = {
                "burn_media": str(burn_media),
                "hardware_encoding": False,
                "target_font": "Target Font",
                "source_font": "Source Font",
            }
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            translated_path = artifacts / "translated-cues.json"
            translated_path.write_text(
                json.dumps(
                    [
                        asdict(
                            Cue(
                                1,
                                1000,
                                2000,
                                "First",
                                target_text="第一条",
                                source_language="en",
                                target_language="zh-CN",
                            )
                        ),
                        asdict(
                            Cue(
                                2,
                                5000,
                                7000,
                                "Second",
                                target_text="第二条",
                                source_language="en",
                                target_language="zh-CN",
                            )
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            qa_path = artifacts / "qa-translated.json"
            qa_path.write_text(json.dumps({"status": "pass"}), encoding="utf-8")
            burned_path = artifacts / "burned.mp4"
            burned_path.write_bytes(b"burned")
            job.artifacts.extend(
                [
                    Artifact(
                        "translated_cues", str(translated_path), file_checksum(translated_path)
                    ),
                    Artifact(
                        "translated_subtitle_qa",
                        str(qa_path),
                        file_checksum(qa_path),
                    ),
                    Artifact("burned_video", str(burned_path), file_checksum(burned_path)),
                ]
            )
            job_file = save_job(job)
            source_info = MediaInfo(
                str(media), 30000, 5, "mp4", 1920, 1080, 30.0, "h264", "aac", True, True
            )
            preview_info = MediaInfo(
                str(artifacts / "preview.mp4"),
                10000,
                10,
                "mp4",
                1920,
                1080,
                30.0,
                "h264",
                "aac",
                True,
                True,
            )
            burn_options = {}
            burned_media = None

            def burn(_media, _subtitles, output, **kwargs):
                nonlocal burned_media
                burned_media = Path(_media)
                burn_options.update(kwargs)
                Path(output).write_bytes(b"preview")
                return Path(output)

            with (
                patch("xvt.pipeline.probe_media", side_effect=[source_info, preview_info]),
                patch("xvt.pipeline.burn_subtitles", side_effect=burn),
            ):
                result = preview_translated_job(
                    job_file,
                    cue_id=2,
                    duration_seconds=10,
                )

            persisted = load_job(job_file)
            preview_ass_text = (artifacts / "preview.ass").read_text(encoding="utf-8")
            with (
                patch("xvt.pipeline.probe_media", return_value=source_info),
                self.assertRaisesRegex(ValueError, "existing artifact"),
            ):
                preview_translated_job(job_file, output=burned_path)
            media_alias = root / "media-alias.mp4"
            os.link(burn_media, media_alias)
            with (
                patch("xvt.pipeline.probe_media", return_value=source_info),
                self.assertRaisesRegex(ValueError, "source media"),
            ):
                preview_translated_job(job_file, output=media_alias)

        self.assertEqual(result.name, "preview.mp4")
        self.assertEqual(burned_media, burn_media.resolve())
        self.assertEqual(burn_options["start_ms"], 3000)
        self.assertEqual(burn_options["clip_duration_ms"], 10000)
        self.assertEqual(burn_options["duration_ms"], 10000)
        self.assertEqual(burn_options["progress_event_type"], "preview_progress")
        self.assertFalse(burn_options["hardware_encoding"])
        self.assertIn("Target Font", preview_ass_text)
        self.assertIn("Source Font", preview_ass_text)
        self.assertEqual(
            {artifact.kind for artifact in persisted.artifacts} & {"preview_ass", "preview_video"},
            {"preview_ass", "preview_video"},
        )

    def test_preview_rejects_failed_translated_subtitle_qa(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            translated_path = artifacts / "translated-cues.json"
            translated_path.write_text(
                json.dumps([asdict(Cue(1, 0, 1000, "Hello", "你好"))]),
                encoding="utf-8",
            )
            qa_path = artifacts / "qa-translated.json"
            qa_path.write_text(json.dumps({"status": "failed"}), encoding="utf-8")
            job.artifacts.extend(
                [
                    Artifact(
                        "translated_cues", str(translated_path), file_checksum(translated_path)
                    ),
                    Artifact(
                        "translated_subtitle_qa",
                        str(qa_path),
                        file_checksum(qa_path),
                    ),
                ]
            )
            job_file = save_job(job)

            with self.assertRaisesRegex(RuntimeError, "QA must pass"):
                preview_translated_job(job_file)

    def test_preview_rejects_non_finite_time_values_before_loading_job(self) -> None:
        with self.assertRaisesRegex(ValueError, "--start"):
            preview_translated_job("/tmp/missing-job.json", start_seconds=float("nan"))
        with self.assertRaisesRegex(ValueError, "--duration"):
            preview_translated_job("/tmp/missing-job.json", duration_seconds=float("inf"))

    def test_burn_media_qa_rejects_missing_audio_and_changed_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            job = create_job(str(media), root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            translated_path = artifacts / "translated-cues.json"
            translated_path.write_text(
                json.dumps(
                    [
                        asdict(
                            Cue(
                                1,
                                0,
                                1000,
                                "Hello",
                                target_text="你好",
                                source_language="en",
                                target_language="zh-CN",
                            )
                        )
                    ]
                ),
                encoding="utf-8",
            )
            job.artifacts.append(Artifact("translated_cues", str(translated_path)))
            save_job(job)
            input_info = MediaInfo(
                str(media), 2000, 5, "mp4", 1920, 1080, 30.0, "h264", "aac", True, True
            )
            output_info = MediaInfo(
                str(artifacts / "burned.mp4"),
                2000,
                10,
                "mp4",
                1280,
                720,
                30.0,
                "h264",
                None,
                True,
                False,
            )

            def burn(_media, _subtitles, output, **_kwargs):
                Path(output).write_bytes(b"burned")
                return Path(output)

            with (
                patch("xvt.pipeline.probe_media", side_effect=[input_info, output_info]),
                patch("xvt.pipeline.burn_subtitles", side_effect=burn),
                self.assertRaisesRegex(RuntimeError, "failed media QA"),
            ):
                burn_translated_job(root / "jobs" / job.id / "job.json")

            report = json.loads((artifacts / "media-qa.json").read_text(encoding="utf-8"))

        self.assertEqual(report["status"], "failed")
        self.assertIn("output has no audio stream", report["issues"])
        self.assertIn("output dimensions differ from source video", report["issues"])


if __name__ == "__main__":
    unittest.main()

import json
import os
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

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
    retranslate_cues,
    run_local_transcription,
    run_subtitle_transcription,
    run_translation,
)
from xvt.process import CommandResult
from xvt.qa import check_subtitles, display_width, display_width_limit
from xvt.recovery import file_checksum
from xvt.segment import SegmentationConfig, segment_transcript
from xvt.translation_provider import ProviderResponseError


class PipelineTest(unittest.TestCase):
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

    def test_resegment_translation_invalidates_cue_retranslation_artifacts(self) -> None:
        # P7e: cue_retranslation_batches/cue_retranslation_response 引用重分段前的
        # 废弃 cue id，重分段后必须失效，不能以 checksum 有效的姿态残留在
        # job.artifacts 中污染 status/verify/恢复计划的可复用产物清单。
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
            cue_retranslation_batches_path = artifacts / "cue-retranslation-batches.json"
            cue_retranslation_batches_path.write_text("{}", encoding="utf-8")
            subtitle_qa_path = artifacts / "qa.json"
            subtitle_qa_path.write_text("{}", encoding="utf-8")
            cue_retranslation_response_path = artifacts / "cue-retranslation-response.json"
            cue_retranslation_response_path.write_text("{}", encoding="utf-8")
            job.artifacts.extend(
                [
                    Artifact("source_cues", str(source_path), file_checksum(source_path)),
                    Artifact(
                        "translated_cues", str(translated_path), file_checksum(translated_path)
                    ),
                    Artifact("media_info", str(media_path), file_checksum(media_path)),
                    Artifact("subtitle_qa", str(subtitle_qa_path), file_checksum(subtitle_qa_path)),
                    Artifact(
                        "cue_retranslation_batches",
                        str(cue_retranslation_batches_path),
                        file_checksum(cue_retranslation_batches_path),
                    ),
                    Artifact(
                        "cue_retranslation_response",
                        str(cue_retranslation_response_path),
                        file_checksum(cue_retranslation_response_path),
                    ),
                ]
            )
            job_file = save_job(job)

            result = resegment_translation(job_file)

            self.assertTrue(result.changed)
            updated = load_job(job_file)
            artifact_kinds = {artifact.kind for artifact in updated.artifacts}
            self.assertNotIn("cue_retranslation_batches", artifact_kinds)
            self.assertNotIn("cue_retranslation_response", artifact_kinds)
            # P7f：旧 subtitle_qa 引用重分段前的 cue，不得以有效产物姿态残留。
            self.assertNotIn("subtitle_qa", artifact_kinds)

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
                patch("xvt.pipeline.transcription.probe_media", return_value=media_info),
                patch("xvt.pipeline.transcription.run_command", side_effect=run_ffmpeg),
                patch("xvt.pipeline.transcription.transcribe_audio", return_value=transcript),
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

    def _make_review_blocked_subtitle_workflow(self, root: Path) -> Path:
        """构造一个因源字幕折叠后超宽而被 needs_review 阻断的端到端字幕 Job。"""
        from xvt.workflow import WorkflowOptions, run_workflow

        source = root / "source.srt"
        wide_line = "word " * 16  # 折叠后约 80 显示单位，超过 72 触发 review
        source.write_text(
            f"1\n00:00:00,000 --> 00:00:20,000\n{wide_line.strip()}\n",
            encoding="utf-8",
        )
        translated = root / "translated.srt"
        translated.write_text(
            "1\n00:00:00,000 --> 00:00:20,000\n你好。\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RuntimeError, "needs_review"):
            run_workflow(
                WorkflowOptions(
                    input=str(source),
                    output=root / "output",
                    language="en",
                    target_language="zh-CN",
                    translation_subtitles=translated,
                )
            )
        job_file = next((root / "output" / "jobs").glob("*/job.json"))
        return job_file

    def test_continue_without_acknowledgment_stays_blocked(self) -> None:
        from xvt.workflow import continue_workflow

        with tempfile.TemporaryDirectory() as directory:
            job_file = self._make_review_blocked_subtitle_workflow(Path(directory))
            with self.assertRaisesRegex(RuntimeError, "needs_review"):
                continue_workflow(job_file)

    def test_acknowledged_continue_completes_both_qa_gates(self) -> None:
        from xvt.pipeline import acknowledge_subtitle_review
        from xvt.workflow import continue_workflow

        events = []
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._make_review_blocked_subtitle_workflow(Path(directory))
            acknowledge_subtitle_review(job_file, event_sink=events.append)
            with patch("xvt.workflow.resume_remote_transcription") as remote:
                result = continue_workflow(job_file, event_sink=events.append)

            self.assertEqual(result.status, "completed")
            remote.assert_not_called()
            updated = load_job(job_file)
            kinds = {artifact.kind for artifact in updated.artifacts}
            self.assertIn("source_srt", kinds)
            self.assertIn("bilingual_srt", kinds)

        acknowledged = [event for event in events if event.type == "subtitle_review_acknowledged"]
        # 一次显式确认 + 两道 QA 门（源字幕 QA 与译文 QA）各一次复用放行。
        self.assertEqual(acknowledged[0].data["source"], "user_flag")
        self.assertGreaterEqual(len(acknowledged), 3)
        self.assertTrue(
            all(event.data["source"] == "stored_acknowledgment" for event in acknowledged[1:])
        )

    def test_acknowledge_rejects_error_level_reports(self) -> None:
        from xvt.errors import UserInputError
        from xvt.pipeline import acknowledge_subtitle_review

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.srt", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            qa_path = artifacts / "qa.json"
            qa_path.write_text(
                json.dumps(
                    {
                        "status": "needs_review",
                        "cue_count": 1,
                        "issues": [
                            {"code": "line_too_wide", "severity": "review", "cue_id": 1},
                            {"code": "cue_overlap", "severity": "error", "cue_id": 2},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            job.artifacts.append(Artifact("subtitle_qa", str(qa_path), file_checksum(qa_path)))
            job_file = save_job(job)

            with self.assertRaisesRegex(UserInputError, "cannot be acknowledged"):
                acknowledge_subtitle_review(job_file)

    def test_acknowledgment_invalidated_when_subtitles_change(self) -> None:
        from xvt.pipeline.review import _acknowledged_review_issues

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.srt", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            cues_path = artifacts / "source-cues.json"
            cues_path.write_text("[]", encoding="utf-8")
            job.artifacts.append(Artifact("source_cues", str(cues_path), file_checksum(cues_path)))
            job.config["subtitle_review_acknowledgment"] = {
                "source_cues_checksum": file_checksum(cues_path),
                "acknowledged_issues": [["line_too_wide", 1]],
            }
            self.assertEqual(_acknowledged_review_issues(job), {("display_width", 1)})

            # 字幕内容变化（checksum 改变）后确认失效。
            cues_path.write_text("[{}]", encoding="utf-8")
            job.artifacts[-1] = Artifact("source_cues", str(cues_path), file_checksum(cues_path))
            self.assertIsNone(_acknowledged_review_issues(job))

    def test_new_review_issue_blocks_despite_acknowledgment(self) -> None:
        from xvt.pipeline.review import _complete_qa_stage
        from xvt.qa import SubtitleIssue, SubtitleQaReport

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.srt", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            cues_path = artifacts / "source-cues.json"
            cues_path.write_text("[]", encoding="utf-8")
            job.artifacts.append(Artifact("source_cues", str(cues_path), file_checksum(cues_path)))
            job.config["subtitle_review_acknowledgment"] = {
                "source_cues_checksum": file_checksum(cues_path),
                "acknowledged_issues": [["line_too_wide", 1]],
            }
            save_job(job)
            report = SubtitleQaReport(
                status="needs_review",
                cue_count=2,
                issues=(
                    SubtitleIssue("line_too_wide", "review", 1, "acknowledged"),
                    SubtitleIssue("line_too_wide", "review", 2, "new problem"),
                ),
            )
            with self.assertRaisesRegex(RuntimeError, "needs_review"):
                _complete_qa_stage(job, report, None)

    def test_malformed_provider_response_enters_correction_retry(self) -> None:
        # 回归测试：结构性无效的 provider 响应（ProviderResponseError）必须进入
        # 纠错重试分支，第二次请求携带纠正提示词后成功。
        provider_env = {
            "XVT_TRANSLATION_BASE_URL": "https://provider.example.com/v1",
            "XVT_TRANSLATION_MODEL": "test-model",
            "XVT_TRANSLATION_API_KEY": "secret",
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,500\nHello world.\n",
                encoding="utf-8",
            )
            job = run_subtitle_transcription(source, directory, language="en")

            calls = []

            def flaky_provider(batch, config, *, correction=None, **_kwargs):
                calls.append(correction)
                if len(calls) == 1:
                    raise ProviderResponseError(
                        "translation provider response must contain an items array"
                    )
                return {item.id: "你好，世界。" for item in batch.items}

            events = []
            with (
                patch.dict(os.environ, provider_env, clear=False),
                patch("xvt.pipeline.translate_batch", side_effect=flaky_provider),
            ):
                result = run_translation(
                    get_job_file(job),
                    target_language="zh-CN",
                    provider="openai-compatible",
                    max_retries=2,
                    event_sink=events.append,
                )

        self.assertEqual(result.mode, "openai-compatible")
        self.assertEqual(len(calls), 2)
        self.assertIsNone(calls[0])
        self.assertIn("previous response was invalid", calls[1] or "")

        batch_events = [
            event
            for event in events
            if event.type
            in {
                "translation_batch_started",
                "translation_batch_retrying",
                "translation_batch_completed",
            }
        ]
        self.assertEqual(
            {event.type for event in batch_events},
            {
                "translation_batch_started",
                "translation_batch_retrying",
                "translation_batch_completed",
            },
        )
        for event in batch_events:
            self.assertEqual(event.data["batch_total"], 1)

    def test_malformed_provider_response_exhausts_retries_as_dependency_error(self) -> None:
        provider_env = {
            "XVT_TRANSLATION_BASE_URL": "https://provider.example.com/v1",
            "XVT_TRANSLATION_MODEL": "test-model",
            "XVT_TRANSLATION_API_KEY": "secret",
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,500\nHello world.\n",
                encoding="utf-8",
            )
            job = run_subtitle_transcription(source, directory, language="en")

            with (
                patch.dict(os.environ, provider_env, clear=False),
                patch(
                    "xvt.pipeline.translate_batch",
                    side_effect=ProviderResponseError("always malformed"),
                ),
                self.assertRaises(ProviderResponseError) as raised,
            ):
                run_translation(
                    get_job_file(job),
                    target_language="zh-CN",
                    provider="openai-compatible",
                    max_retries=1,
                )

        self.assertEqual(raised.exception.category, "dependency_or_process")
        self.assertEqual(int(raised.exception.exit_code), 3)


if __name__ == "__main__":
    unittest.main()

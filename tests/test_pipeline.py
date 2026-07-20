import json
import os
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from xvt.job_store import create_job, load_job, save_job
from xvt.media import MediaInfo
from xvt.models import Artifact, Cue, Transcript, Word
from xvt.pipeline import (
    burn_translated_job,
    preview_translated_job,
)
from xvt.qa import check_subtitles, display_width, display_width_limit
from xvt.recovery import file_checksum
from xvt.segment import SegmentationConfig, segment_transcript


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


if __name__ == "__main__":
    unittest.main()

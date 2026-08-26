import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xvt.job_store import create_job, load_job, save_job
from xvt.models import Artifact, Cue, StageName, StageStatus, Transcript, Word
from xvt.qa import check_subtitles, display_width, display_width_limit
from xvt.recovery import file_checksum, valid_artifact_path
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
        result = run_workflow(
            WorkflowOptions(
                input=str(source),
                output=root / "output",
                language="en",
                target_language="zh-CN",
                translation_subtitles=translated,
            )
        )
        self.assertEqual(result.status, "awaiting_input")
        job_file = next((root / "output" / "jobs").glob("*/job.json"))
        return job_file

    def test_continue_without_acknowledgment_stays_blocked(self) -> None:
        from xvt.workflow import continue_workflow

        with tempfile.TemporaryDirectory() as directory:
            job_file = self._make_review_blocked_subtitle_workflow(Path(directory))
            result = continue_workflow(job_file)

        self.assertEqual(result.status, "awaiting_input")

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
            self.assertEqual(updated.stages[StageName.TRANSLATE].status, StageStatus.SUCCEEDED)

        acknowledged = [event for event in events if event.type == "subtitle_review_acknowledged"]
        # 一次显式确认 + 两道 QA 门（源字幕 QA 与译文 QA）各一次复用放行。
        self.assertEqual(acknowledged[0].data["source"], "user_flag")
        self.assertGreaterEqual(len(acknowledged), 3)
        self.assertTrue(
            all(event.data["source"] == "stored_acknowledgment" for event in acknowledged[1:])
        )

    def test_acknowledged_continue_finalizes_stale_translate_failure(self) -> None:
        """字幕 QA needs_review 确认后，continue 必须清掉 translate 上的过期阻断错误。"""
        from xvt.pipeline import acknowledge_subtitle_review
        from xvt.pipeline.common import SUBTITLE_REVIEW_REQUIRED_MESSAGE
        from xvt.workflow import WorkflowOptions, continue_workflow, run_workflow

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:20,000\nShort source\n",
                encoding="utf-8",
            )
            translated = root / "translated.srt"
            translated.write_text(
                f"1\n00:00:00,000 --> 00:00:20,000\n{'超长字幕' * 20}\n",
                encoding="utf-8",
            )
            waiting = run_workflow(
                WorkflowOptions(
                    input=str(source),
                    output=root / "output",
                    language="en",
                    target_language="zh-CN",
                    translation_subtitles=translated,
                )
            )
            self.assertEqual(waiting.status, "awaiting_input")
            job_file = next((root / "output" / "jobs").glob("*/job.json"))
            acknowledge_subtitle_review(job_file)

            # 语义复核腿在译文 QA 阻断时会把 translate 记为 failed；确认后这条错误已过期。
            job = load_job(job_file)
            job.stages[StageName.TRANSLATE].status = StageStatus.FAILED
            job.stages[StageName.TRANSLATE].error = SUBTITLE_REVIEW_REQUIRED_MESSAGE
            job_file = save_job(job)

            result = continue_workflow(job_file)

            self.assertEqual(result.status, "completed")
            updated = load_job(job_file)
            self.assertEqual(updated.stages[StageName.TRANSLATE].status, StageStatus.SUCCEEDED)
            self.assertIsNone(updated.stages[StageName.TRANSLATE].error)
            self.assertIsNotNone(valid_artifact_path(updated, "bilingual_srt"))

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
        from xvt.pipeline.review import acknowledged_review_issues

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
            self.assertEqual(acknowledged_review_issues(job), {("display_width", 1)})

            # 字幕内容变化（checksum 改变）后确认失效。
            cues_path.write_text("[{}]", encoding="utf-8")
            job.artifacts[-1] = Artifact("source_cues", str(cues_path), file_checksum(cues_path))
            self.assertIsNone(acknowledged_review_issues(job))

    def test_acknowledgment_rejects_boolean_cue_ids(self) -> None:
        # bool 是 int 的子类：`true == 1`，放行会让伪造的确认记录匹配 cue 1 的真实
        # review 问题。与 review_gate_satisfied 的 bool 排除保持对称。
        from xvt.pipeline.review import acknowledged_review_issues

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
                "acknowledged_issues": [["line_too_wide", True]],
            }
            self.assertIsNone(acknowledged_review_issues(job))

    def test_new_review_issue_blocks_despite_acknowledgment(self) -> None:
        from xvt.pipeline.review import complete_qa_stage
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
            job_file = save_job(job)
            report = SubtitleQaReport(
                status="needs_review",
                cue_count=2,
                issues=(
                    SubtitleIssue("line_too_wide", "review", 1, "acknowledged"),
                    SubtitleIssue("line_too_wide", "review", 2, "new problem"),
                ),
            )
            self.assertFalse(complete_qa_stage(job, report, None))
            self.assertEqual(
                load_job(job_file).stages[StageName.SUBTITLE_QA].status,
                StageStatus.AWAITING_INPUT,
            )


if __name__ == "__main__":
    unittest.main()

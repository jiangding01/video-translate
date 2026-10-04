import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import test_pipeline_semantic as semantic_tests

from xvt.cli import main
from xvt.errors import UserInputError
from xvt.job_store import load_job
from xvt.media import MediaInfo
from xvt.models import Cue
from xvt.pipeline import (
    apply_translation_review_fixes,
    preflight_translation_review_fixes,
    run_translation_semantic_review,
    translation_review_input_path,
)
from xvt.pipeline.burnin import _check_render_layout
from xvt.pipeline.review import review_gate_satisfied
from xvt.qa import SubtitleIssue, SubtitleQaReport, check_subtitles
from xvt.recovery import file_checksum
from xvt.render import AssStyleOptions


class RevisionPreflightTest(unittest.TestCase):
    def _response(self, root: Path, text: str):
        job_file = semantic_tests.SemanticReviewPipelineTest()._overlong_candidate_job(root)
        with (
            patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
            patch("xvt.pipeline.semantic.semantic_review_batch", return_value=()),
        ):
            run_translation_semantic_review(job_file)
        response = translation_review_input_path(load_job(job_file))
        assert response is not None
        payload = json.loads(response.read_text())
        payload["items"][0]["text"] = text
        response.write_text(json.dumps(payload), encoding="utf-8")
        return job_file, response

    def _snapshot(self, root: Path):
        return {str(p.relative_to(root)): file_checksum(p) for p in root.rglob("*") if p.is_file()}

    def test_valid_preflight_is_read_only_and_apply_preserves_timeline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, response = self._response(root, "你好。")
            before = self._snapshot(root)
            result = preflight_translation_review_fixes(job_file, response)
            self.assertTrue(result["valid"])
            self.assertEqual(result["changed_ids"], [1])
            self.assertEqual(before, self._snapshot(root))

            apply_translation_review_fixes(job_file, response)
            job = load_job(job_file)
            path = Path(
                next(a.path for a in job.artifacts if a.kind == "translation_reviewed_cues")
            )
            cues = json.loads(path.read_text())
            self.assertEqual(
                [(c["id"], c["start_ms"], c["end_ms"]) for c in cues],
                [(1, 0, 1000), (2, 1000, 2000)],
            )
            self.assertEqual(cues[0]["target_text"], "你好。")

    def test_atomic_render_review_guidance_does_not_recommend_continue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, _ = self._response(root, "你好。")
            job = load_job(job_file)
            job.config.pop("workflow")
            report = SubtitleQaReport(
                "needs_review",
                1,
                (SubtitleIssue("bilingual_overflow_risk", "review", 1, "target line width"),),
            )
            info = MediaInfo("video.mp4", 2000, 1, "mp4", 640, 360, 30, "h264", "aac", True, True)
            with patch("xvt.pipeline.burnin.check_subtitles", return_value=report):
                with self.assertRaisesRegex(UserInputError, "no persisted workflow goal") as error:
                    _check_render_layout(job, [], info, AssStyleOptions(), None)
            self.assertNotIn("run xvt continue", str(error.exception))

    def test_blocked_preflight_and_formal_apply_leave_all_files_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, response = self._response(root, "仍然超长" * 30)
            before = self._snapshot(root)
            result = preflight_translation_review_fixes(job_file, response)
            self.assertFalse(result["valid"])
            self.assertEqual(result["blockers"][0]["cue_id"], 1)
            with self.assertRaisesRegex(UserInputError, "no changes saved"):
                apply_translation_review_fixes(job_file, response)
            self.assertEqual(before, self._snapshot(root))

    def test_cli_check_jsonl_no_locks_writes_or_provider_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, response = self._response(root, "你好。")
            before = self._snapshot(root)
            output = io.StringIO()
            with (
                contextlib.redirect_stdout(output),
                patch("xvt.cli.apply_translation_review_fixes") as apply,
                patch("xvt.pipeline.semantic.semantic_review_batch") as provider,
            ):
                code = main(["apply", str(job_file), str(response), "--check", "--events-jsonl"])
            self.assertEqual(code, 0)
            events = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(events[-1]["type"], "cli_completed")
            self.assertEqual(before, self._snapshot(root))
            apply.assert_not_called()
            provider.assert_not_called()

    def test_cli_invalid_check_and_continue_combination_does_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, response = self._response(root, "仍然超长" * 30)
            before = self._snapshot(root)
            for flags in (("--check",), ("--check", "--continue")):
                with (
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    code = main(["apply", str(job_file), str(response), *flags])
                self.assertEqual(code, 2)
                self.assertEqual(before, self._snapshot(root))

    def test_changed_layout_invalidates_old_review_acknowledgment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, _ = self._response(root, "你好。")
            job = load_job(job_file)
            source = next(a for a in job.artifacts if a.kind == "source_cues")
            job.config["subtitle_review_acknowledgment"] = {
                "source_cues_checksum": source.checksum,
                "acknowledged_issues": [["bilingual_overflow_risk", 1]],
                "layout": {"video_width": 640, "target_font": "A"},
            }
            report = {
                "status": "needs_review",
                "issues": [
                    {
                        "code": "bilingual_overflow_risk",
                        "severity": "review",
                        "cue_id": 1,
                    }
                ],
                "layout": {"video_width": 640, "target_font": "A"},
            }
            self.assertTrue(review_gate_satisfied(job, report))
            report["layout"] = {"video_width": 360, "target_font": "B"}
            self.assertFalse(review_gate_satisfied(job, report))

    def test_preview_accepts_current_acknowledgment_without_writing_formal_qa(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, _ = self._response(root, "你好。")
            job = load_job(job_file)
            source = next(a for a in job.artifacts if a.kind == "source_cues")
            layout = {"video_width": 640, "target_font": "A"}
            job.config["subtitle_review_acknowledgment"] = {
                "source_cues_checksum": source.checksum,
                "acknowledged_issues": [["bilingual_overflow_risk", 1]],
                "layout": layout,
            }
            report = SubtitleQaReport(
                "needs_review",
                1,
                (SubtitleIssue("bilingual_overflow_risk", "review", 1, "target line width"),),
                layout=layout,
            )
            info = MediaInfo("video.mp4", 2000, 1, "mp4", 640, 360, 30, "h264", "aac", True, True)
            before = self._snapshot(root)
            with patch("xvt.pipeline.burnin.check_subtitles", return_value=report):
                _check_render_layout(
                    job,
                    [Cue(1, 0, 1000, "Hello", "你好")],
                    info,
                    AssStyleOptions(target_font="A"),
                    None,
                    persist=False,
                )
            self.assertEqual(before, self._snapshot(root))

    def test_failed_measurement_still_binds_acknowledgment_to_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, _ = self._response(root, "你好。")
            job = load_job(job_file)
            cue = Cue(1, 0, 4000, "Hi", "字幕" * 30, target_language="zh-CN")
            with patch("xvt.qa.measure_bilingual_line_widths", side_effect=ValueError("missing")):
                first = check_subtitles(
                    [cue],
                    require_translation=True,
                    measure_layout=True,
                    video_width=640,
                    video_height=360,
                    render_options=AssStyleOptions(target_font="A"),
                )
                second = check_subtitles(
                    [cue],
                    require_translation=True,
                    measure_layout=True,
                    video_width=360,
                    video_height=640,
                    render_options=AssStyleOptions(target_font="B"),
                )
            self.assertEqual(first.status, "needs_review")
            self.assertEqual(second.status, "needs_review")
            source = next(a for a in job.artifacts if a.kind == "source_cues")
            job.config["subtitle_review_acknowledgment"] = {
                "source_cues_checksum": source.checksum,
                "acknowledged_issues": [["bilingual_overflow_risk", 1]],
                "layout": first.layout,
            }
            self.assertTrue(review_gate_satisfied(job, json.loads(json.dumps(asdict(first)))))
            self.assertFalse(review_gate_satisfied(job, json.loads(json.dumps(asdict(second)))))

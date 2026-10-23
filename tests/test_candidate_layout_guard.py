import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from xvt.job_store import create_job, get_job_directory
from xvt.models import Artifact, Cue
from xvt.pipeline.semantic import _ensure_candidate_qa
from xvt.process import CommandResult
from xvt.recovery import file_checksum


class CandidateLayoutGuardTest(unittest.TestCase):
    def test_candidate_that_fits_is_not_sent_back_for_shortening(self):
        with tempfile.TemporaryDirectory() as directory:
            job = create_job("/tmp/video.mp4", directory)
            artifacts = get_job_directory(job) / "artifacts"
            artifacts.mkdir()
            media = artifacts / "media.json"
            media.write_text(json.dumps({"width": 640, "height": 360, "duration_ms": 4000}))
            job.artifacts.append(Artifact("media_info", str(media), file_checksum(media)))
            candidate = artifacts / "candidate.json"
            cue = Cue(
                1,
                0,
                4000,
                "Hello",
                "或用CLAUDE_CODE_SUBAGENT_MODEL环境变量",
                source_language="en",
                target_language="zh-CN",
            )
            candidate.write_text(json.dumps([asdict(cue)]))
            with patch(
                "xvt.qa.measure_bilingual_line_widths",
                create=True,
                return_value={(1, "target"): (450, 492), (1, "source"): (27, 492)},
            ):
                report_path, blockers = _ensure_candidate_qa(job, candidate, None)
            self.assertEqual(blockers, ())
            self.assertEqual(json.loads(Path(report_path).read_text())["status"], "warning")

    def test_missing_glyph_does_not_become_a_candidate_blocker(self):
        """缺字写进候选 QA 报告，但**不得**成为阻断项。

        阻断项会把语义收尾停在 `awaiting_input` 并要求人工修订，而人工修订模板只收
        缺译文 / 术语违规 / 译文行宽三类——字体 issue 进不去模板，用户会被要求修订
        却看不到要改什么。这条死锁今天由 `candidate_qa` 的过滤器挡住，这里把它钉死。
        """

        with tempfile.TemporaryDirectory() as directory:
            job = create_job("/tmp/video.mp4", directory)
            artifacts = get_job_directory(job) / "artifacts"
            artifacts.mkdir()
            media = artifacts / "media.json"
            media.write_text(json.dumps({"width": 640, "height": 360, "duration_ms": 4000}))
            job.artifacts.append(Artifact("media_info", str(media), file_checksum(media)))
            candidate = artifacts / "candidate.json"
            cue = Cue(
                1,
                0,
                4000,
                "Hello",
                "字幕\U0001e900",
                source_language="en",
                target_language="zh-CN",
            )
            candidate.write_text(json.dumps([asdict(cue)]))
            stderr = (
                "fontselect: failed to find any fallback with glyph 0x1E900 "
                "for font: (Hiragino Sans GB, 700, 0)"
            )
            with patch("xvt.render.run_command", return_value=CommandResult((), 0, "", stderr)):
                report_path, blockers = _ensure_candidate_qa(job, candidate, None)
            report = json.loads(Path(report_path).read_text())
            self.assertEqual(report["status"], "needs_review")
            self.assertIn("font_missing_glyph", {issue["code"] for issue in report["issues"]})
            self.assertEqual(blockers, ())

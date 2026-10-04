import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from xvt.job_store import create_job, get_job_directory
from xvt.models import Artifact, Cue
from xvt.pipeline.semantic import _ensure_candidate_qa
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

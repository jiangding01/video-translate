import json
import tempfile
import unittest
from pathlib import Path

from xvt.job_store import create_job, get_job_file, load_job, normalize_output_root, save_job
from xvt.models import StageName, StageStatus


class JobStoreTest(unittest.TestCase):
    def test_create_job_persists_pending_stages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job("https://example.com/video", directory, {"target": "zh-CN"})
            job_file = get_job_file(job)

            self.assertTrue(job_file.exists())
            self.assertEqual(job.config, {"target": "zh-CN"})
            self.assertEqual(
                job.stages[StageName.INSPECT].status,
                StageStatus.PENDING,
            )

    def test_save_and_load_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job("/tmp/video.mp4", directory)
            job.stages[StageName.INSPECT].status = StageStatus.SUCCEEDED
            save_job(job)

            loaded = load_job(get_job_file(job))

            self.assertEqual(loaded, job)
            self.assertEqual(loaded.stages[StageName.INSPECT].status, StageStatus.SUCCEEDED)
            self.assertFalse((get_job_file(job).parent / ".job.json.tmp").exists())

    def test_relative_output_root_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "absolute path"):
            normalize_output_root("relative/output")

    def test_unknown_schema_version_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "job.json"
            path.write_text(json.dumps({"schema_version": 999}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "schema version"):
                load_job(path)

    def test_malformed_current_schema_is_rejected_as_invalid_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "job.json"
            path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "Invalid job file: missing fields"):
                load_job(path)

    def test_invalid_artifact_types_are_rejected_as_invalid_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job("/tmp/video.mp4", directory)
            job_file = get_job_file(job)
            data = job.to_dict()
            data["artifacts"] = [{"kind": "source_srt", "path": [], "checksum": None}]
            job_file.write_text(json.dumps(data), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "artifact kind and path"):
                load_job(job_file)


if __name__ == "__main__":
    unittest.main()

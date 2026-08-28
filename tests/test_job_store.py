import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xvt.errors import ExitCode, UserInputError
from xvt.job_store import create_job, get_job_file, load_job, normalize_output_root, save_job
from xvt.models import Job, StageName, StageStatus


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

    def test_artifact_unknown_fields_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job("/tmp/video.mp4", directory)
            job_file = get_job_file(job)
            data = job.to_dict()
            data["artifacts"] = [
                {
                    "kind": "source_srt",
                    "path": "artifacts/source.srt",
                    "checksum": "abc123",
                    "future_field": "unknown-to-this-version",
                }
            ]
            job_file.write_text(json.dumps(data), encoding="utf-8")

            loaded = load_job(job_file)

            self.assertEqual(len(loaded.artifacts), 1)
            artifact = loaded.artifacts[0]
            self.assertEqual(artifact.kind, "source_srt")
            self.assertEqual(artifact.path, "artifacts/source.srt")
            self.assertEqual(artifact.checksum, "abc123")
            self.assertFalse(hasattr(artifact, "future_field"))

    def test_stage_record_unknown_fields_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job("/tmp/video.mp4", directory)
            job_file = get_job_file(job)
            data = job.to_dict()
            data["stages"][StageName.INSPECT.value]["future_field"] = "unknown-to-this-version"
            data["stages"][StageName.INSPECT.value]["status"] = StageStatus.SUCCEEDED.value
            job_file.write_text(json.dumps(data), encoding="utf-8")

            loaded = load_job(job_file)

            stage = loaded.stages[StageName.INSPECT]
            self.assertEqual(stage.status, StageStatus.SUCCEEDED)
            self.assertFalse(hasattr(stage, "future_field"))

    def test_registered_migration_upgrades_older_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job("/tmp/video.mp4", directory)
            data = job.to_dict()
            data["schema_version"] = 0
            del data["output_root"]

            def _migrate_v0_to_v1(old: dict) -> dict:
                migrated = dict(old)
                migrated["output_root"] = job.output_root
                migrated["schema_version"] = 1
                return migrated

            with patch.dict("xvt.models._MIGRATIONS", {0: _migrate_v0_to_v1}):
                loaded = Job.from_dict(data)

            self.assertEqual(loaded.schema_version, 1)
            self.assertEqual(loaded.output_root, job.output_root)

    def test_unknown_schema_version_without_migration_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "job.json"
            path.write_text(json.dumps({"schema_version": 999}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "^Unsupported job schema version"):
                load_job(path)

    def test_missing_job_file_is_a_user_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "does-not-exist.json"

            with self.assertRaisesRegex(UserInputError, "job file does not exist") as caught:
                load_job(missing)

            self.assertEqual(caught.exception.exit_code, ExitCode.USER_ERROR)
            self.assertEqual(caught.exception.category, "invalid_input")

    def test_job_file_pointing_at_a_directory_is_a_user_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(UserInputError, "job file does not exist") as caught:
                load_job(directory)

            self.assertEqual(caught.exception.exit_code, ExitCode.USER_ERROR)


if __name__ == "__main__":
    unittest.main()

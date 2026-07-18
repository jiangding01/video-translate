import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from xvt.cli import main
from xvt.errors import ExitCode, JobLockedError
from xvt.job_store import create_job, save_job
from xvt.locking import batch_lock, job_lock


class LockingTest(unittest.TestCase):
    def test_job_lock_conflict_fails_fast_with_holder_pid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = Path(directory) / "job.json"
            job_file.write_text("{}", encoding="utf-8")
            with job_lock(job_file):
                with self.assertRaisesRegex(JobLockedError, r"PID \d+"):
                    with job_lock(job_file):
                        pass

    def test_job_lock_is_released_after_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = Path(directory) / "job.json"
            job_file.write_text("{}", encoding="utf-8")
            with job_lock(job_file):
                pass
            with job_lock(job_file):
                pass

    def test_batch_lock_conflict_fails_fast(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            batch_file = Path(directory) / "batch.json"
            batch_file.write_text("{}", encoding="utf-8")
            with batch_lock(batch_file):
                with self.assertRaises(JobLockedError):
                    with batch_lock(batch_file):
                        pass

    def test_cli_continue_on_locked_job_returns_locked_exit_code(self) -> None:
        error_output = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            job = create_job("/tmp/input.mp4", directory)
            job_file = save_job(job)

            with (
                job_lock(job_file),
                patch("xvt.cli.continue_workflow") as continue_mock,
                redirect_stderr(error_output),
            ):
                return_code = main(["continue", str(job_file)])

        self.assertEqual(return_code, ExitCode.LOCKED)
        continue_mock.assert_not_called()
        self.assertIn("另一个 XVT 进程", error_output.getvalue())


if __name__ == "__main__":
    unittest.main()

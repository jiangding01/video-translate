import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from xvt.batch import run_batch
from xvt.cli import main
from xvt.errors import ExitCode, JobLockedError
from xvt.job_store import create_job, get_job_file, save_job
from xvt.locking import batch_lock, job_lock, lock_scope
from xvt.pipeline import run_subtitle_transcription
from xvt.workflow import WorkflowOptions, WorkflowResult


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

    def test_job_lock_is_held_before_first_persistence(self) -> None:
        # P7f：job.json 首次落盘时锁必须已被占用——create_job 在 save_job 之前取锁，
        # 并发 continue 在文件出现的瞬间就无法抢占。
        from xvt import job_store

        observations: list[bool] = []
        original_save = job_store.save_job

        def guarded_save(job):
            try:
                with job_lock(job_store.get_job_file(job)):
                    observations.append(False)
            except JobLockedError:
                observations.append(True)
            return original_save(job)

        with tempfile.TemporaryDirectory() as directory:
            with (
                lock_scope(),
                patch("xvt.job_store.save_job", side_effect=guarded_save),
            ):
                create_job("/tmp/input.mp4", directory)

        self.assertEqual(observations, [True])

    def test_run_holds_job_lock_from_creation_until_scope_ends(self) -> None:
        # run 全生命周期锁：Job 创建后锁立即被持有，直到命令锁作用域结束。
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,500\nHello world.\n",
                encoding="utf-8",
            )
            with lock_scope():
                job = run_subtitle_transcription(source, directory, language="en")
                job_file = get_job_file(job)
                with self.assertRaises(JobLockedError):
                    with job_lock(job_file):
                        pass
            # 作用域结束后锁释放，可再次获取。
            with job_lock(job_file):
                pass

    def test_batch_holds_batch_lock_during_first_execution(self) -> None:
        # batch 创建期锁：batch.json 写出前即持锁，首次执行期间独占。
        observed: list[bool] = []

        def fake_run_workflow(options, *, event_sink):
            batch_file = next(Path(options.output).glob("batches/*/batch.json"))
            try:
                with batch_lock(batch_file):
                    observed.append(False)
            except JobLockedError:
                observed.append(True)
            return WorkflowResult("completed", Path("/tmp/job.json"))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / "inputs"
            inputs.mkdir()
            (inputs / "source.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,500\nHello world.\n",
                encoding="utf-8",
            )
            with (
                lock_scope(),
                patch("xvt.batch.run_workflow", side_effect=fake_run_workflow),
            ):
                run_batch(inputs, WorkflowOptions(input="", output=root / "output"))

        self.assertEqual(observed, [True])

    def test_cross_process_continue_conflicts_with_held_lock(self) -> None:
        # 跨进程冲突：本进程持有 Job 锁时，另一个真实进程执行 continue 必须以退出码 7 失败。
        with tempfile.TemporaryDirectory() as directory:
            job = create_job("/tmp/input.mp4", directory)
            job_file = save_job(job)

            with job_lock(job_file):
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "import sys; from xvt.cli import main; "
                        "sys.exit(main(['continue', sys.argv[1]]))",
                        str(job_file),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )

        self.assertEqual(completed.returncode, int(ExitCode.LOCKED), completed.stderr)
        self.assertIn("另一个 XVT 进程", completed.stderr)


if __name__ == "__main__":
    unittest.main()

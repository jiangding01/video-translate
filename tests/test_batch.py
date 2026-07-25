import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xvt.batch import continue_batch, discover_batch_inputs, run_batch
from xvt.errors import JobLockedError
from xvt.locking import job_lock, lock_scope
from xvt.pipeline import apply_subtitle_translation
from xvt.process import CommandCancelled
from xvt.recovery import build_recovery_plan
from xvt.workflow import WorkflowOptions, WorkflowResult


def _write_subtitle(path: Path, text: str = "Hello world.") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"1\n00:00:00,000 --> 00:00:01,500\n{text}\n",
        encoding="utf-8",
    )


class BatchTest(unittest.TestCase):
    def test_discovers_supported_files_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / "inputs"
            output = inputs / "output"
            _write_subtitle(inputs / "B.srt")
            (inputs / "B-alias.srt").symlink_to(inputs / "B.srt")
            _write_subtitle(inputs / "nested" / "a.vtt")
            _write_subtitle(inputs / ".hidden.srt")
            _write_subtitle(output / "generated.srt")
            _write_subtitle(inputs / "jobs" / "old" / "artifacts" / "source.srt")
            (inputs / "notes.txt").write_text("ignore", encoding="utf-8")

            shallow = discover_batch_inputs(inputs, output_root=output)
            recursive = discover_batch_inputs(inputs, output_root=output, recursive=True)
            same_root = discover_batch_inputs(inputs, output_root=inputs, recursive=True)

        self.assertEqual([path.name for path in shallow], ["B.srt"])
        self.assertEqual([path.name for path in recursive], ["B.srt", "source.srt", "a.vtt"])
        self.assertEqual(
            [path.name for path in same_root],
            ["B.srt", "a.vtt", "generated.srt"],
        )

    def test_runs_each_subtitle_as_an_independent_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / "inputs"
            output = root / "output"
            _write_subtitle(inputs / "first.srt", "First.")
            _write_subtitle(inputs / "second.srt", "Second.")

            result = run_batch(
                inputs,
                WorkflowOptions(input="", output=output, language="en"),
            )
            report = json.loads(result.batch_file.read_text(encoding="utf-8"))

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.completed, 2)
        self.assertEqual({item["status"] for item in report["items"]}, {"completed"})
        self.assertEqual(len({item["job_file"] for item in report["items"]}), 2)
        self.assertTrue(all(item["artifacts"] for item in report["items"]))
        self.assertEqual(report["summary"]["completed"], 2)

    def test_burn_rejects_non_video_inputs_before_creating_batch(self) -> None:
        for filename in ("source.srt", "audio.wav"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                inputs = root / "inputs"
                output = root / "output"
                if filename.endswith(".srt"):
                    _write_subtitle(inputs / filename)
                else:
                    inputs.mkdir()
                    (inputs / filename).write_bytes(b"audio")

                with self.assertRaisesRegex(ValueError, "audio or subtitle-only"):
                    run_batch(
                        inputs,
                        WorkflowOptions(
                            input="",
                            output=output,
                            target_language="zh-CN",
                            burn=True,
                        ),
                    )

                self.assertFalse((output / "batches").exists())

    def test_failure_does_not_block_later_items(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / "inputs"
            output = root / "output"
            _write_subtitle(inputs / "bad.srt")
            (inputs / "bad.srt").write_text("not a subtitle", encoding="utf-8")
            _write_subtitle(inputs / "good.srt")

            result = run_batch(inputs, WorkflowOptions(input="", output=output))
            report = json.loads(result.batch_file.read_text(encoding="utf-8"))

        statuses = {Path(item["input"]).name: item["status"] for item in report["items"]}
        self.assertEqual(result.status, "partial_failure")
        self.assertEqual(statuses, {"bad.srt": "failed", "good.srt": "completed"})
        self.assertIsNotNone(report["items"][0]["job_file"])

    def test_agent_waiting_batch_continues_after_translation_import(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / "inputs"
            output = root / "output"
            source = inputs / "source.srt"
            translated = root / "translated.srt"
            _write_subtitle(source)
            _write_subtitle(translated, "你好，世界。")

            waiting = run_batch(
                inputs,
                WorkflowOptions(
                    input="",
                    output=output,
                    language="en",
                    target_language="zh-CN",
                    provider="agent",
                ),
            )
            report = json.loads(waiting.batch_file.read_text(encoding="utf-8"))
            apply_subtitle_translation(
                report["items"][0]["job_file"],
                translated,
                target_language="zh-CN",
            )
            completed = continue_batch(waiting.batch_file)

        self.assertEqual(waiting.status, "awaiting_input")
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.completed, 1)

    def test_batch_persists_relative_glossary_as_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / "inputs"
            output = root / "output"
            glossary = root / "glossary.json"
            _write_subtitle(inputs / "source.srt")
            glossary.write_text('{"terms": []}\n', encoding="utf-8")
            relative_glossary = os.path.relpath(glossary, Path.cwd())

            waiting = run_batch(
                inputs,
                WorkflowOptions(
                    input="",
                    output=output,
                    target_language="zh-CN",
                    provider="agent",
                    glossary=relative_glossary,
                ),
            )
            report = json.loads(waiting.batch_file.read_text(encoding="utf-8"))

        self.assertEqual(report["workflow"]["glossary"], str(glossary.resolve()))

    def test_batch_persists_polished_translation_quality(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / "inputs"
            output = root / "output"
            _write_subtitle(inputs / "source.srt")

            waiting = run_batch(
                inputs,
                WorkflowOptions(
                    input="",
                    output=output,
                    target_language="zh-CN",
                    translation_quality="polished",
                    provider="agent",
                ),
            )
            report = json.loads(waiting.batch_file.read_text(encoding="utf-8"))

        self.assertEqual(waiting.status, "awaiting_input")
        self.assertEqual(report["workflow"]["translation_quality"], "polished")

    def test_continue_does_not_rerun_completed_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / "inputs"
            output = root / "output"
            _write_subtitle(inputs / "source.srt")
            completed = run_batch(inputs, WorkflowOptions(input="", output=output))

            with (
                patch("xvt.batch.run_workflow") as run_workflow,
                patch("xvt.batch.continue_workflow") as continue_workflow,
            ):
                resumed = continue_batch(completed.batch_file)

        self.assertEqual(resumed.status, "completed")
        run_workflow.assert_not_called()
        continue_workflow.assert_not_called()

    def test_continue_does_not_skip_completed_job_with_missing_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / "inputs"
            output = root / "output"
            _write_subtitle(inputs / "source.srt")
            completed = run_batch(inputs, WorkflowOptions(input="", output=output))
            report = json.loads(completed.batch_file.read_text(encoding="utf-8"))
            source_srt = next(
                Path(path)
                for path in report["items"][0]["artifacts"]
                if Path(path).name == "source.srt"
            )
            source_srt.unlink()

            events: list = []
            resumed = continue_batch(completed.batch_file, event_sink=events.append)
            updated = json.loads(completed.batch_file.read_text(encoding="utf-8"))

        self.assertEqual(resumed.status, "partial_failure")
        self.assertEqual(updated["items"][0]["status"], "failed")
        self.assertEqual(updated["items"][0]["recovery_plan"]["category"], "manual_fix")
        failed = next(event for event in events if event.type == "batch_item_failed")
        self.assertEqual(failed.data["index"], 1)
        self.assertEqual(failed.data["total"], 1)

    def test_interruption_is_persisted_before_propagating(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / "inputs"
            output = root / "output"
            _write_subtitle(inputs / "source.srt")

            with patch("xvt.batch.run_workflow", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    run_batch(inputs, WorkflowOptions(input="", output=output))

            batch_file = next((output / "batches").glob("*/batch.json"))
            report = json.loads(batch_file.read_text(encoding="utf-8"))

        self.assertEqual(report["items"][0]["status"], "interrupted")
        self.assertEqual(report["status"], "interrupted")
        self.assertEqual(report["summary"]["failed"], 1)
        self.assertIn("xvt batch-continue", report["next_action"])

    def test_batch_reads_recovery_state_under_job_lock(self) -> None:
        # P7f：batch 对已有 Job 的恢复分类与 artifact 校验必须发生在 Job 锁之下，
        # 否则并发命令可在读取与执行之间改动 Job，导致按旧状态误判条目完成。
        observed: list[bool] = []
        original_plan = build_recovery_plan

        def guarded(job_file, *args, **kwargs):
            try:
                with job_lock(job_file):
                    observed.append(False)
            except JobLockedError:
                observed.append(True)
            return original_plan(job_file, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / "inputs"
            _write_subtitle(inputs / "source.srt")
            completed = run_batch(inputs, WorkflowOptions(input="", output=root / "output"))

            with (
                lock_scope(),
                patch("xvt.batch.build_recovery_plan", side_effect=guarded),
            ):
                continue_batch(completed.batch_file)

        self.assertTrue(observed)
        self.assertTrue(all(observed), "recovery state was read without holding the job lock")

    def test_locked_item_does_not_reread_recovery_state_without_lock(self) -> None:
        # P7g：条目取 Job 锁失败（另一进程正在写入）时，失败记录路径不得在
        # 无锁状态下重读恢复计划与 artifact——留待 batch-continue 在锁下重算。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / "inputs"
            _write_subtitle(inputs / "source.srt")
            completed = run_batch(inputs, WorkflowOptions(input="", output=root / "output"))
            report = json.loads(completed.batch_file.read_text(encoding="utf-8"))
            job_file = report["items"][0]["job_file"]

            with (
                job_lock(job_file),
                patch("xvt.batch.build_recovery_plan") as plan_mock,
                lock_scope(),
            ):
                resumed = continue_batch(completed.batch_file)
            updated = json.loads(completed.batch_file.read_text(encoding="utf-8"))

        plan_mock.assert_not_called()
        self.assertEqual(resumed.status, "partial_failure")
        self.assertEqual(updated["items"][0]["status"], "failed")
        self.assertIsNone(updated["items"][0]["recovery_plan"])
        self.assertIn("另一个 XVT 进程", updated["items"][0]["error"])

    def test_command_cancelled_is_persisted_as_interruption_not_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / "inputs"
            output = root / "output"
            _write_subtitle(inputs / "source.srt")

            with patch("xvt.batch.run_workflow", side_effect=CommandCancelled("cancelled")):
                with self.assertRaises(CommandCancelled):
                    run_batch(inputs, WorkflowOptions(input="", output=output))

            batch_file = next((output / "batches").glob("*/batch.json"))
            report = json.loads(batch_file.read_text(encoding="utf-8"))

        self.assertEqual(report["items"][0]["status"], "interrupted")
        self.assertEqual(report["status"], "interrupted")

    def test_failure_message_is_redacted_in_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / "inputs"
            output = root / "output"
            _write_subtitle(inputs / "source.srt")
            events = []

            with patch(
                "xvt.batch.run_workflow",
                side_effect=ValueError("https://example.com/video?token=secret"),
            ):
                result = run_batch(
                    inputs, WorkflowOptions(input="", output=output), event_sink=events.append
                )
            report = json.loads(result.batch_file.read_text(encoding="utf-8"))

        self.assertNotIn("secret", report["items"][0]["error"])
        self.assertIn("%3Credacted%3E", report["items"][0]["error"])
        failed = next(event for event in events if event.type == "batch_item_failed")
        self.assertNotIn("error_type", failed.data)
        self.assertNotIn("message", failed.data)
        self.assertEqual(failed.data["error"]["category"], "workflow")
        self.assertNotIn("secret", failed.data["error"]["message"])
        self.assertIn("%3Credacted%3E", failed.data["error"]["message"])

    def test_waiting_message_is_redacted_in_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / "inputs"
            output = root / "output"
            _write_subtitle(inputs / "source.srt")
            workflow_result = WorkflowResult(
                "awaiting_input",
                root / "job.json",
                message="https://example.com/v1?token=secret",
            )

            with (
                patch("xvt.batch.run_workflow", return_value=workflow_result),
                patch("xvt.batch._record_recovery", return_value="awaiting_input"),
                patch("xvt.batch._record_artifacts"),
            ):
                result = run_batch(inputs, WorkflowOptions(input="", output=output))
            report = json.loads(result.batch_file.read_text(encoding="utf-8"))

        self.assertNotIn("secret", report["items"][0]["error"])
        self.assertIn("%3Credacted%3E", report["items"][0]["error"])

    def test_continue_rejects_malformed_item_without_key_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            batch_file = Path(directory) / "batch.json"
            batch_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "output_root": str(Path(directory).resolve()),
                        "workflow": {},
                        "items": [{"status": "pending", "job_file": None}],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "item 1 input"):
                continue_batch(batch_file)

            batch_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "output_root": str(Path(directory).resolve()),
                        "workflow": {},
                        "items": [
                            {
                                "input": str((Path(directory) / "source.srt").resolve()),
                                "status": "failed",
                                "job_file": "/tmp/outside/job.json",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "inside output_root/jobs"):
                continue_batch(batch_file)


if __name__ == "__main__":
    unittest.main()

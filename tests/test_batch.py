import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xvt.batch import BATCH_SCHEMA_VERSION, continue_batch, discover_batch_inputs, run_batch
from xvt.errors import ExitCode, JobLockedError, UserInputError
from xvt.job_store import create_job, save_job
from xvt.locking import job_lock, lock_scope
from xvt.models import Artifact, StageName, StageStatus
from xvt.pipeline import apply_subtitle_translation, retranslate_cues
from xvt.process import CommandCancelled
from xvt.recovery import build_recovery_plan, file_checksum
from xvt.workflow import WorkflowOptions, WorkflowResult, _record_burn_fingerprint


def _write_subtitle(path: Path, text: str = "Hello world.") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"1\n00:00:00,000 --> 00:00:01,500\n{text}\n",
        encoding="utf-8",
    )


class BatchTest(unittest.TestCase):
    def test_continue_waits_for_relink_candidate_without_calling_workflow_continue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            media = root / "video.mp4"
            media.write_bytes(b"media")
            job = create_job(
                str(media),
                output,
                {"workflow": {"target_language": "zh-CN", "burn": True}},
            )
            artifacts = root / "artifacts"
            artifacts.mkdir()
            source_cues = artifacts / "source-cues.json"
            translated_cues = artifacts / "translated-cues.json"
            translated_srt = artifacts / "translated.srt"
            bilingual_srt = artifacts / "bilingual.srt"
            burned = artifacts / "burned.mp4"
            media_qa = artifacts / "media-qa.json"
            translated_qa = artifacts / "qa-translated.json"
            source_cues.write_text("[]", encoding="utf-8")
            translated_cues.write_text("[]", encoding="utf-8")
            translated_srt.write_text("", encoding="utf-8")
            bilingual_srt.write_text("", encoding="utf-8")
            burned.write_bytes(b"burned-output")
            media_qa.write_text('{"status":"pass","issues":[]}\n', encoding="utf-8")
            translated_qa.write_text('{"status":"pass","issues":[]}\n', encoding="utf-8")
            job.artifacts.extend(
                [
                    Artifact("source_cues", str(source_cues), file_checksum(source_cues)),
                    Artifact(
                        "translated_cues",
                        str(translated_cues),
                        file_checksum(translated_cues),
                    ),
                    Artifact("translated_srt", str(translated_srt), file_checksum(translated_srt)),
                    Artifact("bilingual_srt", str(bilingual_srt), file_checksum(bilingual_srt)),
                    Artifact(
                        "translated_subtitle_qa",
                        str(translated_qa),
                        file_checksum(translated_qa),
                    ),
                    Artifact("burned_video", str(burned), file_checksum(burned)),
                    Artifact("media_qa", str(media_qa), file_checksum(media_qa)),
                ]
            )
            job.stages[StageName.MEDIA_QA].status = StageStatus.SUCCEEDED
            job_file = save_job(job)
            _record_burn_fingerprint(job_file)
            burned.unlink()
            batch_file = output / "batches" / "test" / "batch.json"
            batch_file.parent.mkdir(parents=True)
            batch_file.write_text(
                json.dumps(
                    {
                        "schema_version": BATCH_SCHEMA_VERSION,
                        "id": "test",
                        "output_root": str(output.resolve()),
                        "workflow": {},
                        "items": [
                            {
                                "input": str(media.resolve()),
                                "status": "pending",
                                "job_file": str(job_file.resolve()),
                                "artifacts": [],
                                "error": None,
                                "recovery_plan": None,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with patch("xvt.batch.continue_workflow") as continue_workflow:
                result = continue_batch(batch_file)
            updated = json.loads(batch_file.read_text(encoding="utf-8"))

        self.assertEqual(result.status, "awaiting_input")
        self.assertEqual(updated["items"][0]["status"], "awaiting_input")
        self.assertEqual(updated["items"][0]["recovery_plan"]["category"], "awaiting_input")
        self.assertIn(
            "relink-burned-output",
            updated["items"][0]["recovery_plan"]["primary_command"],
        )
        # 场景 A 回归守卫：目标产物（burned_video）失效必须停在 relink 引导，
        # 绝不能被「非目标产物失效只标注」的放行逻辑一起放成 completed/degraded。
        self.assertIsNone(updated["items"][0].get("verification"))
        continue_workflow.assert_not_called()

    def _burned_complete_batch(self, root: Path) -> tuple[Path, Path, Path]:
        """构造一个目标全部达成的烧录条目，外加一个可删除的中间产物。"""

        output = root / "output"
        media = root / "video.mp4"
        media.write_bytes(b"media")
        job = create_job(
            str(media),
            output,
            {"workflow": {"target_language": "zh-CN", "burn": True}},
        )
        artifacts = root / "artifacts"
        artifacts.mkdir()
        payloads = {
            "source_cues": ("source-cues.json", "[]"),
            "translated_cues": ("translated-cues.json", "[]"),
            "translated_srt": ("translated.srt", ""),
            "bilingual_srt": ("bilingual.srt", ""),
            "translated_subtitle_qa": ("qa-translated.json", '{"status":"pass","issues":[]}\n'),
            "media_qa": ("media-qa.json", '{"status":"pass","issues":[]}\n'),
        }
        for kind, (name, text) in payloads.items():
            path = artifacts / name
            path.write_text(text, encoding="utf-8")
            job.artifacts.append(Artifact(kind, str(path), file_checksum(path)))
        burned = artifacts / "burned.mp4"
        burned.write_bytes(b"burned-output")
        job.artifacts.append(Artifact("burned_video", str(burned), file_checksum(burned)))
        audio = artifacts / "normalized.wav"
        audio.write_bytes(b"normalized audio payload")
        job.artifacts.append(Artifact("normalized_audio", str(audio), file_checksum(audio)))
        job.stages[StageName.MEDIA_QA].status = StageStatus.SUCCEEDED
        job_file = save_job(job)
        _record_burn_fingerprint(job_file)
        batch_file = output / "batches" / "test" / "batch.json"
        batch_file.parent.mkdir(parents=True, exist_ok=True)
        batch_file.write_text(
            json.dumps(
                {
                    "schema_version": BATCH_SCHEMA_VERSION,
                    "id": "test",
                    "output_root": str(output.resolve()),
                    "workflow": {},
                    "items": [
                        {
                            "input": str(media.resolve()),
                            "status": "pending",
                            "job_file": str(job_file.resolve()),
                            "artifacts": [],
                            "error": None,
                            "recovery_plan": None,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return batch_file, job_file, audio

    def test_completed_item_with_invalid_intermediate_artifact_stays_completed(self) -> None:
        """用户删掉 normalized_audio 这类中间文件后，已达成目标的条目不再被改写成 failed。

        旧行为是 `failed` + 退出码 6，且引导给的 `xvt verify` 只会报告失效不会修复——
        再跑一次 `batch-continue` 仍是 6，条目永远走不出去。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch_file, _job_file, audio = self._burned_complete_batch(root)
            audio.unlink()

            with patch("xvt.batch.continue_workflow") as continue_workflow:
                result = continue_batch(batch_file)
            updated = json.loads(batch_file.read_text(encoding="utf-8"))
            item = updated["items"][0]

        self.assertEqual(result.status, "completed")
        self.assertEqual(item["status"], "completed")
        self.assertEqual(item["verification"]["status"], "degraded")
        self.assertEqual(item["verification"]["invalid_artifacts"], ["normalized_audio"])
        # 恢复计划仍是 complete（与单 Job status 一致），只在 reason 里追加说明。
        self.assertEqual(item["recovery_plan"]["category"], "complete")
        self.assertIn("normalized_audio", item["recovery_plan"]["reason"])
        self.assertIn("xvt verify", item["recovery_plan"]["reason"])
        continue_workflow.assert_not_called()

    def test_batch_item_completed_event_carries_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch_file, _job_file, audio = self._burned_complete_batch(root)
            audio.unlink()

            events: list = []
            with patch("xvt.batch.continue_workflow"):
                continue_batch(batch_file, event_sink=events.append)
            degraded = next(event for event in events if event.type == "batch_item_completed")

            fresh_root = root / "fresh"
            inputs = fresh_root / "inputs"
            _write_subtitle(inputs / "source.srt")
            fresh_events: list = []
            run_batch(
                inputs,
                WorkflowOptions(input="", output=fresh_root / "output"),
                event_sink=fresh_events.append,
            )
            fresh = next(event for event in fresh_events if event.type == "batch_item_completed")

        self.assertEqual(
            degraded.data["verification"],
            {"status": "degraded", "invalid_artifacts": ["normalized_audio"]},
        )
        # 刚跑完的条目也带该字段（值为 null），消费端不必按字段在不在分叉。
        self.assertIn("verification", fresh.data)
        self.assertIsNone(fresh.data["verification"])

    def test_batch_continue_is_idempotent_for_degraded_items(self) -> None:
        """连跑两次都必须是 completed / 退出码 0：旧行为是两次都 failed / 6（死循环）。"""

        from contextlib import redirect_stdout
        from io import StringIO

        from xvt.cli import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch_file, _job_file, audio = self._burned_complete_batch(root)
            audio.unlink()

            codes = []
            for _ in range(2):
                with redirect_stdout(StringIO()):
                    codes.append(main(["batch-continue", str(batch_file)]))
            updated = json.loads(batch_file.read_text(encoding="utf-8"))

        self.assertEqual(codes, [0, 0])
        self.assertEqual(updated["items"][0]["status"], "completed")
        self.assertEqual(updated["items"][0]["verification"]["status"], "degraded")

    def test_completed_job_with_missing_source_srt_is_repaired_not_shortcut(self) -> None:
        """`source_srt` 不在 `plan_advance_steps` 的判定里，但纯转写批次上它就是交付物。

        因此它不能走「非目标产物失效只标注」的放行分支：条目必须落到 `continue`，
        由 `resume_job` 真正重建 source.srt，而不是拿着不存在的成品报完成。
        """

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
            rebuilt = source_srt.exists()

        completed_event = next(event for event in events if event.type == "batch_item_completed")
        self.assertEqual(resumed.status, "completed")
        self.assertEqual(updated["items"][0]["status"], "completed")
        self.assertTrue(rebuilt)
        # 关键守卫：没有走 reused=True 的直通车，而是真的跑了一次 continue。
        self.assertNotIn("reused", completed_event.data)
        self.assertIsNone(updated["items"][0]["verification"])
        self.assertTrue(any(event.type == "job_resumed" for event in events))

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

    def test_legacy_batch_without_translation_quality_defaults_to_standard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / "inputs"
            output = root / "output"
            _write_subtitle(inputs / "source.srt")
            completed = run_batch(inputs, WorkflowOptions(input="", output=output))
            report = json.loads(completed.batch_file.read_text(encoding="utf-8"))
            report["workflow"].pop("translation_quality")

            options = WorkflowOptions(
                input=report["items"][0]["input"],
                output=report["output_root"],
                **report["workflow"],
            )

        self.assertEqual(options.translation_quality, "standard")

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

    def test_failed_items_outrank_awaiting_items_in_the_batch_status(self) -> None:
        """`--fail-on-waiting` 的「6 优先于 10」靠这条结构性保证：混合态必然是 partial_failure。"""

        from xvt.batch import _result_status

        mixed = {"total": 3, "completed": 1, "awaiting_input": 1, "failed": 1, "pending": 0}
        self.assertEqual(_result_status(mixed), "partial_failure")
        self.assertEqual(
            _result_status({**mixed, "failed": 0, "pending": 1}),
            "partial_failure",
        )
        self.assertEqual(
            _result_status({**mixed, "failed": 0, "pending": 0}),
            "awaiting_input",
        )
        self.assertEqual(
            _result_status({**mixed, "failed": 0, "pending": 0, "awaiting_input": 0}),
            "completed",
        )

    def test_missing_batch_file_is_a_user_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "gone" / "batch.json"

            with self.assertRaisesRegex(UserInputError, "batch file does not exist") as caught:
                continue_batch(missing)

            self.assertEqual(caught.exception.exit_code, ExitCode.USER_ERROR)

    def test_batch_continue_cli_returns_user_error_for_missing_batch_file(self) -> None:
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO

        from xvt.cli import main

        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / "gone" / "batch.json")
            output, errors = StringIO(), StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                return_code = main(["batch-continue", missing])

        self.assertEqual(return_code, ExitCode.USER_ERROR)
        combined = output.getvalue() + errors.getvalue()
        self.assertNotIn("Traceback", combined)
        self.assertIn("batch file does not exist", combined)


if __name__ == "__main__":
    unittest.main()


class BatchCueRetranslationTest(unittest.TestCase):
    """等待项绝不由批处理自动推进：局部重译等待必须停在 awaiting_input。"""

    def _waiting_batch(self, root: Path) -> tuple[Path, Path]:
        output = root / "output"
        media = root / "video.mp4"
        media.write_bytes(b"media")
        job = create_job(str(media), output, {"workflow": {"target_language": "zh-CN"}})
        # 重译 manifest 会写进 Job 目录，先把它建出来。
        (output / "jobs" / job.id / "artifacts").mkdir(parents=True)
        artifacts = root / "artifacts"
        artifacts.mkdir()
        source_cues = artifacts / "source-cues.json"
        source_cues.write_text(
            json.dumps([{"id": 1, "start_ms": 0, "end_ms": 1000, "source_text": "Hello world."}]),
            encoding="utf-8",
        )
        translated_cues = artifacts / "translated-cues.json"
        translated_cues.write_text(
            json.dumps(
                [
                    {
                        "id": 1,
                        "start_ms": 0,
                        "end_ms": 1000,
                        "source_text": "Hello world.",
                        "target_text": "你好，世界",
                        "target_language": "zh-CN",
                    }
                ]
            ),
            encoding="utf-8",
        )
        translated_srt = artifacts / "translated.srt"
        translated_srt.write_text("", encoding="utf-8")
        bilingual_srt = artifacts / "bilingual.srt"
        bilingual_srt.write_text("", encoding="utf-8")
        translated_qa = artifacts / "qa-translated.json"
        translated_qa.write_text('{"status":"pass","issues":[]}\n', encoding="utf-8")
        job.artifacts.extend(
            [
                Artifact("source_cues", str(source_cues), file_checksum(source_cues)),
                Artifact("translated_cues", str(translated_cues), file_checksum(translated_cues)),
                Artifact("translated_srt", str(translated_srt), file_checksum(translated_srt)),
                Artifact("bilingual_srt", str(bilingual_srt), file_checksum(bilingual_srt)),
                Artifact(
                    "translated_subtitle_qa", str(translated_qa), file_checksum(translated_qa)
                ),
            ]
        )
        job_file = save_job(job)
        batch_file = output / "batches" / "test" / "batch.json"
        batch_file.parent.mkdir(parents=True)
        batch_file.write_text(
            json.dumps(
                {
                    "schema_version": BATCH_SCHEMA_VERSION,
                    "id": "test",
                    "output_root": str(output.resolve()),
                    "workflow": {},
                    "items": [
                        {
                            "input": str(media.resolve()),
                            "status": "pending",
                            "job_file": str(job_file.resolve()),
                            "artifacts": [],
                            "error": None,
                            "recovery_plan": None,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return batch_file, job_file

    def test_pending_cue_retranslation_is_not_marked_completed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch_file, job_file = self._waiting_batch(root)

            # 请求重译之前，这个条目对批处理而言是「已完成」。
            with patch("xvt.batch.continue_workflow") as continue_workflow:
                completed = continue_batch(batch_file)
            self.assertEqual(completed.status, "completed")
            self.assertEqual(
                json.loads(batch_file.read_text(encoding="utf-8"))["items"][0]["status"],
                "completed",
            )
            continue_workflow.assert_not_called()

            batch = json.loads(batch_file.read_text(encoding="utf-8"))
            batch["items"][0]["status"] = "pending"
            batch_file.write_text(json.dumps(batch), encoding="utf-8")
            retranslate_cues(job_file, cue_ids=(1,), target_language="zh-CN", provider="agent")

            with patch("xvt.batch.continue_workflow") as continue_workflow:
                result = continue_batch(batch_file)
            updated = json.loads(batch_file.read_text(encoding="utf-8"))

        self.assertEqual(result.status, "awaiting_input")
        self.assertEqual(updated["items"][0]["status"], "awaiting_input")
        self.assertEqual(updated["items"][0]["recovery_plan"]["category"], "awaiting_input")
        self.assertIn(
            "apply-cue-retranslation",
            updated["items"][0]["recovery_plan"]["prerequisite_commands"][0],
        )
        continue_workflow.assert_not_called()

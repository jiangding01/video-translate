import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xvt.batch import BATCH_SCHEMA_VERSION, continue_batch, discover_batch_inputs, run_batch
from xvt.errors import ExitCode, JobLockedError, UserInputError
from xvt.job_store import create_job, get_job_file, load_job, save_job
from xvt.locking import job_lock, lock_scope
from xvt.media import MediaInfo
from xvt.models import Artifact, StageName, StageStatus, Transcript, Word
from xvt.pipeline import (
    apply_agent_translation,
    apply_subtitle_translation,
    prepare_agent_translation,
    retranslate_cues,
    run_local_transcription,
    run_subtitle_transcription,
)
from xvt.process import CommandCancelled, CommandResult
from xvt.recovery import build_recovery_plan, file_checksum, valid_artifact_path
from xvt.translate import GlossaryTerm, TranslationPolicy
from xvt.workflow import WorkflowOptions, WorkflowResult, _record_burn_fingerprint
from xvt.workflow_config import WorkflowConfig, set_workflow_config


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

    def test_interruption_while_emitting_batch_started_is_persisted(self) -> None:
        # 缺陷9窗口①：batch.json 已写成 running，但 batch_started 还在发射。
        # 该事件必须位于 _process_batch 的外层 try 内，否则中断会留下一个没有
        # next_action 的 running 批次。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / "inputs"
            output = root / "output"
            _write_subtitle(inputs / "source.srt")
            events = []

            def raise_on_batch_started(event):
                events.append(event)
                if event.type == "batch_started":
                    raise KeyboardInterrupt

            with self.assertRaises(KeyboardInterrupt):
                run_batch(
                    inputs,
                    WorkflowOptions(input="", output=output),
                    event_sink=raise_on_batch_started,
                )

            batch_file = next((output / "batches").glob("*/batch.json"))
            report = json.loads(batch_file.read_text(encoding="utf-8"))

        self.assertEqual(report["status"], "interrupted")
        self.assertEqual(report["items"][0]["status"], "pending")
        self.assertIn("xvt batch-continue", report["next_action"])
        self.assertEqual([event.type for event in events], ["batch_started"])

    def test_interruption_between_items_is_persisted(self) -> None:
        # 缺陷9窗口②：SIGINT 落在两个 item 之间——第一个 item 已跑完、第二个还没
        # 进 lock_scope。_process_item 内部没有机会落盘，得靠 _process_batch 主循环
        # 外层的兜底 except 补上。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / "inputs"
            output = root / "output"
            _write_subtitle(inputs / "a.srt")
            _write_subtitle(inputs / "b.srt")

            call_count = {"n": 0}

            def fake_lock_scope():
                call_count["n"] += 1
                if call_count["n"] == 2:
                    raise KeyboardInterrupt
                return lock_scope()

            with patch("xvt.batch.lock_scope", side_effect=fake_lock_scope):
                with self.assertRaises(KeyboardInterrupt):
                    run_batch(inputs, WorkflowOptions(input="", output=output))

            batch_file = next((output / "batches").glob("*/batch.json"))
            report = json.loads(batch_file.read_text(encoding="utf-8"))

        statuses = [item["status"] for item in report["items"]]
        self.assertNotIn("running", statuses)
        self.assertEqual(report["status"], "interrupted")
        self.assertIn("xvt batch-continue", report["next_action"])

    def test_interruption_after_running_write_but_before_try_is_persisted(self) -> None:
        # 缺陷9窗口③：item 已被置为 running 并落盘（batch.py 里 _write_batch 那次），
        # 但 _process_item 的 try 块还没开始——此时中断会绕过 _process_item 自己的
        # except，必须靠外层兜底把这个仍标 running 的条目降级为 interrupted。
        # BATCH_ITEM_STARTED 事件恰好在“running 落盘之后、try 之前”触发，借它注入。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / "inputs"
            output = root / "output"
            _write_subtitle(inputs / "source.srt")

            events = []

            def raise_on_item_started(event):
                events.append(event)
                if event.type == "batch_item_started":
                    raise KeyboardInterrupt

            with self.assertRaises(KeyboardInterrupt):
                run_batch(
                    inputs,
                    WorkflowOptions(input="", output=output),
                    event_sink=raise_on_item_started,
                )

            batch_file = next((output / "batches").glob("*/batch.json"))
            report = json.loads(batch_file.read_text(encoding="utf-8"))

        self.assertEqual(report["items"][0]["status"], "interrupted")
        self.assertEqual(report["status"], "interrupted")
        self.assertIn("xvt batch-continue", report["next_action"])
        # 条目已发过 batch_item_started，就必须有条目级终态事件——否则消费端
        # 会把它永远当成在跑（运行级 cli_failed 只给批次终态）。
        interrupted = [event for event in events if event.type == "batch_item_interrupted"]
        self.assertEqual(len(interrupted), 1)
        self.assertEqual(interrupted[0].data["index"], 1)
        self.assertEqual(interrupted[0].data["input"], report["items"][0]["input"])
        self.assertEqual(interrupted[0].data["total"], 1)
        self.assertIn("xvt batch-continue", interrupted[0].data["next_action"])
        self.assertEqual(
            [event.type for event in events],
            ["batch_started", "batch_item_started", "batch_item_interrupted"],
        )

    def test_failing_sink_does_not_swallow_interruption(self) -> None:
        # 兜底补发跑在 KeyboardInterrupt 的传播路径上：sink 自己抛异常时，
        # 中断必须原样传播（否则 130 退出码被顶掉），batch.json 也必须已落盘。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / "inputs"
            output = root / "output"
            _write_subtitle(inputs / "source.srt")

            def hostile_sink(event):
                if event.type == "batch_item_started":
                    raise KeyboardInterrupt
                if event.type == "batch_item_interrupted":
                    raise RuntimeError("sink exploded")

            with self.assertRaises(KeyboardInterrupt):
                run_batch(
                    inputs,
                    WorkflowOptions(input="", output=output),
                    event_sink=hostile_sink,
                )

            batch_file = next((output / "batches").glob("*/batch.json"))
            report = json.loads(batch_file.read_text(encoding="utf-8"))

        self.assertEqual(report["items"][0]["status"], "interrupted")
        self.assertEqual(report["status"], "interrupted")
        self.assertIn("xvt batch-continue", report["next_action"])

    def test_item_interruption_sink_failure_preserves_original_interruption(self) -> None:
        # workflow 内部中断走 _process_item 自己的 except；条目终态事件只是尽力而为，
        # stdout/JSONL 管道关闭不能用 BrokenPipeError 顶掉原始 KeyboardInterrupt。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / "inputs"
            output = root / "output"
            _write_subtitle(inputs / "source.srt")

            def broken_interruption_sink(event):
                if event.type == "batch_item_interrupted":
                    raise BrokenPipeError("closed pipe")

            with patch("xvt.batch.run_workflow", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    run_batch(
                        inputs,
                        WorkflowOptions(input="", output=output),
                        event_sink=broken_interruption_sink,
                    )

            batch_file = next((output / "batches").glob("*/batch.json"))
            report = json.loads(batch_file.read_text(encoding="utf-8"))

        self.assertEqual(report["items"][0]["status"], "interrupted")
        self.assertEqual(report["status"], "interrupted")
        self.assertIn("xvt batch-continue", report["next_action"])

    def test_interruption_before_final_summary_write_is_persisted(self) -> None:
        # 缺陷9窗口④：所有 item 都已处理完，但汇总落盘（_result 之后的 _write_batch）
        # 还没执行——中断必须让 batch.json 落到 interrupted 而不是停在 running。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / "inputs"
            output = root / "output"
            _write_subtitle(inputs / "source.srt")

            with patch("xvt.batch._result", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    run_batch(inputs, WorkflowOptions(input="", output=output))

            batch_file = next((output / "batches").glob("*/batch.json"))
            report = json.loads(batch_file.read_text(encoding="utf-8"))

        self.assertEqual(report["items"][0]["status"], "completed")
        self.assertEqual(report["status"], "interrupted")
        self.assertIn("xvt batch-continue", report["next_action"])

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


def _write_batch_file(output: Path, media: Path, job_file: Path) -> Path:
    """按批处理文件格式登记单条已有 Job 的条目。"""

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
    return batch_file


class BatchBilingualAssDeliverableTest(unittest.TestCase):
    """bilingual_ass 是交付物，不是中间产物：失效时不能被 degraded 放行。

    旧行为把它归到「不影响成品的中间产物」里，条目标 completed + degraded，用户
    再跑多少次 batch-continue 都拿不回 ASS。修好之后条目会落到 continue，走零 API
    成本的重渲染。
    """

    def _translated_video_batch(self, root: Path) -> tuple[Path, Path]:
        output = root / "output"
        media = root / "input.mp4"
        media.write_bytes(b"video")

        def run_ffmpeg(args, **_kwargs):
            Path(args[-1]).write_bytes(b"audio")
            return CommandResult(tuple(args), 0, "", "")

        media_info = MediaInfo(
            path=str(media),
            duration_ms=5000,
            size_bytes=5,
            format_name="mp4",
            width=1920,
            height=1080,
            frame_rate=30.0,
            video_codec="h264",
            audio_codec="aac",
            has_video=True,
            has_audio=True,
        )
        transcript = Transcript(
            language="en",
            engine="mlx",
            words=(Word("Hello.", 1000, 1800, 0.9),),
        )
        with (
            patch("xvt.pipeline.transcription.local.probe_media", return_value=media_info),
            patch("xvt.pipeline.transcription.local.run_command", side_effect=run_ffmpeg),
            patch("xvt.pipeline.transcription.local.transcribe_audio", return_value=transcript),
        ):
            job = run_local_transcription(media, output)
        job_file = get_job_file(job)
        set_workflow_config(job, WorkflowConfig(target_language="zh-CN", provider="agent"))
        save_job(job)
        prepare_agent_translation(
            job_file,
            source_language="en",
            target_language="zh-CN",
        )
        response = root / "translation-response.json"
        response.write_text(
            json.dumps({"items": [{"id": 1, "text": "你好。"}]}),
            encoding="utf-8",
        )
        apply_agent_translation(job_file, response, target_language="zh-CN")
        return _write_batch_file(output, media, job_file), job_file

    def test_invalid_bilingual_ass_is_rebuilt_instead_of_marked_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch_file, job_file = self._translated_video_batch(root)
            ass_path = valid_artifact_path(load_job(job_file), "bilingual_ass")
            self.assertIsNotNone(ass_path)
            assert ass_path is not None
            ass_path.unlink()

            # 真实 continue_workflow：桩会绕过重渲染，让「最终 ASS 有效」变成假绿。
            result = continue_batch(batch_file)
            item = json.loads(batch_file.read_text(encoding="utf-8"))["items"][0]
            rebuilt = load_job(job_file)
            # 临时目录退出后文件就没了，落盘校验必须在作用域内取值。
            rebuilt_ass = valid_artifact_path(rebuilt, "bilingual_ass")
            ass_restored = ass_path.exists()

        self.assertEqual(result.status, "completed")
        self.assertEqual(item["status"], "completed")
        # 关键守卫：不得以 degraded「不影响成品」的名义放行。
        self.assertIsNone(item.get("verification"))
        self.assertTrue(ass_restored)
        self.assertIsNotNone(rebuilt_ass)

    def test_intact_item_is_still_reused_without_continue(self) -> None:
        """护栏：ASS 有效时条目照旧复用，不该因为新增校验多跑一次 continue。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch_file, _job_file = self._translated_video_batch(root)

            with patch("xvt.batch.continue_workflow") as continue_workflow:
                result = continue_batch(batch_file)
            item = json.loads(batch_file.read_text(encoding="utf-8"))["items"][0]

        self.assertEqual(result.status, "completed")
        self.assertEqual(item["status"], "completed")
        self.assertIsNone(item.get("verification"))
        continue_workflow.assert_not_called()


class BatchQaBlockedCueRetranslationTest(unittest.TestCase):
    """SUBTITLE_QA 停在 failed、旁边挂着有效局部重译 manifest 的条目必须停在 awaiting_input。

    契约见 HANDOFF：continue 不宣告完成，batch-continue 停在该条目并给出回填引导。
    这两个状态完全可以同时为真——术语死锁的出口就是 retranslate-cues，回填前译文
    QA 不会重跑，所以 SUBTITLE_QA 不会自动复位。旧行为把它按 qa_blocked 处理，条目
    被记成 failed，用户手里那份 manifest 反而没人认。
    """

    def _qa_blocked_waiting_batch(self, root: Path) -> tuple[Path, Path]:
        output = root / "output"
        source = root / "source.srt"
        source.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nSource 1.\n\n",
            encoding="utf-8",
        )
        job = run_subtitle_transcription(source, output, language="en")
        job_file = get_job_file(job)
        set_workflow_config(
            job,
            WorkflowConfig(
                target_language="zh-CN",
                requested_outputs=("translated_subtitles", "bilingual_subtitles"),
                provider="agent",
                batch_size=1,
                translation_policy=TranslationPolicy(terms=(GlossaryTerm("Source", "小组件"),)),
            ),
        )
        save_job(job)
        prepare_agent_translation(
            job_file,
            source_language="en",
            target_language="zh-CN",
            await_input=False,
        )
        response = root / "translation-response.json"
        response.write_text(
            json.dumps({"items": [{"id": 1, "text": "译文一"}]}),
            encoding="utf-8",
        )
        # 术语违规让译文 QA 落在 failed；stage 与报告在抛出前已经落盘。
        with self.assertRaises(RuntimeError):
            apply_agent_translation(job_file, response, target_language="zh-CN")
        blocked = load_job(job_file)
        self.assertIs(blocked.stages[StageName.SUBTITLE_QA].status, StageStatus.FAILED)
        # 术语死锁的出口：请求局部重译，等 Agent 回填。
        retranslate_cues(job_file, cue_ids=(1,), target_language="zh-CN", provider="agent")
        return _write_batch_file(output, source, job_file), job_file

    def test_qa_blocked_item_with_pending_cue_retranslation_waits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch_file, job_file = self._qa_blocked_waiting_batch(root)

            with patch("xvt.batch.continue_workflow") as continue_workflow:
                result = continue_batch(batch_file)
            item = json.loads(batch_file.read_text(encoding="utf-8"))["items"][0]
            still_failed = load_job(job_file).stages[StageName.SUBTITLE_QA].status

        # 前提复述：QA 阻断与在途等待同时为真，等待才是真状态。
        self.assertIs(still_failed, StageStatus.FAILED)
        self.assertEqual(result.status, "awaiting_input")
        self.assertEqual(item["status"], "awaiting_input")
        self.assertEqual(item["recovery_plan"]["category"], "awaiting_input")
        self.assertEqual(item["recovery_plan"]["advance_steps"], ["await_cue_retranslation"])
        guidance = " ".join(
            [
                *item["recovery_plan"]["prerequisite_commands"],
                item["recovery_plan"]["primary_command"] or "",
            ]
        )
        self.assertIn("apply-cue-retranslation", guidance)
        continue_workflow.assert_not_called()


if __name__ == "__main__":
    unittest.main()

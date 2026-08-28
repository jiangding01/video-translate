import json
import os
import shlex
import signal
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from xvt import cancellation
from xvt.batch import BatchResult
from xvt.cli import (
    _SOURCE_REVIEW_SRT_NAME,
    _STEP_ROUTES,
    _TASK_ROUTES,
    _TRANSLATION_REVIEW_SRT_NAME,
    _UNROUTABLE_AWAIT_STEPS,
    _print_batch_result,
    main,
)
from xvt.errors import DependencyError, ExitCode, InternalError, UserInputError
from xvt.events import EventType, create_event
from xvt.job_store import create_job, get_job_file, load_job, save_job
from xvt.models import Artifact, Job, StageName, StageStatus
from xvt.pipeline import (
    RebuildSourceResult,
    ResegmentationResult,
    TranslationRunResult,
    apply_agent_translation,
    apply_cue_retranslation,
    prepare_agent_translation,
    retranslate_cues,
    run_subtitle_transcription,
    run_translation_polish,
)
from xvt.pipeline.revision import (
    SOURCE_REVIEW_TASK,
    TRANSLATION_REVIEW_TASK,
    source_review_stem,
    translation_review_stem,
)
from xvt.process import CommandCancelled
from xvt.recovery import file_checksum, valid_artifact_path
from xvt.workflow import AWAIT_STEPS, WorkflowResult, _record_burn_fingerprint
from xvt.workflow_config import WorkflowConfig, set_workflow_config


class CliTest(unittest.TestCase):
    def _missing_relinkable_burned_job(self, root: Path) -> Path:
        media = root / "video.mp4"
        media.write_bytes(b"media")
        burned = root / "burned.mp4"
        burned.write_bytes(b"burned-output")
        cues = root / "translated-cues.json"
        cues.write_text("[]", encoding="utf-8")
        source_cues = root / "source-cues.json"
        source_cues.write_text("[]", encoding="utf-8")
        translated_srt = root / "translated.srt"
        translated_srt.write_text("", encoding="utf-8")
        bilingual_srt = root / "bilingual.srt"
        bilingual_srt.write_text("", encoding="utf-8")
        translated_qa = root / "qa-translated.json"
        translated_qa.write_text('{"status":"pass","issues":[]}\n', encoding="utf-8")
        media_qa = root / "media-qa.json"
        media_qa.write_text('{"status":"pass","issues":[]}\n', encoding="utf-8")
        job = create_job(
            str(media),
            root,
            {"workflow": {"target_language": "zh-CN", "burn": True}},
        )
        job.artifacts.extend(
            [
                Artifact("source_cues", str(source_cues), file_checksum(source_cues)),
                Artifact("translated_cues", str(cues), file_checksum(cues)),
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
        return job_file

    def test_batch_forwards_shared_workflow_options(self) -> None:
        output = StringIO()
        result = BatchResult("completed", Path("/tmp/batch.json"), 2, 2, 0, 0)
        with (
            patch("xvt.cli.run_batch", return_value=result) as run,
            redirect_stdout(output),
        ):
            return_code = main(
                [
                    "batch",
                    "/tmp/inputs",
                    "--output",
                    "/tmp/output",
                    "--recursive",
                    "--target-language",
                    "zh-CN",
                    "--provider",
                    "agent",
                    "--translation-quality",
                    "polished",
                    "--segmentation-preset",
                    "compact",
                    "--source-strategy",
                    "asr",
                    "--burn",
                    "--software",
                ]
            )

        options = run.call_args.args[1]
        self.assertEqual(return_code, 0)
        self.assertTrue(run.call_args.kwargs["recursive"])
        self.assertEqual(options.target_language, "zh-CN")
        self.assertEqual(options.provider, "agent")
        self.assertEqual(options.translation_quality, "polished")
        self.assertEqual(options.segmentation_preset, "compact")
        self.assertEqual(options.source_strategy, "asr")
        self.assertTrue(options.burn)
        self.assertFalse(options.hardware_encoding)
        self.assertIn("/tmp/batch.json", output.getvalue())

    def test_batch_partial_failure_returns_nonzero_without_cli_completed(self) -> None:
        output = StringIO()
        error_output = StringIO()
        result = BatchResult("partial_failure", Path("/tmp/batch.json"), 2, 1, 0, 1)

        def run_with_failure(_directory, _options, *, recursive, event_sink):
            self.assertFalse(recursive)
            event_sink(
                create_event(
                    EventType.BATCH_FINISHED,
                    batch_file="/tmp/batch.json",
                    status="partial_failure",
                    total=2,
                    completed=1,
                    awaiting_input=0,
                    failed=1,
                )
            )
            return result

        with (
            patch("xvt.cli.run_batch", side_effect=run_with_failure),
            redirect_stdout(output),
            redirect_stderr(error_output),
        ):
            return_code = main(
                [
                    "batch",
                    "/tmp/inputs",
                    "--output",
                    "/tmp/output",
                    "--events-jsonl",
                ]
            )

        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(return_code, 6)
        self.assertEqual(events[-1]["type"], "batch_finished")
        self.assertNotIn("cli_completed", {event["type"] for event in events})
        self.assertEqual(error_output.getvalue(), "")

    def test_batch_human_failure_shows_report_and_recovery_command(self) -> None:
        output = StringIO()
        error_output = StringIO()
        result = BatchResult("partial_failure", Path("/tmp/batch.json"), 1, 0, 0, 1)

        def run_with_failure(_directory, _options, *, recursive, event_sink):
            self.assertFalse(recursive)
            event_sink(create_event(EventType.BATCH_STARTED, batch_file="/tmp/batch.json", total=1))
            event_sink(
                create_event(
                    EventType.BATCH_ITEM_FAILED,
                    input="/tmp/source.srt",
                    message="subtitle QA failed",
                    recovery_plan={
                        "reason": "需要修复字幕",
                        "prerequisite_commands": [],
                        "primary_command": "xvt continue /tmp/job.json",
                    },
                )
            )
            event_sink(
                create_event(
                    EventType.BATCH_FINISHED,
                    status="partial_failure",
                    completed=0,
                    awaiting_input=0,
                    failed=1,
                    next_action="xvt batch-continue /tmp/batch.json",
                )
            )
            return result

        with (
            patch("xvt.cli.run_batch", side_effect=run_with_failure),
            redirect_stdout(output),
            redirect_stderr(error_output),
        ):
            return_code = main(["batch", "/tmp/inputs", "--output", "/tmp/output"])

        self.assertEqual(return_code, 6)
        self.assertIn("批处理文件: /tmp/batch.json", error_output.getvalue())
        self.assertIn("原因：subtitle QA failed", error_output.getvalue())
        self.assertIn("xvt continue /tmp/job.json", error_output.getvalue())
        self.assertIn("xvt batch-continue /tmp/batch.json", error_output.getvalue())
        self.assertIn("/tmp/batch.json", output.getvalue())

    def test_batch_completed_jsonl_has_terminal_cli_event(self) -> None:
        output = StringIO()
        result = BatchResult("completed", Path("/tmp/batch.json"), 1, 1, 0, 0)
        with (
            patch("xvt.cli.run_batch", return_value=result),
            redirect_stdout(output),
        ):
            return_code = main(
                [
                    "batch",
                    "/tmp/inputs",
                    "--output",
                    "/tmp/output",
                    "--events-jsonl",
                ]
            )

        event = json.loads(output.getvalue())
        self.assertEqual(return_code, 0)
        self.assertEqual(event["type"], "cli_completed")
        self.assertEqual(event["data"]["batch_file"], "/tmp/batch.json")

    def test_batch_awaiting_input_jsonl_does_not_claim_cli_completed(self) -> None:
        output = StringIO()
        result = BatchResult("awaiting_input", Path("/tmp/batch.json"), 1, 0, 1, 0)

        def run_waiting(_directory, _options, *, recursive, event_sink):
            self.assertFalse(recursive)
            event_sink(
                create_event(
                    EventType.BATCH_FINISHED,
                    batch_file="/tmp/batch.json",
                    status="awaiting_input",
                    total=1,
                    completed=0,
                    awaiting_input=1,
                    failed=0,
                )
            )
            return result

        with (
            patch("xvt.cli.run_batch", side_effect=run_waiting),
            redirect_stdout(output),
        ):
            return_code = main(
                [
                    "batch",
                    "/tmp/inputs",
                    "--output",
                    "/tmp/output",
                    "--events-jsonl",
                ]
            )

        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(return_code, 0)
        self.assertEqual(events[-1]["type"], "batch_finished")
        self.assertNotIn("cli_completed", {event["type"] for event in events})

    def test_batch_continue_forwards_manifest(self) -> None:
        output = StringIO()
        result = BatchResult("awaiting_input", Path("/tmp/batch.json"), 2, 1, 1, 0)
        with (
            patch("xvt.cli.continue_batch", return_value=result) as resume,
            redirect_stdout(output),
        ):
            return_code = main(["batch-continue", "/tmp/batch.json"])

        self.assertEqual(return_code, 0)
        resume.assert_called_once()
        self.assertEqual(resume.call_args.args[0], "/tmp/batch.json")
        self.assertIn("1 awaiting input", output.getvalue())
        self.assertIn("xvt batch-continue", output.getvalue())

    def test_batch_interrupt_before_job_creation_shows_batch_recovery(self) -> None:
        error_output = StringIO()

        def interrupt(_directory, _options, *, recursive, event_sink):
            self.assertFalse(recursive)
            event_sink(create_event(EventType.BATCH_STARTED, batch_file="/tmp/batch.json", total=1))
            raise KeyboardInterrupt

        with (
            patch("xvt.cli.run_batch", side_effect=interrupt),
            redirect_stderr(error_output),
        ):
            return_code = main(["batch", "/tmp/inputs", "--output", "/tmp/output"])

        self.assertEqual(return_code, 130)
        self.assertIn("批处理文件: /tmp/batch.json", error_output.getvalue())
        self.assertIn("xvt batch-continue /tmp/batch.json", error_output.getvalue())
        self.assertNotIn("任务尚未创建", error_output.getvalue())

    def test_batch_continue_early_failure_keeps_manifest_path(self) -> None:
        error_output = StringIO()
        with (
            patch("xvt.cli.continue_batch", side_effect=ValueError("invalid batch file")),
            redirect_stderr(error_output),
        ):
            return_code = main(["batch-continue", "/tmp/batch.json"])

        self.assertEqual(return_code, 2)
        self.assertIn(
            f"批处理文件: {Path('/tmp/batch.json').resolve()}",
            error_output.getvalue(),
        )
        self.assertIn("请修正批处理文件或路径", error_output.getvalue())
        self.assertNotIn("任务尚未创建", error_output.getvalue())

    def test_preview_forwards_clip_and_render_options(self) -> None:
        output = StringIO()
        with (
            patch(
                "xvt.cli.preview_translated_job", return_value=Path("/tmp/preview.mp4")
            ) as preview,
            redirect_stdout(output),
        ):
            return_code = main(
                [
                    "preview",
                    "/tmp/job.json",
                    "--cue",
                    "18",
                    "--duration",
                    "12.5",
                    "--media",
                    "/tmp/input.mp4",
                    "--output",
                    "/tmp/custom-preview.mp4",
                    "--software",
                    "--target-font",
                    "Target Font",
                    "--source-font",
                    "Source Font",
                ]
            )

        self.assertEqual(return_code, 0)
        self.assertEqual(preview.call_args.kwargs["cue_id"], 18)
        self.assertEqual(preview.call_args.kwargs["duration_seconds"], 12.5)
        self.assertEqual(preview.call_args.kwargs["media"], "/tmp/input.mp4")
        self.assertFalse(preview.call_args.kwargs["hardware_encoding"])
        self.assertEqual(preview.call_args.kwargs["target_font"], "Target Font")
        self.assertEqual(output.getvalue(), "/tmp/preview.mp4\n")

    def test_preview_interrupt_has_preview_specific_recovery_message(self) -> None:
        error_output = StringIO()
        with (
            patch("xvt.cli.preview_translated_job", side_effect=KeyboardInterrupt),
            redirect_stderr(error_output),
        ):
            return_code = main(["preview", "/tmp/job.json"])

        self.assertEqual(return_code, 130)
        failure = error_output.getvalue()
        self.assertIn("预览已中断", failure)
        self.assertIn("重新执行原 preview 命令", failure)
        self.assertNotIn("xvt verify", failure)

    def test_preview_leaves_encoding_unspecified_without_software_flag(self) -> None:
        with patch(
            "xvt.cli.preview_translated_job", return_value=Path("/tmp/preview.mp4")
        ) as preview:
            return_code = main(["preview", "/tmp/job.json"])

        self.assertEqual(return_code, 0)
        self.assertIsNone(preview.call_args.kwargs["hardware_encoding"])

    def test_jsonl_preview_interrupt_has_no_workflow_recovery_plan(self) -> None:
        output = StringIO()
        error_output = StringIO()
        with (
            patch("xvt.cli.preview_translated_job", side_effect=KeyboardInterrupt),
            redirect_stdout(output),
            redirect_stderr(error_output),
        ):
            return_code = main(["preview", "/tmp/job.json", "--events-jsonl"])

        event = json.loads(output.getvalue())
        self.assertEqual(return_code, 130)
        self.assertEqual(event["type"], "cli_failed")
        self.assertNotIn("recovery_plan", event["data"])
        self.assertEqual(event["data"]["next_action"], "rerun_preview")
        self.assertEqual(error_output.getvalue(), "error: interrupted by user\n")

    def test_preview_missing_artifacts_exits_with_workflow_error(self) -> None:
        """契约锁定：preview 缺产物是 Job 状态问题，不是用户输入错误。

        RuntimeError → category `workflow` → 退出码 4（此前为 ValueError → 2）。
        """
        output = StringIO()
        error_output = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            job = create_job(str(media), root)
            (root / "jobs" / job.id / "artifacts").mkdir(parents=True, exist_ok=True)
            job_file = save_job(job)
            with redirect_stdout(output), redirect_stderr(error_output):
                return_code = main(["preview", str(job_file), "--events-jsonl"])

        self.assertEqual(return_code, 4)
        streams = output.getvalue().splitlines() + error_output.getvalue().splitlines()
        events = [json.loads(line) for line in streams if line.startswith("{")]
        failure = next(event for event in events if event["type"] == "cli_failed")
        self.assertEqual(failure["data"]["error"]["category"], "workflow")
        self.assertIn(
            "requires checksum-valid translated subtitles",
            failure["data"]["error"]["message"],
        )

    def test_resegment_translation_shows_continue_command_for_workflow_jobs(self) -> None:
        output = StringIO()
        job = Job("job-1", "", "", "input", "/tmp", {"workflow": {}})
        result = ResegmentationResult(True, Path("/tmp/source-cues.json"), Path("/tmp/report.json"))

        with (
            patch("xvt.cli.resegment_translation", return_value=result),
            patch("xvt.cli.load_job", return_value=job),
            redirect_stdout(output),
        ):
            return_code = main(["resegment-translation", "/tmp/job.json"])

        self.assertEqual(return_code, 0)
        self.assertIn('xvt continue "/tmp/job.json"', output.getvalue())

    def test_rebuild_source_quotes_follow_up_command_arguments(self) -> None:
        output = StringIO()
        result = RebuildSourceResult(
            Path("/tmp/source-cues.json"),
            Path("/tmp/source.srt"),
            Path("/tmp/qa.json"),
            Path("/tmp/source-selection.json"),
        )
        unsafe_job_file = "/tmp/$(touch xvt-unsafe)/job.json"
        job = Job(
            "job-1",
            "",
            "",
            "input",
            "/tmp",
            {"target_language": "zh-CN; touch xvt-unsafe"},
        )
        with (
            patch("xvt.cli.hold_job_lock"),
            patch("xvt.cli.rebuild_source", return_value=result),
            patch("xvt.cli.load_job", return_value=job),
            redirect_stdout(output),
        ):
            return_code = main(["rebuild-source", unsafe_job_file, "--strategy", "asr"])

        command = next(
            line.removeprefix("继续翻译: ")
            for line in output.getvalue().splitlines()
            if line.startswith("继续翻译: ")
        )
        self.assertEqual(return_code, 0)
        self.assertEqual(
            shlex.split(command),
            [
                "xvt",
                "translate",
                str(Path(unsafe_job_file).resolve()),
                "--target-language",
                "zh-CN; touch xvt-unsafe",
            ],
        )

    def test_retranslate_cues_forwards_requested_ids(self) -> None:
        output = StringIO()
        result = TranslationRunResult("agent", (Path("/tmp/cue-retranslation-batches.json"),))
        with (
            patch("xvt.cli.retranslate_cues", return_value=result) as retranslate,
            redirect_stdout(output),
        ):
            return_code = main(
                [
                    "retranslate-cues",
                    "/tmp/job.json",
                    "--cue",
                    "3",
                    "--cue",
                    "7",
                    "--target-language",
                    "zh-CN",
                    "--provider",
                    "agent",
                ]
            )

        self.assertEqual(return_code, 0)
        self.assertEqual(retranslate.call_args.kwargs["cue_ids"], (3, 7))
        self.assertIn("mode: agent", output.getvalue())
        self.assertIn("apply-cue-retranslation", output.getvalue())

    def test_polish_translation_preserves_persisted_provider_defaults(self) -> None:
        output = StringIO()
        result = TranslationRunResult("agent", (Path("/tmp/polish-batches.json"),))
        with (
            patch("xvt.cli.run_translation_polish", return_value=result) as polish,
            redirect_stdout(output),
        ):
            return_code = main(["polish-translation", "/tmp/job.json"])

        self.assertEqual(return_code, 0)
        self.assertIsNone(polish.call_args.kwargs["provider"])
        self.assertIsNone(polish.call_args.kwargs["batch_size"])
        self.assertIn("mode: agent", output.getvalue())

    def test_apply_polish_translation_forwards_response(self) -> None:
        paths = (Path("/tmp/translated.srt"), Path("/tmp/bilingual.srt"))
        with patch("xvt.cli.apply_agent_translation_polish", return_value=paths) as apply:
            return_code = main(["apply-polish-translation", "/tmp/job.json", "/tmp/response.json"])

        self.assertEqual(return_code, 0)
        self.assertEqual(apply.call_args.args[:2], ("/tmp/job.json", "/tmp/response.json"))

    def test_apply_translation_review_fixes_forwards_response_and_prints_status(self) -> None:
        paths = (Path("/tmp/revision.json"), Path("/tmp/reviewed-cues.json"))
        output = StringIO()
        with (
            patch("xvt.cli.apply_translation_review_fixes", return_value=paths) as apply,
            redirect_stdout(output),
        ):
            return_code = main(
                [
                    "apply-translation-review-fixes",
                    "/tmp/job.json",
                    "/tmp/response.json",
                ]
            )

        self.assertEqual(return_code, 0)
        self.assertEqual(apply.call_args.args[:2], ("/tmp/job.json", "/tmp/response.json"))
        self.assertIn("xvt status /tmp/job.json", output.getvalue())

    def test_prepare_source_review_forwards_cues_and_prints_apply_command(self) -> None:
        path = Path("/tmp/source-review.json")
        output = StringIO()
        with (
            patch("xvt.cli.prepare_source_review", return_value=path) as prepare,
            redirect_stdout(output),
        ):
            return_code = main(
                ["prepare-source-review", "/tmp/job.json", "--cue", "3", "--cue", "5"]
            )

        self.assertEqual(return_code, 0)
        self.assertEqual(prepare.call_args.args[:2], ("/tmp/job.json", [3, 5]))
        self.assertIn("/tmp/source-review.srt", output.getvalue())
        self.assertIn("apply-source-review-fixes", output.getvalue())

    def test_apply_source_review_fixes_forwards_response_and_prints_status(self) -> None:
        paths = (Path("/tmp/revision.json"), Path("/tmp/reviewed-cues.json"))
        output = StringIO()
        with (
            patch("xvt.cli.apply_source_review_fixes", return_value=paths) as apply,
            redirect_stdout(output),
        ):
            return_code = main(["apply-source-review-fixes", "/tmp/job.json", "/tmp/response.json"])

        self.assertEqual(return_code, 0)
        self.assertEqual(apply.call_args.args[:2], ("/tmp/job.json", "/tmp/response.json"))
        self.assertIn("xvt status /tmp/job.json", output.getvalue())

    def _write_response(self, directory: str, payload: dict[str, object]) -> str:
        path = Path(directory) / "response.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def test_apply_routes_source_review_fixes_by_task(self) -> None:
        paths = (Path("/tmp/revision.json"),)
        with tempfile.TemporaryDirectory() as directory:
            response = self._write_response(directory, {"task": "source_review_fixes"})
            with (
                patch("xvt.cli.apply_source_review_fixes", return_value=paths) as apply,
                redirect_stdout(StringIO()),
            ):
                return_code = main(["apply", "/tmp/job.json", response])

        self.assertEqual(return_code, 0)
        self.assertEqual(apply.call_args.args[:2], ("/tmp/job.json", response))

    def test_apply_routes_translation_review_fixes_by_task(self) -> None:
        paths = (Path("/tmp/revision.json"),)
        with tempfile.TemporaryDirectory() as directory:
            response = self._write_response(directory, {"task": "translation_review_fixes"})
            with (
                patch("xvt.cli.apply_translation_review_fixes", return_value=paths) as apply,
                redirect_stdout(StringIO()),
            ):
                return_code = main(["apply", "/tmp/job.json", response])

        self.assertEqual(return_code, 0)
        self.assertEqual(apply.call_args.args[:2], ("/tmp/job.json", response))

    def test_apply_routes_semantic_review_by_task(self) -> None:
        result = SimpleNamespace(status="completed", paths=(Path("/tmp/reviewed.json"),))
        with tempfile.TemporaryDirectory() as directory:
            response = self._write_response(directory, {"task": "semantic_review_and_repair"})
            with (
                patch("xvt.cli.apply_agent_semantic_review", return_value=result) as apply,
                redirect_stdout(StringIO()),
            ):
                return_code = main(["apply", "/tmp/job.json", response])

        self.assertEqual(return_code, 0)
        self.assertEqual(apply.call_args.args[:2], ("/tmp/job.json", response))

    def test_apply_continue_runs_workflow_after_agent_response(self) -> None:
        result = SimpleNamespace(status="completed", paths=(Path("/tmp/reviewed.json"),))
        workflow_result = WorkflowResult("completed", Path("/tmp/job.json"), (), None)
        with tempfile.TemporaryDirectory() as directory:
            response = self._write_response(directory, {"task": "semantic_review_only"})
            with (
                patch("xvt.cli.apply_agent_semantic_review", return_value=result),
                patch("xvt.cli.load_job", return_value=Job("job-1", "", "", "input", "/tmp", {})),
                patch("xvt.cli.get_workflow_config", return_value=SimpleNamespace()),
                patch(
                    "xvt.cli.continue_workflow", return_value=workflow_result
                ) as continue_workflow,
                redirect_stdout(StringIO()),
            ):
                return_code = main(["apply", "/tmp/job.json", response, "--continue"])

        self.assertEqual(return_code, 0)
        continue_workflow.assert_called_once()

    def test_apply_continue_suppresses_intermediate_recovery_summary(self) -> None:
        paths = (Path("/tmp/revision.json"), Path("/tmp/reviewed.json"))
        workflow_result = WorkflowResult("completed", Path("/tmp/job.json"), (), None)
        output = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            response = self._write_response(directory, {"task": "translation_review_fixes"})
            with (
                patch("xvt.cli.apply_translation_review_fixes", return_value=paths),
                patch("xvt.cli.load_job", return_value=Job("job-1", "", "", "input", "/tmp", {})),
                patch("xvt.cli.get_workflow_config", return_value=SimpleNamespace()),
                patch("xvt.cli.continue_workflow", return_value=workflow_result),
                patch("xvt.cli._print_recovery_navigation") as recovery,
                redirect_stdout(output),
            ):
                return_code = main(["apply", "/tmp/job.json", response, "--continue"])

        self.assertEqual(return_code, 0)
        self.assertEqual(output.getvalue(), "/tmp/job.json\n")
        recovery.assert_not_called()

    def test_apply_continue_jsonl_contains_only_json_events(self) -> None:
        result = SimpleNamespace(status="completed", paths=(Path("/tmp/reviewed.json"),))
        workflow_result = WorkflowResult("completed", Path("/tmp/job.json"), (), None)
        output = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            response = self._write_response(directory, {"task": "semantic_review_only"})
            with (
                patch("xvt.cli.apply_agent_semantic_review", return_value=result),
                patch("xvt.cli.load_job", return_value=Job("job-1", "", "", "input", "/tmp", {})),
                patch("xvt.cli.get_workflow_config", return_value=SimpleNamespace()),
                patch("xvt.cli.continue_workflow", return_value=workflow_result),
                redirect_stdout(output),
            ):
                return_code = main(
                    [
                        "apply",
                        "/tmp/job.json",
                        response,
                        "--continue",
                        "--events-jsonl",
                    ]
                )

        lines = [line for line in output.getvalue().splitlines() if line]
        events = [json.loads(line) for line in lines]
        self.assertEqual(return_code, 0)
        self.assertTrue(events)
        self.assertEqual(events[-1]["type"], "cli_completed")
        self.assertNotIn("/tmp/reviewed.json", lines)

    def test_apply_continue_rejects_source_review(self) -> None:
        errors = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            response = self._write_response(directory, {"task": "source_review_fixes"})
            with redirect_stderr(errors):
                return_code = main(["apply", "/tmp/job.json", response, "--continue"])

        self.assertEqual(return_code, 2)
        self.assertIn("invalidate translation and downstream", errors.getvalue())

    def test_apply_routes_polished_translation_and_resolves_target_language(self) -> None:
        job = Job("job-1", "", "", "input", "/tmp", {})
        job.config["target_language"] = "zh"
        paths = (Path("/tmp/translated.json"),)
        with tempfile.TemporaryDirectory() as directory:
            response = self._write_response(directory, {"task": "translate_and_polish"})
            with (
                patch("xvt.cli.apply_agent_translation", return_value=paths) as apply,
                patch("xvt.cli.load_job", return_value=job),
                redirect_stdout(StringIO()),
            ):
                return_code = main(["apply", "/tmp/job.json", response])

        self.assertEqual(return_code, 0)
        self.assertEqual(apply.call_args.kwargs["target_language"], "zh")

    def test_apply_routes_by_advance_step_when_response_has_no_task(self) -> None:
        job = Job("job-1", "", "", "input", "/tmp", {})
        paths = (Path("/tmp/polished.json"),)
        with tempfile.TemporaryDirectory() as directory:
            response = self._write_response(directory, {"fingerprint": "abc", "items": []})
            with (
                patch("xvt.cli.apply_agent_translation_polish", return_value=paths) as apply,
                patch("xvt.cli.load_job", return_value=job),
                patch("xvt.cli.get_workflow_config", return_value=SimpleNamespace()),
                patch("xvt.cli.plan_advance_steps", return_value=("await_agent_polish",)),
                redirect_stdout(StringIO()),
            ):
                return_code = main(["apply", "/tmp/job.json", response])

        self.assertEqual(return_code, 0)
        self.assertEqual(apply.call_args.args[:2], ("/tmp/job.json", response))

    def test_apply_rejects_unknown_task(self) -> None:
        errors = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            response = self._write_response(directory, {"task": "not_a_real_task"})
            with redirect_stderr(errors):
                return_code = main(["apply", "/tmp/job.json", response])

        self.assertNotEqual(return_code, 0)
        self.assertIn("not_a_real_task", errors.getvalue())

    def test_apply_points_retranslate_cues_responses_at_the_manual_command(self) -> None:
        """retranslate_cues 有真实响应但不可自动路由，报错必须给出可复制的手工命令。"""
        errors = StringIO()
        events = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            response = self._write_response(
                directory,
                {"task": "retranslate_cues", "fingerprint": "abc", "items": []},
            )
            with redirect_stderr(errors), redirect_stdout(events):
                return_code = main(["apply", "/tmp/job.json", response, "--events-jsonl"])

        message = errors.getvalue()
        event = json.loads(events.getvalue())
        self.assertEqual(return_code, 2)
        self.assertEqual(event["type"], "cli_failed")
        self.assertEqual(event["data"]["error"]["category"], "invalid_input")
        self.assertIn("xvt apply-cue-retranslation", message)
        self.assertIn("--cue", message)
        self.assertIn("--target-language", message)
        self.assertIn("cue-retranslation-batches.json", message)
        self.assertNotIn("unknown task", message)

    def test_apply_guides_task_less_cue_retranslation_responses(self) -> None:
        """协议一致的重译响应只有 fingerprint + items，没有 task：只能靠 Job 状态识别。

        此前它落到步骤路由回退，被报成「job is not awaiting Agent input」——而这个 Job 的
        TRANSLATE stage 明明是 awaiting_input，用户按提示去看 status 又被告知已完成，死环。
        """

        errors = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            source_path = artifacts / "source-cues.json"
            source_path.write_text(
                json.dumps(
                    [
                        {"id": 1, "start_ms": 0, "end_ms": 1000, "source_text": "First"},
                        {"id": 2, "start_ms": 1000, "end_ms": 2000, "source_text": "Second"},
                    ]
                ),
                encoding="utf-8",
            )
            translated_path = artifacts / "translated-cues.json"
            translated_path.write_text(
                json.dumps(
                    [
                        {
                            "id": 1,
                            "start_ms": 0,
                            "end_ms": 1000,
                            "source_text": "First",
                            "target_text": "旧译文一",
                            "target_language": "zh-CN",
                        },
                        {
                            "id": 2,
                            "start_ms": 1000,
                            "end_ms": 2000,
                            "source_text": "Second",
                            "target_text": "旧译文二",
                            "target_language": "zh-CN",
                        },
                    ]
                ),
                encoding="utf-8",
            )
            # 必须带 checksum：没有 checksum 的产物在 valid_artifact_path 里算不上有效，
            # 计划会退化成「重新翻译」，测不到本用例要盯的步骤路由回退。
            job.artifacts.extend(
                [
                    Artifact("source_cues", str(source_path), file_checksum(source_path)),
                    Artifact(
                        "translated_cues", str(translated_path), file_checksum(translated_path)
                    ),
                ]
            )
            # 端到端 Job（有 workflow 目标）才会走到步骤路由回退，正是报「not awaiting」的那条路。
            job.config["workflow"] = {"target_language": "zh-CN"}
            job_file = save_job(job)
            manifest = retranslate_cues(
                job_file,
                cue_ids=(1,),
                target_language="zh-CN",
                provider="agent",
            ).paths[0]
            fingerprint = json.loads(manifest.read_text(encoding="utf-8"))["fingerprint"]
            persisted = json.loads(Path(job_file).read_text(encoding="utf-8"))
            self.assertEqual(persisted["stages"]["translate"]["status"], "awaiting_input")

            response = self._write_response(
                directory,
                {"fingerprint": fingerprint, "items": [{"id": 1, "text": "新译文一"}]},
            )
            with redirect_stderr(errors):
                return_code = main(["apply", str(job_file), response])

        message = errors.getvalue()
        self.assertEqual(return_code, 2)
        self.assertIn("xvt apply-cue-retranslation", message)
        self.assertIn("--cue", message)
        self.assertIn("--target-language", message)
        self.assertNotIn("not awaiting Agent input", message)

    def test_apply_routes_polish_response_despite_a_stale_retranslation_manifest(self) -> None:
        """回归修复：重译回填后再进入润色等待，残留 manifest 不得顶掉合法的润色回填。

        ``cue_retranslation_batches`` 只派生自 ``source_cues``，译文被改写时不会失效，
        因此回填后它仍然 checksum 有效。此前 `xvt apply` 只看「stage 在等输入 + manifest
        有效」，会拒绝一条合法的润色响应并指向 apply-cue-retranslation，而同一时刻
        `status` 说「等待润色回填，用 xvt apply」——两者互相矛盾。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello.\n",
                encoding="utf-8",
            )
            job = run_subtitle_transcription(source, root, language="en")
            job_file = get_job_file(job)
            set_workflow_config(
                job,
                WorkflowConfig(target_language="zh-CN", provider="agent", batch_size=1),
            )
            save_job(job)
            prepare_agent_translation(
                job_file,
                source_language="en",
                target_language="zh-CN",
                await_input=False,
            )
            apply_agent_translation(
                job_file,
                self._write_response(directory, {"items": [{"id": 1, "text": "译文一"}]}),
                target_language="zh-CN",
            )
            manifest_path = retranslate_cues(
                job_file,
                cue_ids=(1,),
                target_language="zh-CN",
                provider="agent",
            ).paths[0]
            fingerprint = json.loads(manifest_path.read_text(encoding="utf-8"))["fingerprint"]
            cue_response = root / "cue-response.json"
            cue_response.write_text(
                json.dumps({"fingerprint": fingerprint, "items": [{"id": 1, "text": "重译一"}]}),
                encoding="utf-8",
            )
            apply_cue_retranslation(job_file, cue_response, cue_ids=(1,), target_language="zh-CN")
            polish_manifest = run_translation_polish(job_file).paths[0]
            polish_fingerprint = json.loads(polish_manifest.read_text(encoding="utf-8"))[
                "fingerprint"
            ]
            polish_response = root / "polish-response.json"
            polish_response.write_text(
                json.dumps(
                    {
                        "fingerprint": polish_fingerprint,
                        "items": [{"id": 1, "text": "润色一"}],
                    }
                ),
                encoding="utf-8",
            )
            # 前提：残留 manifest 仍然 checksum 有效，否则这条用例测不到 E5。
            self.assertIsNotNone(
                valid_artifact_path(load_job(job_file), "cue_retranslation_batches")
            )

            with redirect_stdout(StringIO()):
                return_code = main(["apply", str(job_file), str(polish_response)])

            self.assertEqual(return_code, 0)
            self.assertIn(
                "translation_polished_cues",
                {artifact.kind for artifact in load_job(job_file).artifacts},
            )

    def test_apply_reports_the_invoked_command_name_in_events(self) -> None:
        """成功与失败必须报同一个子命令名，且是用户实际执行的 `apply`。"""
        paths = (Path("/tmp/revision.json"),)
        completed = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            response = self._write_response(directory, {"task": "source_review_fixes"})
            with (
                patch("xvt.cli.apply_source_review_fixes", return_value=paths),
                redirect_stdout(completed),
            ):
                main(["apply", "/tmp/job.json", response, "--events-jsonl"])

            failed = StringIO()
            with (
                patch(
                    "xvt.cli.apply_source_review_fixes",
                    side_effect=ValueError("bad revision"),
                ),
                redirect_stdout(failed),
                redirect_stderr(StringIO()),
            ):
                main(["apply", "/tmp/job.json", response, "--events-jsonl"])

        success_event = json.loads(completed.getvalue())
        failure_event = json.loads(failed.getvalue())
        self.assertEqual(success_event["type"], "cli_completed")
        self.assertEqual(failure_event["type"], "cli_failed")
        self.assertEqual(success_event["data"]["command"], "apply")
        self.assertEqual(failure_event["data"]["command"], "apply")

    def test_apply_rejects_atomic_job_without_workflow_goal(self) -> None:
        job = Job("job-1", "", "", "input", "/tmp", {})
        errors = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            response = self._write_response(directory, {"items": []})
            with (
                patch("xvt.cli.load_job", return_value=job),
                patch("xvt.cli.get_workflow_config", return_value=None),
                redirect_stderr(errors),
            ):
                return_code = main(["apply", "/tmp/job.json", response])

        self.assertNotEqual(return_code, 0)
        self.assertIn("no persisted workflow goal", errors.getvalue())

    def test_apply_continue_rejects_atomic_review_before_mutation(self) -> None:
        job = Job("job-1", "", "", "input", "/tmp", {})
        errors = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            response = self._write_response(directory, {"task": "translation_review_fixes"})
            with (
                patch("xvt.cli.load_job", return_value=job),
                patch("xvt.cli.get_workflow_config", return_value=None),
                patch("xvt.cli.apply_translation_review_fixes") as apply,
                redirect_stderr(errors),
            ):
                return_code = main(["apply", "/tmp/job.json", response, "--continue"])

        self.assertEqual(return_code, 2)
        self.assertIn("persisted end-to-end workflow goal", errors.getvalue())
        apply.assert_not_called()

    def test_apply_rejects_when_job_is_not_awaiting_input(self) -> None:
        job = Job("job-1", "", "", "input", "/tmp", {"workflow": {}})
        errors = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            response = self._write_response(directory, {"items": []})
            with (
                patch("xvt.cli.load_job", return_value=job),
                patch("xvt.cli.get_workflow_config", return_value=SimpleNamespace()),
                patch("xvt.cli.plan_advance_steps", return_value=("burn",)),
                redirect_stderr(errors),
            ):
                return_code = main(["apply", "/tmp/job.json", response])

        self.assertNotEqual(return_code, 0)
        self.assertIn("not awaiting Agent input", errors.getvalue())

    def test_apply_reports_unreadable_response_as_user_input_error(self) -> None:
        """缺文件 / 喂字幕都是用户输入错误，不能落到 dependency_or_process。"""
        errors = StringIO()
        with redirect_stderr(errors):
            missing_code = main(["apply", "/tmp/job.json", "/tmp/does-not-exist.json"])
        self.assertEqual(missing_code, 2)

        subtitle_errors = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            subtitle = Path(directory) / "translated.srt"
            subtitle.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n你好。\n",
                encoding="utf-8",
            )
            with redirect_stderr(subtitle_errors):
                return_code = main(["apply", "/tmp/job.json", str(subtitle)])

        self.assertEqual(return_code, 2)
        self.assertIn("import-translation", subtitle_errors.getvalue())

    def test_apply_routes_review_srt_by_its_generated_name(self) -> None:
        """人工修订 SRT 是修订通道的载体，必须按固定文件名路由而不是当译文字幕拒掉。"""
        paths = (Path("/tmp/revision.json"), Path("/tmp/reviewed.json"))
        with tempfile.TemporaryDirectory() as directory:
            stem = translation_review_stem("0123456789abcdef0123456789abcdef")
            review = Path(directory) / f"{stem}.srt"
            review.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n#1\n你好，朋友。\n",
                encoding="utf-8",
            )
            with (
                patch("xvt.cli.apply_translation_review_fixes", return_value=paths) as apply,
                redirect_stdout(StringIO()),
            ):
                return_code = main(["apply", "/tmp/job.json", str(review)])

        self.assertEqual(return_code, 0)
        self.assertEqual(apply.call_args.args[:2], ("/tmp/job.json", str(review)))

    def test_apply_review_srt_continue_runs_workflow(self) -> None:
        paths = (Path("/tmp/revision.json"), Path("/tmp/reviewed.json"))
        workflow_result = WorkflowResult("completed", Path("/tmp/job.json"), (), None)
        with tempfile.TemporaryDirectory() as directory:
            stem = translation_review_stem("0123456789abcdef0123456789abcdef")
            review = Path(directory) / f"{stem}.srt"
            review.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n#1\n你好，朋友。\n",
                encoding="utf-8",
            )
            with (
                patch("xvt.cli.apply_translation_review_fixes", return_value=paths),
                patch("xvt.cli.load_job", return_value=Job("job-1", "", "", "input", "/tmp", {})),
                patch("xvt.cli.get_workflow_config", return_value=SimpleNamespace()),
                patch("xvt.cli.continue_workflow", return_value=workflow_result) as continue_job,
                redirect_stdout(StringIO()),
            ):
                return_code = main(["apply", "/tmp/job.json", str(review), "--continue"])

        self.assertEqual(return_code, 0)
        continue_job.assert_called_once()

    def test_apply_routes_source_review_srt_by_its_generated_name(self) -> None:
        paths = (Path("/tmp/revision.json"), Path("/tmp/reviewed.json"))
        with tempfile.TemporaryDirectory() as directory:
            stem = source_review_stem("0123456789abcdef0123456789abcdef")
            review = Path(directory) / f"{stem}.srt"
            review.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n#1\nHello.\n",
                encoding="utf-8",
            )
            with (
                patch("xvt.cli.apply_source_review_fixes", return_value=paths) as apply,
                redirect_stdout(StringIO()),
            ):
                return_code = main(["apply", "/tmp/job.json", str(review)])

        self.assertEqual(return_code, 0)
        self.assertEqual(apply.call_args.args[:2], ("/tmp/job.json", str(review)))

    def test_apply_routes_real_agent_translation_job_end_to_end(self) -> None:
        """不 mock 任何路由/实现，验证 _STEP_ROUTES 的 key 与真实 planner 对得上。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello.\n"
                "\n2\n00:00:01,000 --> 00:00:02,000\nGoodbye.\n",
                encoding="utf-8",
            )
            job = run_subtitle_transcription(source, root, language="en")
            set_workflow_config(
                job,
                WorkflowConfig(target_language="zh-CN"),
            )
            save_job(job)
            job_file = str(get_job_file(job))
            manifest_path = prepare_agent_translation(
                job_file,
                source_language="en",
                target_language="zh-CN",
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            cue_ids = [item["id"] for batch in manifest["batches"] for item in batch["items"]]
            self.assertTrue(cue_ids)
            response = self._write_response(
                directory,
                {"items": [{"id": cue_id, "text": f"译文 {cue_id}"} for cue_id in cue_ids]},
            )

            with redirect_stdout(StringIO()):
                return_code = main(["apply", job_file, response])

            self.assertEqual(return_code, 0)
            applied = load_job(job_file)
            self.assertIn("translated_cues", {artifact.kind for artifact in applied.artifacts})

    def test_step_routes_keys_match_workflow_await_steps(self) -> None:
        """路由表的 key 必须与真实实现同源，避免改名后静默失配。

        不可自动路由的等待步骤必须显式登记在 ``_UNROUTABLE_AWAIT_STEPS`` 里
        （apply-cue-retranslation 需要 xvt apply 拿不到的 --cue / --target-language），
        新增等待步骤时必须在两张表之一里表态，否则这条断言会红。
        """
        self.assertEqual(set(_STEP_ROUTES), set(AWAIT_STEPS) - _UNROUTABLE_AWAIT_STEPS)
        self.assertLessEqual(_UNROUTABLE_AWAIT_STEPS, AWAIT_STEPS)
        self.assertFalse(_UNROUTABLE_AWAIT_STEPS & set(_STEP_ROUTES))
        self.assertIn(SOURCE_REVIEW_TASK, _TASK_ROUTES)
        self.assertIn(TRANSLATION_REVIEW_TASK, _TASK_ROUTES)

    def test_review_srt_pattern_matches_generated_stem(self) -> None:
        """统一入口的形状正则必须认得模板真实写出的文件名，改名不能静默失配。"""
        fingerprint = "0123456789abcdef0123456789abcdef"
        source_stem = source_review_stem(fingerprint)
        translation_stem = translation_review_stem(fingerprint)
        self.assertIsNotNone(_SOURCE_REVIEW_SRT_NAME.match(source_stem))
        self.assertIsNone(_SOURCE_REVIEW_SRT_NAME.match(f"{source_stem}-extra"))
        self.assertIsNotNone(_TRANSLATION_REVIEW_SRT_NAME.match(translation_stem))
        self.assertIsNone(_TRANSLATION_REVIEW_SRT_NAME.match(f"{translation_stem}-extra"))

    def test_inspect_prints_json(self) -> None:
        output = StringIO()
        with (
            patch("xvt.cli.inspect_input", return_value={"kind": "local", "duration_ms": 1000}),
            redirect_stdout(output),
        ):
            return_code = main(["inspect", "/tmp/video.mp4", "--json"])

        self.assertEqual(return_code, 0)
        self.assertEqual(json.loads(output.getvalue())["duration_ms"], 1000)

    def test_root_help_describes_default_path_and_advanced_commands(self) -> None:
        output = StringIO()
        error_output = StringIO()
        with redirect_stdout(output), redirect_stderr(error_output):
            with self.assertRaises(SystemExit) as raised:
                main(["--help"])

        self.assertEqual(raised.exception.code, 0)
        help_text = output.getvalue() + error_output.getvalue()
        self.assertIn("run → status → continue", help_text)
        self.assertIn("recommended path:", help_text)
        self.assertIn("--translation-quality standard|polished", help_text)
        self.assertIn("[advanced]", help_text)
        self.assertIn("resume ≠ continue", help_text)
        normalized_help = " ".join(help_text.split())
        self.assertIn("re-run source QA, then inspect status", normalized_help)

    def test_status_reads_job_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job("/tmp/input.mp4", directory)
            output = StringIO()
            with redirect_stdout(output):
                return_code = main(["status", str(get_job_file(job)), "--json"])

        self.assertEqual(return_code, 0)
        status = json.loads(output.getvalue())
        self.assertEqual(status["id"], job.id)
        self.assertEqual(status["recovery_plan"]["category"], "unrecoverable")
        self.assertIsNone(status["recovery_plan"]["primary_command"])

    def test_status_rejects_malformed_job_without_traceback(self) -> None:
        output = StringIO()
        error_output = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            job_file = Path(directory) / "job.json"
            job_file.write_text('{"schema_version": 1}', encoding="utf-8")
            with redirect_stdout(output), redirect_stderr(error_output):
                return_code = main(["status", str(job_file), "--json"])

        self.assertEqual(return_code, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("Invalid job file: missing fields", error_output.getvalue())
        self.assertNotIn("Traceback", error_output.getvalue())

    def test_status_uses_persisted_workflow_goal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job("/tmp/input.mp4", directory)
            job.config["workflow"] = {"target_language": "zh-CN", "burn": True}
            save_job(job)
            output = StringIO()
            with redirect_stdout(output):
                return_code = main(["status", str(get_job_file(job)), "--json"])

        self.assertEqual(return_code, 0)
        status = json.loads(output.getvalue())
        self.assertEqual(status["recovery_plan"]["category"], "unrecoverable")
        self.assertNotIn("xvt continue", status["next_action"])

    def test_status_exposes_relink_navigation_in_plain_and_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._missing_relinkable_burned_job(Path(directory))
            plain_output = StringIO()
            with redirect_stdout(plain_output):
                plain_code = main(["status", str(job_file)])
            json_output = StringIO()
            with redirect_stdout(json_output):
                json_code = main(["status", str(job_file), "--json"])

        self.assertEqual(plain_code, 0)
        self.assertIn("awaiting_input", plain_output.getvalue())
        self.assertIn("relink-burned-output", plain_output.getvalue())
        self.assertIn("EXISTING_VIDEO", plain_output.getvalue())
        self.assertIn("alternative: xvt continue", plain_output.getvalue())
        self.assertLess(
            plain_output.getvalue().index("next: xvt relink-burned-output"),
            plain_output.getvalue().index("alternative: xvt continue"),
        )
        self.assertEqual(json_code, 0)
        status = json.loads(json_output.getvalue())
        plan = status["recovery_plan"]
        self.assertEqual(plan["category"], "awaiting_input")
        self.assertIn("relink-burned-output", plan["primary_command"])
        self.assertEqual(plan["advance_steps"], ["burn"])
        self.assertEqual(status["next_action"], plan["primary_command"])

    def test_verify_json_returns_verify_failed_exit_code_for_invalid_artifact(self) -> None:
        output = StringIO()
        report = {"status": "fail", "artifacts": []}
        with (
            patch("xvt.cli.verify_job", return_value=report),
            redirect_stdout(output),
        ):
            return_code = main(["verify", "/tmp/job.json", "--json"])

        self.assertEqual(return_code, 5)
        self.assertEqual(json.loads(output.getvalue())["status"], "fail")

    def test_relink_burned_output_dispatches_and_prints_path(self) -> None:
        output = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            job_file = Path(directory) / "job.json"
            job_file.write_text("{}", encoding="utf-8")
            candidate = Path(directory) / "saved.mp4"
            candidate.write_bytes(b"video")
            with (
                patch(
                    "xvt.cli.relink_burned_output",
                    return_value=candidate.resolve(),
                ) as relink,
                redirect_stdout(output),
            ):
                return_code = main(["relink-burned-output", str(job_file), str(candidate)])

        self.assertEqual(return_code, 0)
        self.assertEqual(relink.call_args.args, (str(job_file), str(candidate)))
        self.assertIn(str(candidate.resolve()), output.getvalue())

    def test_relink_burned_output_jsonl_reports_cli_completion(self) -> None:
        output = StringIO()

        def relink_with_events(_job_file, _candidate, *, event_sink):
            event_sink(
                create_event(
                    EventType.ARTIFACT_INVALIDATED,
                    job_id="job-1",
                    kind="burned_video",
                    path="/tmp/old.mp4",
                )
            )
            event_sink(
                create_event(
                    EventType.ARTIFACT_CREATED,
                    job_id="job-1",
                    kind="burned_video",
                    path=str(candidate.resolve()),
                )
            )
            return candidate.resolve()

        with tempfile.TemporaryDirectory() as directory:
            job_file = Path(directory) / "job.json"
            job_file.write_text("{}", encoding="utf-8")
            candidate = Path(directory) / "saved.mp4"
            candidate.write_bytes(b"video")
            with (
                patch(
                    "xvt.cli.relink_burned_output",
                    side_effect=relink_with_events,
                ),
                redirect_stdout(output),
            ):
                return_code = main(
                    [
                        "relink-burned-output",
                        str(job_file),
                        str(candidate),
                        "--events-jsonl",
                    ]
                )

        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(return_code, 0)
        self.assertEqual(
            [event["type"] for event in events],
            ["artifact_invalidated", "artifact_created", "cli_completed"],
        )
        self.assertEqual(events[-1]["data"]["command"], "relink-burned-output")
        self.assertEqual(events[-1]["data"]["artifacts"], [str(candidate.resolve())])

    def test_resume_prints_job_file(self) -> None:
        output = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            job = create_job("/tmp/input.mp4", directory)
            with (
                patch("xvt.cli.resume_job", return_value=job) as resume,
                redirect_stdout(output),
            ):
                return_code = main(["resume", str(get_job_file(job))])

        self.assertEqual(return_code, 0)
        resume.assert_called_once()
        self.assertEqual(output.getvalue(), f"{get_job_file(job)}\n")

    def test_resume_jsonl_waiting_for_subtitle_review_has_no_cli_completed(self) -> None:
        output = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            job = create_job("/tmp/input.mp4", directory)
            job.stages[StageName.SUBTITLE_QA].status = StageStatus.AWAITING_INPUT

            def resume_waiting(_job_file, *, event_sink):
                event_sink(
                    create_event(
                        EventType.STAGE_WAITING,
                        stage="subtitle_qa",
                        status="awaiting_input",
                    )
                )
                return job

            with (
                patch("xvt.cli.resume_job", side_effect=resume_waiting),
                redirect_stdout(output),
            ):
                return_code = main(["resume", str(get_job_file(job)), "--events-jsonl"])

        events = [json.loads(line) for line in output.getvalue().splitlines() if line]
        self.assertEqual(return_code, 0)
        self.assertEqual([event["type"] for event in events], ["stage_waiting"])
        self.assertNotIn("cli_completed", {event["type"] for event in events})

    def test_rebuild_source_dispatches_and_prints_next_workflow_command(self) -> None:
        output = StringIO()
        result = RebuildSourceResult(
            Path("/tmp/source-cues.json"),
            Path("/tmp/source.srt"),
            Path("/tmp/qa.json"),
            Path("/tmp/source-selection.json"),
        )
        job = Job("job-1", "", "", "input", "/tmp", {"workflow": {}})
        with (
            patch("xvt.cli.rebuild_source", return_value=result) as rebuild,
            patch("xvt.cli.load_job", return_value=job),
            redirect_stdout(output),
        ):
            return_code = main(
                [
                    "rebuild-source",
                    "/tmp/job.json",
                    "--strategy",
                    "asr",
                    "--cookies-from-browser",
                    "chrome",
                    "--proxy",
                    "http://127.0.0.1:7890",
                ]
            )

        self.assertEqual(return_code, 0)
        self.assertEqual(rebuild.call_args.kwargs["strategy"], "asr")
        self.assertEqual(rebuild.call_args.kwargs["cookies_from_browser"], "chrome")
        self.assertIn("已使用 ASR 重建源字幕", output.getvalue())
        self.assertIn(
            f"xvt continue {shlex.quote(str(Path('/tmp/job.json').resolve()))}",
            output.getvalue(),
        )

    def test_rebuild_source_jsonl_emits_terminal_artifacts(self) -> None:
        output = StringIO()
        result = RebuildSourceResult(
            Path("/tmp/source-cues.json"),
            Path("/tmp/source.srt"),
            Path("/tmp/qa.json"),
            Path("/tmp/source-selection.json"),
        )
        with (
            patch("xvt.cli.rebuild_source", return_value=result),
            redirect_stdout(output),
        ):
            return_code = main(
                ["rebuild-source", "/tmp/job.json", "--strategy", "asr", "--events-jsonl"]
            )

        event = json.loads(output.getvalue())
        self.assertEqual(return_code, 0)
        self.assertEqual(event["type"], "cli_completed")
        self.assertEqual(
            event["data"]["artifacts"],
            [
                "/tmp/source-cues.json",
                "/tmp/source.srt",
                "/tmp/qa.json",
                "/tmp/source-selection.json",
            ],
        )

    def test_rebuild_source_is_a_locked_mutating_command(self) -> None:
        from xvt.cli import _JOB_MUTATING_COMMANDS

        self.assertIn("rebuild-source", _JOB_MUTATING_COMMANDS)

    def test_continue_forwards_runtime_network_options(self) -> None:
        output = StringIO()
        result = WorkflowResult("completed", Path("/tmp/job.json"))
        with (
            patch("xvt.cli.continue_workflow", return_value=result) as continue_run,
            redirect_stdout(output),
        ):
            return_code = main(
                [
                    "continue",
                    "/tmp/job.json",
                    "--cookies-from-browser",
                    "chrome",
                    "--proxy",
                    "http://127.0.0.1:7890",
                ]
            )

        self.assertEqual(return_code, 0)
        self.assertEqual(continue_run.call_args.kwargs["cookies_from_browser"], "chrome")
        self.assertEqual(continue_run.call_args.kwargs["proxy"], "http://127.0.0.1:7890")
        self.assertEqual(output.getvalue(), "/tmp/job.json\n")

    def test_errors_are_reported_without_traceback(self) -> None:
        error_output = StringIO()
        with (
            patch("xvt.cli.inspect_input", side_effect=ValueError("bad input")),
            redirect_stderr(error_output),
        ):
            return_code = main(["inspect", str(Path("missing.mp4"))])

        self.assertEqual(return_code, 2)
        self.assertEqual(error_output.getvalue(), "error: bad input\n")

    def test_run_forwards_explicit_network_options(self) -> None:
        output = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            result = WorkflowResult("completed", Path(directory) / "job.json")
            with (
                patch("xvt.cli.run_workflow", return_value=result) as run,
                redirect_stdout(output),
            ):
                return_code = main(
                    [
                        "run",
                        "https://youtu.be/abc",
                        "--output",
                        directory,
                        "--cookies-from-browser",
                        "chrome",
                        "--proxy",
                        "http://127.0.0.1:7890",
                        "--subtitles",
                        "/tmp/source.srt",
                        "--align-subtitles",
                        "--min-alignment-confidence",
                        "0.7",
                        "--allow-low-confidence-alignment",
                        "--source-strategy",
                        "subtitles",
                    ]
                )

        self.assertEqual(return_code, 0)
        options = run.call_args.args[0]
        self.assertEqual(options.cookies_from_browser, "chrome")
        self.assertEqual(options.proxy, "http://127.0.0.1:7890")
        self.assertEqual(options.subtitles, "/tmp/source.srt")
        self.assertTrue(options.align_subtitles)
        self.assertEqual(options.minimum_alignment_confidence, 0.7)
        self.assertTrue(options.allow_low_confidence_alignment)
        self.assertEqual(options.source_strategy, "subtitles")

    def test_run_shows_human_progress_on_stderr_and_keeps_paths_on_stdout(self) -> None:
        output = StringIO()
        error_output = StringIO()
        result = WorkflowResult("completed", Path("/tmp/job.json"))

        def run_with_progress(_options, *, event_sink):
            event_sink(create_event(EventType.WORKFLOW_STARTED))
            event_sink(
                create_event(
                    EventType.JOB_STARTED,
                    job_id="job-1",
                    job_file=str(result.job_file),
                    input="/tmp/input.mp4",
                )
            )
            event_sink(create_event(EventType.STAGE_STARTED, stage="acquire", status="running"))
            event_sink(
                create_event(
                    EventType.DOWNLOAD_PROGRESS,
                    percent=42.0,
                    speed=2 * 1024 * 1024,
                    eta_seconds=15,
                )
            )
            event_sink(
                create_event(
                    EventType.TRANSCRIPTION_PROGRESS,
                    engine="faster",
                    determinate=True,
                    processed_ms=210000,
                    total_ms=500000,
                    percent=42.0,
                    elapsed_seconds=30,
                    eta_seconds=41,
                )
            )
            event_sink(
                create_event(
                    EventType.TRANSLATION_BATCH_STARTED,
                    batch=2,
                    batch_total=5,
                )
            )
            event_sink(
                create_event(
                    EventType.TRANSLATION_BATCH_STARTED,
                    batch=38,
                    batch_index=1,
                    batch_total=1,
                )
            )
            event_sink(
                create_event(
                    EventType.SOURCE_SELECTED,
                    selected_kind="asr",
                    reason="automatic_subtitles_reference_only",
                )
            )
            event_sink(
                create_event(
                    EventType.TRANSLATION_BATCH_RETRYING,
                    batch=2,
                    batch_total=5,
                    attempt=2,
                    delay_seconds=1.0,
                    reason="provider temporarily unavailable",
                )
            )
            event_sink(
                create_event(
                    EventType.TRANSLATION_BATCH_STARTED,
                    batch=3,
                )
            )
            event_sink(
                create_event(
                    EventType.TRANSLATION_BATCH_RETRYING,
                    batch=3,
                    batch_total=5,
                    attempt=2,
                    delay_seconds=1.0,
                    reason="something unexpected",
                )
            )
            event_sink(create_event(EventType.STAGE_COMPLETED, stage="acquire", status="succeeded"))
            event_sink(create_event(EventType.WORKFLOW_COMPLETED, job_file=str(result.job_file)))
            return result

        with (
            patch("xvt.cli.run_workflow", side_effect=run_with_progress),
            redirect_stdout(output),
            redirect_stderr(error_output),
        ):
            return_code = main(["run", "/tmp/input.mp4", "--output", "/tmp/output"])

        self.assertEqual(return_code, 0)
        self.assertEqual(output.getvalue(), "/tmp/job.json\n")
        progress = error_output.getvalue()
        self.assertIn("XVT 开始处理任务", progress)
        self.assertIn("任务文件: /tmp/job.json", progress)
        self.assertIn("[进行中] 下载源视频  42%  2.0 MB/s  剩余 00:15", progress)
        self.assertIn("[进行中] 转写音频  42%  03:30/08:20  剩余 00:41", progress)
        self.assertIn("[进行中] 翻译字幕（批次 2/5）", progress)
        self.assertIn("[进行中] 翻译字幕（批次 1/1）", progress)
        self.assertIn(
            "[选择] 源字幕：采用 ASR 字幕（automatic_subtitles_reference_only）",
            progress,
        )
        # 已知重试原因在渲染层映射为中文，契约字符串本身保持英文。
        self.assertIn(
            "[重试] 翻译字幕（批次 2/5，第 2 次尝试，等待 1s，原因：翻译服务暂时不可用）",
            progress,
        )
        # 缺少 batch_total 的旧事件流回退为仅显示批次 ID。
        self.assertIn("[进行中] 翻译字幕（批次 3）", progress)
        # 未知重试原因回退显示契约原文。
        self.assertIn(
            "[重试] 翻译字幕（批次 3/5，第 2 次尝试，等待 1s，原因：something unexpected）",
            progress,
        )
        self.assertIn("任务处理完成", progress)

    def test_human_progress_coalesces_skips_and_dedupes_failures(self) -> None:
        output = StringIO()
        error_output = StringIO()
        result = WorkflowResult(
            "completed",
            Path("/tmp/job.json"),
            (Path("/tmp/source.srt"), Path("/tmp/burned.mp4")),
        )

        def run_with_skips(_options, *, event_sink):
            event_sink(create_event(EventType.WORKFLOW_STARTED))
            for stage in ("import_subtitles", "align", "translate", "burn", "media_qa"):
                event_sink(create_event(EventType.STAGE_COMPLETED, stage=stage, status="skipped"))
            event_sink(
                create_event(
                    EventType.STAGE_STARTED,
                    stage="normalize_audio",
                    status="running",
                )
            )
            event_sink(
                create_event(
                    EventType.STAGE_COMPLETED,
                    stage="normalize_audio",
                    status="succeeded",
                )
            )
            event_sink(
                create_event(
                    EventType.STAGE_STARTED,
                    stage="subtitle_qa",
                    status="running",
                    detail="source",
                )
            )
            event_sink(
                create_event(
                    EventType.STAGE_FAILED,
                    stage="subtitle_qa",
                    status="failed",
                    detail="source",
                    message="needs_review",
                )
            )
            event_sink(
                create_event(
                    EventType.STAGE_FAILED,
                    stage="subtitle_qa",
                    status="failed",
                    detail="source",
                    message="needs_review",
                )
            )
            return result

        with (
            patch("xvt.cli.run_workflow", side_effect=run_with_skips),
            redirect_stdout(output),
            redirect_stderr(error_output),
        ):
            return_code = main(["run", "/tmp/input.mp4", "--output", "/tmp/output"])

        self.assertEqual(return_code, 0)
        progress = error_output.getvalue()
        self.assertIn("[跳过] 导入源字幕、对齐字幕", progress)
        self.assertNotIn("[跳过] 翻译字幕", progress)
        self.assertNotIn("[跳过] 烧录视频", progress)
        self.assertEqual(progress.count("[失败] 检查源字幕"), 1)
        self.assertIn("视频:", output.getvalue())
        self.assertIn("字幕:", output.getvalue())
        self.assertIn("/tmp/burned.mp4", output.getvalue())
        self.assertIn("/tmp/source.srt", output.getvalue())

    def test_workflow_result_lists_non_media_artifacts_under_other(self) -> None:
        output = StringIO()
        error_output = StringIO()
        result = WorkflowResult(
            "awaiting_input",
            Path("/tmp/job.json"),
            (Path("/tmp/translation-batches.json"),),
            "translation requires Agent input",
        )

        def run_waiting(_options, *, event_sink):
            event_sink(create_event(EventType.WORKFLOW_STARTED))
            event_sink(create_event(EventType.WORKFLOW_WAITING, reason="needs agent"))
            return result

        with (
            patch("xvt.cli.run_workflow", side_effect=run_waiting),
            redirect_stdout(output),
            redirect_stderr(error_output),
        ):
            return_code = main(["run", "/tmp/input.mp4", "--output", "/tmp/output"])

        self.assertEqual(return_code, 0)
        text = output.getvalue()
        self.assertIn("其他:", text)
        self.assertIn("/tmp/translation-batches.json", text)
        self.assertIn("translation is awaiting input", text)

    def test_workflow_awaiting_result_prints_recovery_plan(self) -> None:
        output = StringIO()
        result = WorkflowResult("awaiting_input", Path("/tmp/job.json"), (), "needs agent")
        plan = SimpleNamespace(
            reason="等待 Agent 语义校对",
            report_paths=("/tmp/report.json",),
            input_paths=("/tmp/review.json",),
            prerequisite_commands=("xvt apply /tmp/job.json RESPONSE",),
            primary_command="xvt continue /tmp/job.json",
            alternative_commands=("xvt apply-semantic-review /tmp/job.json RESPONSE",),
        )

        with (
            patch("xvt.cli.run_workflow", return_value=result),
            patch("xvt.cli.build_recovery_plan", return_value=plan),
            redirect_stdout(output),
            redirect_stderr(StringIO()),
        ):
            return_code = main(["run", "/tmp/input.mp4", "--output", "/tmp/output"])

        self.assertEqual(return_code, 0)
        text = output.getvalue()
        self.assertIn("恢复建议：等待 Agent 语义校对", text)
        self.assertIn("报告: /tmp/report.json", text)
        self.assertIn("待编辑: /tmp/review.json", text)
        self.assertIn("xvt continue /tmp/job.json", text)

    def test_status_displays_needs_review_instead_of_failed(self) -> None:
        output = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            job.config["workflow"] = {"target_language": "zh-CN", "burn": False}
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir(parents=True)
            cues = artifacts / "source-cues.json"
            cues.write_text("[]", encoding="utf-8")
            qa = artifacts / "qa.json"
            qa.write_text(
                json.dumps(
                    {
                        "status": "needs_review",
                        "issues": [{"code": "line_too_wide", "severity": "review", "cue_id": 1}],
                    }
                ),
                encoding="utf-8",
            )
            from xvt.models import Artifact
            from xvt.recovery import file_checksum

            job.artifacts.extend(
                [
                    Artifact("source_cues", str(cues), file_checksum(cues)),
                    Artifact("subtitle_qa", str(qa), file_checksum(qa)),
                ]
            )
            job.stages[StageName.SUBTITLE_QA].status = StageStatus.FAILED
            job_file = save_job(job)
            with redirect_stdout(output):
                return_code = main(["status", str(job_file)])

        self.assertEqual(return_code, 0)
        text = output.getvalue()
        self.assertIn("subtitle_qa=needs_review", text)
        self.assertNotIn("subtitle_qa=failed", text)
        self.assertIn("--acknowledge-subtitle-review", text)

    def test_run_failure_shows_job_and_safe_status_command(self) -> None:
        output = StringIO()
        error_output = StringIO()

        def fail_after_job_created(_options, *, event_sink):
            event_sink(create_event(EventType.WORKFLOW_STARTED))
            event_sink(
                create_event(
                    EventType.JOB_STARTED,
                    job_id="job-1",
                    job_file="/tmp/job.json",
                    input="https://example.com/video",
                )
            )
            event_sink(create_event(EventType.STAGE_STARTED, stage="acquire", status="running"))
            raise RuntimeError("download unavailable")

        with (
            patch("xvt.cli.run_workflow", side_effect=fail_after_job_created),
            redirect_stdout(output),
            redirect_stderr(error_output),
        ):
            return_code = main(["run", "https://example.com/video", "--output", "/tmp/output"])

        self.assertEqual(return_code, 4)
        self.assertEqual(output.getvalue(), "")
        failure = error_output.getvalue()
        self.assertIn("任务失败：download unavailable", failure)
        self.assertIn("失败阶段：获取素材", failure)
        self.assertIn("任务文件: /tmp/job.json", failure)
        self.assertIn('xvt status "/tmp/job.json"', failure)

    def test_existing_job_failure_keeps_job_path(self) -> None:
        error_output = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            job = create_job("/tmp/input.mp4", directory)
            job_file = get_job_file(job)
            with (
                patch("xvt.cli.run_translation", side_effect=ValueError("missing source cues")),
                redirect_stderr(error_output),
            ):
                return_code = main(["translate", str(job_file), "--target-language", "zh-CN"])

        self.assertEqual(return_code, 2)
        failure = error_output.getvalue()
        self.assertIn(f"任务文件: {job_file}", failure)
        self.assertIn(f'xvt status "{job_file}"', failure)
        self.assertNotIn("任务尚未创建", failure)

    def test_keyboard_interrupt_fails_running_stage_and_returns_130(self) -> None:
        error_output = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            job = create_job("/tmp/input.mp4", directory)
            job.stages[StageName.ACQUIRE].status = StageStatus.RUNNING
            job_file = save_job(job)

            def interrupt_workflow(_options, *, event_sink):
                event_sink(
                    create_event(
                        EventType.JOB_STARTED,
                        job_id=job.id,
                        job_file=str(job_file),
                        input=job.input,
                    )
                )
                event_sink(create_event(EventType.STAGE_STARTED, stage="acquire", status="running"))
                raise KeyboardInterrupt

            with (
                patch("xvt.cli.run_workflow", side_effect=interrupt_workflow),
                redirect_stderr(error_output),
            ):
                return_code = main(["run", job.input, "--output", directory])

            interrupted = load_job(job_file)

        self.assertEqual(return_code, 130)
        self.assertEqual(interrupted.stages[StageName.ACQUIRE].status, StageStatus.FAILED)
        self.assertEqual(interrupted.stages[StageName.ACQUIRE].error, "interrupted by user")
        failure = error_output.getvalue()
        self.assertIn("任务已中断", failure)
        self.assertIn("失败阶段：获取素材", failure)
        self.assertIn(f'xvt status "{job_file}"', failure)

    def test_sigterm_returns_143_and_persists_interrupted_stage(self) -> None:
        error_output = StringIO()
        original_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            with tempfile.TemporaryDirectory() as directory:
                job = create_job("/tmp/input.mp4", directory)
                job.stages[StageName.ACQUIRE].status = StageStatus.RUNNING
                job_file = save_job(job)

                def terminate_workflow(_options, *, event_sink):
                    event_sink(
                        create_event(
                            EventType.JOB_STARTED,
                            job_id=job.id,
                            job_file=str(job_file),
                            input=job.input,
                        )
                    )
                    event_sink(
                        create_event(EventType.STAGE_STARTED, stage="acquire", status="running")
                    )
                    os.kill(os.getpid(), signal.SIGTERM)
                    self.fail("signal handler should have raised before this line")

                with (
                    patch("xvt.cli.run_workflow", side_effect=terminate_workflow),
                    redirect_stderr(error_output),
                ):
                    return_code = main(["run", job.input, "--output", directory])

                interrupted = load_job(job_file)

            self.assertEqual(return_code, 143)
            self.assertEqual(interrupted.stages[StageName.ACQUIRE].status, StageStatus.FAILED)
            self.assertEqual(interrupted.stages[StageName.ACQUIRE].error, "interrupted by user")
        finally:
            cancellation.reset()
            for sig, handler in original_handlers.items():
                signal.signal(sig, handler)

    def test_signal_scope_second_signal_does_not_raise(self) -> None:
        # 双击 Ctrl-C：第一次抛 KeyboardInterrupt 进入清理，第二次只保持取消状态。
        with cancellation.signal_scope():
            with self.assertRaises(KeyboardInterrupt):
                os.kill(os.getpid(), signal.SIGINT)
            os.kill(os.getpid(), signal.SIGINT)
            self.assertTrue(cancellation.cancellation_requested())
        self.assertFalse(cancellation.cancellation_requested())

    def test_signal_scope_resets_stale_cancellation_state(self) -> None:
        # 上一次调用遗留的取消状态不得影响下一次 main()：进入作用域即复位。
        cancellation.cancel_event().set()
        error_output = StringIO()
        with (
            patch("xvt.cli.run_workflow", side_effect=ValueError("bad input")),
            redirect_stderr(error_output),
        ):
            return_code = main(["run", "/tmp/input.mp4", "--output", "/tmp/output"])

        self.assertEqual(return_code, 2)
        self.assertNotIn(return_code, (130, 143))

    def test_signal_scope_restores_previous_handlers(self) -> None:
        sentinel = signal.getsignal(signal.SIGTERM)
        with cancellation.signal_scope():
            self.assertNotEqual(signal.getsignal(signal.SIGTERM), sentinel)
        self.assertEqual(signal.getsignal(signal.SIGTERM), sentinel)

    def test_command_cancelled_is_reported_as_interruption(self) -> None:
        error_output = StringIO()
        try:
            with (
                patch("xvt.cli.run_workflow", side_effect=CommandCancelled("cancelled")),
                redirect_stderr(error_output),
            ):
                return_code = main(["run", "/tmp/input.mp4", "--output", "/tmp/output"])

            self.assertEqual(return_code, 130)
            self.assertIn("任务已中断", error_output.getvalue())
        finally:
            cancellation.reset()

    def test_jsonl_keyboard_interrupt_ends_with_structured_failure(self) -> None:
        output = StringIO()
        error_output = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            job = create_job("/tmp/input.mp4", directory)
            job.stages[StageName.ACQUIRE].status = StageStatus.RUNNING
            job_file = save_job(job)

            def interrupt_workflow(_options, *, event_sink):
                event_sink(
                    create_event(
                        EventType.JOB_STARTED,
                        job_id=job.id,
                        job_file=str(job_file),
                        input=job.input,
                    )
                )
                event_sink(create_event(EventType.STAGE_STARTED, stage="acquire", status="running"))
                raise KeyboardInterrupt

            with (
                patch("xvt.cli.run_workflow", side_effect=interrupt_workflow),
                redirect_stdout(output),
                redirect_stderr(error_output),
            ):
                return_code = main(["run", job.input, "--output", directory, "--events-jsonl"])

        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(return_code, 130)
        self.assertEqual(events[-2]["type"], "stage_failed")
        self.assertEqual(events[-1]["type"], "cli_failed")
        self.assertEqual(events[-1]["data"]["error"]["category"], "interrupted")
        self.assertEqual(events[-1]["data"]["recovery_plan"]["category"], "unrecoverable")
        self.assertEqual(error_output.getvalue(), "error: interrupted by user\n")

    def test_run_forwards_youtube_subtitle_options(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = WorkflowResult("completed", Path(directory) / "job.json")
            with patch("xvt.cli.run_workflow", return_value=result) as run:
                return_code = main(
                    [
                        "run",
                        "https://youtu.be/abc",
                        "--output",
                        directory,
                        "--youtube-subtitles",
                        "--subtitle-language",
                        "en",
                        "--allow-auto-subtitles",
                    ]
                )

        self.assertEqual(return_code, 0)
        options = run.call_args.args[0]
        self.assertTrue(options.youtube_subtitles)
        self.assertEqual(options.subtitle_language, "en")
        self.assertTrue(options.allow_auto_subtitles)

    def test_run_can_apply_translation_and_burn_complete_inputs(self) -> None:
        output = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            result = WorkflowResult(
                "completed",
                Path(directory) / "job.json",
                (Path("/tmp/bilingual.srt"), Path("/tmp/burned.mp4")),
            )
            with (
                patch("xvt.cli.run_workflow", return_value=result) as run,
                redirect_stdout(output),
            ):
                return_code = main(
                    [
                        "run",
                        "/tmp/video.mp4",
                        "--output",
                        directory,
                        "--subtitles",
                        "/tmp/source.srt",
                        "--translation-subtitles",
                        "/tmp/translated.srt",
                        "--target-language",
                        "zh-CN",
                        "--burn",
                        "--software",
                    ]
                )

        self.assertEqual(return_code, 0)
        options = run.call_args.args[0]
        self.assertEqual(options.translation_subtitles, "/tmp/translated.srt")
        self.assertEqual(options.target_language, "zh-CN")
        self.assertTrue(options.burn)
        self.assertFalse(options.hardware_encoding)
        self.assertIn("/tmp/burned.mp4", output.getvalue())

    def test_run_forwards_automatic_translation_options(self) -> None:
        result = WorkflowResult("completed", Path("/tmp/job.json"))
        with patch("xvt.cli.run_workflow", return_value=result) as run:
            return_code = main(
                [
                    "run",
                    "/tmp/video.mp4",
                    "--output",
                    "/tmp/output",
                    "--target-language",
                    "zh-CN",
                    "--provider",
                    "openai-compatible",
                    "--base-url",
                    "https://api.example.com/v1",
                    "--model",
                    "translate-model",
                    "--glossary",
                    "/tmp/glossary.json",
                    "--translation-style",
                    "concise",
                    "--segmentation-preset",
                    "compact",
                    "--burn",
                ]
            )

        self.assertEqual(return_code, 0)
        options = run.call_args.args[0]
        self.assertEqual(options.provider, "openai-compatible")
        self.assertEqual(options.base_url, "https://api.example.com/v1")
        self.assertEqual(options.model, "translate-model")
        self.assertEqual(options.glossary, "/tmp/glossary.json")
        self.assertEqual(options.translation_style, "concise")
        self.assertEqual(options.segmentation_preset, "compact")
        self.assertTrue(options.burn)

    def test_run_rejects_glossary_with_imported_translation_subtitles(self) -> None:
        """术语表 + 导入译文在 run 入口即被拒绝，退出码 2 且不落任何 Job。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello.\n",
                encoding="utf-8",
            )
            translated = root / "translated.srt"
            translated.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n你好。\n",
                encoding="utf-8",
            )
            glossary = root / "glossary.json"
            glossary.write_text(
                json.dumps({"terms": [{"source": "Hello", "target": "你好"}]}),
                encoding="utf-8",
            )
            output = root / "output"
            error_output = StringIO()
            with redirect_stderr(error_output), redirect_stdout(StringIO()):
                return_code = main(
                    [
                        "run",
                        str(source),
                        "--output",
                        str(output),
                        "--target-language",
                        "zh-CN",
                        "--translation-subtitles",
                        str(translated),
                        "--glossary",
                        str(glossary),
                    ]
                )

            self.assertEqual(return_code, 2)
            self.assertIn(
                "--glossary cannot be combined with --translation-subtitles",
                error_output.getvalue(),
            )
            self.assertEqual(list((output / "jobs").glob("*/job.json")), [])

    def test_align_subtitles_requires_subtitle_file(self) -> None:
        error_output = StringIO()
        with redirect_stderr(error_output):
            return_code = main(["run", "/tmp/video.mp4", "--output", "/tmp", "--align-subtitles"])

        self.assertEqual(return_code, 2)
        self.assertIn("requires --subtitles", error_output.getvalue())

    def test_prepare_translation_forwards_languages(self) -> None:
        output = StringIO()
        with (
            patch(
                "xvt.cli.prepare_agent_translation", return_value=Path("/tmp/batches.json")
            ) as prepare,
            redirect_stdout(output),
        ):
            return_code = main(
                [
                    "prepare-translation",
                    "/tmp/job.json",
                    "--source-language",
                    "en",
                    "--target-language",
                    "zh-CN",
                    "--glossary",
                    "/tmp/glossary.json",
                    "--translation-style",
                    "natural",
                ]
            )

        self.assertEqual(return_code, 0)
        self.assertEqual(prepare.call_args.kwargs["source_language"], "en")
        self.assertEqual(prepare.call_args.kwargs["glossary"], "/tmp/glossary.json")
        self.assertEqual(prepare.call_args.kwargs["translation_style"], "natural")
        self.assertEqual(output.getvalue(), "/tmp/batches.json\n")

    def test_apply_translation_jsonl_semantic_review_waiting_has_no_cli_completed(self) -> None:
        output = StringIO()
        job = Job("job-1", "", "", "input", "/tmp", {"translation_quality": "standard"})

        def apply_waiting(_job_file, _response_file, *, target_language, event_sink):
            self.assertEqual(target_language, "zh-CN")
            event_sink(
                create_event(
                    EventType.STAGE_WAITING,
                    stage="translate",
                    status="awaiting_input",
                )
            )
            return (Path("/tmp/translated.json"),)

        with (
            patch("xvt.cli.apply_agent_translation", side_effect=apply_waiting),
            patch("xvt.cli.load_job", return_value=job),
            patch("xvt.cli.semantic_review_waiting", return_value=True),
            redirect_stdout(output),
        ):
            return_code = main(
                [
                    "apply-translation",
                    "/tmp/job.json",
                    "/tmp/response.json",
                    "--target-language",
                    "zh-CN",
                    "--events-jsonl",
                ]
            )

        events = [json.loads(line) for line in output.getvalue().splitlines() if line]
        self.assertEqual(return_code, 0)
        self.assertEqual([event["type"] for event in events], ["stage_waiting"])
        self.assertNotIn("cli_completed", {event["type"] for event in events})

    def test_translate_reports_agent_fallback(self) -> None:
        output = StringIO()
        result = TranslationRunResult(
            "agent",
            (Path("/tmp/translation-batches.json"),),
            "missing model",
        )
        with (
            patch("xvt.cli.run_translation", return_value=result) as translate,
            redirect_stdout(output),
        ):
            return_code = main(
                [
                    "translate",
                    "/tmp/job.json",
                    "--target-language",
                    "zh-CN",
                    "--glossary",
                    "/tmp/glossary.json",
                    "--translation-style",
                    "concise",
                ]
            )

        self.assertEqual(return_code, 0)
        self.assertEqual(translate.call_args.kwargs["provider"], "auto")
        self.assertEqual(translate.call_args.kwargs["glossary"], "/tmp/glossary.json")
        self.assertEqual(translate.call_args.kwargs["translation_style"], "concise")
        self.assertIn("using Agent mode", output.getvalue())
        self.assertIn("translation-batches.json", output.getvalue())

    def test_translate_jsonl_semantic_review_waiting_has_no_cli_completed(self) -> None:
        output = StringIO()
        job = Job("job-1", "", "", "input", "/tmp", {"translation_quality": "standard"})
        result = TranslationRunResult("agent", (Path("/tmp/translation-batches.json"),))

        def translate_waiting(_job_file, **options):
            options["event_sink"](
                create_event(
                    EventType.STAGE_WAITING,
                    stage="translate",
                    status="awaiting_input",
                )
            )
            return result

        with (
            patch("xvt.cli.run_translation", side_effect=translate_waiting),
            patch("xvt.cli.load_job", return_value=job),
            patch("xvt.cli.semantic_review_waiting", return_value=True),
            redirect_stdout(output),
        ):
            return_code = main(
                [
                    "translate",
                    "/tmp/job.json",
                    "--target-language",
                    "zh-CN",
                    "--events-jsonl",
                ]
            )

        events = [json.loads(line) for line in output.getvalue().splitlines() if line]
        self.assertEqual(return_code, 0)
        self.assertEqual([event["type"] for event in events], ["stage_waiting"])
        self.assertNotIn("cli_completed", {event["type"] for event in events})

    def test_translate_runs_atomic_polish_for_polished_api_result(self) -> None:
        output = StringIO()
        draft = TranslationRunResult("openai-compatible", (Path("/tmp/draft.json"),))
        polished = TranslationRunResult("reused", (Path("/tmp/translated.srt"),))
        semantic = TranslationRunResult("openai-compatible", (Path("/tmp/semantic.json"),))
        with (
            patch("xvt.cli.run_translation", return_value=draft) as translate,
            patch("xvt.cli.load_job", return_value=SimpleNamespace(config={})),
            patch("xvt.cli.resolve_translation_quality", return_value="polished"),
            patch("xvt.cli.get_workflow_config", return_value=None),
            patch("xvt.cli.run_atomic_translation_polish", return_value=polished) as polish,
            patch("xvt.cli.run_translation_semantic_review", return_value=semantic) as review,
            redirect_stdout(output),
        ):
            return_code = main(
                [
                    "translate",
                    "/tmp/job.json",
                    "--target-language",
                    "zh-CN",
                    "--translation-quality",
                    "polished",
                    "--provider",
                    "openai-compatible",
                    "--base-url",
                    "https://example.com/v1",
                    "--model",
                    "model",
                ]
            )

        self.assertEqual(return_code, 0)
        self.assertEqual(translate.call_args.kwargs["translation_quality"], "polished")
        polish.assert_called_once()
        review.assert_called_once()
        self.assertIn("mode: openai-compatible", output.getvalue())

    def test_burn_can_force_software_encoding(self) -> None:
        output = StringIO()
        with (
            patch("xvt.cli.burn_translated_job", return_value=Path("/tmp/output.mp4")) as burn,
            redirect_stdout(output),
        ):
            return_code = main(
                [
                    "burn",
                    "/tmp/job.json",
                    "--software",
                    "--target-font",
                    "Noto Sans CJK SC",
                    "--source-font",
                    "DejaVu Sans",
                ]
            )

        self.assertEqual(return_code, 0)
        self.assertFalse(burn.call_args.kwargs["hardware_encoding"])
        self.assertEqual(burn.call_args.kwargs["target_font"], "Noto Sans CJK SC")
        self.assertEqual(burn.call_args.kwargs["source_font"], "DejaVu Sans")
        self.assertEqual(output.getvalue(), "/tmp/output.mp4\n")

    def test_import_translation_forwards_target_language(self) -> None:
        output = StringIO()
        paths = (Path("/tmp/translated.srt"), Path("/tmp/bilingual.srt"))
        job = Job("job-1", "", "", "input", "/tmp", {})
        with (
            patch("xvt.cli.apply_subtitle_translation", return_value=paths) as apply,
            patch("xvt.cli.load_job", return_value=job),
            redirect_stdout(output),
        ):
            return_code = main(
                [
                    "import-translation",
                    "/tmp/job.json",
                    "/tmp/zh.srt",
                    "--target-language",
                    "zh-CN",
                ]
            )

        self.assertEqual(return_code, 0)
        self.assertEqual(apply.call_args.kwargs["target_language"], "zh-CN")
        self.assertIn("/tmp/bilingual.srt", output.getvalue())

    def _job_with_source_cues(self, root: Path, cues: list) -> str:
        source = root / "source.srt"
        source.write_text(
            "1\n00:00:00,000 --> 00:00:02,000\nHello.\n",
            encoding="utf-8",
        )
        job = run_subtitle_transcription(source, root, language="en")
        artifact = next(item for item in job.artifacts if item.kind == "source_cues")
        path = Path(artifact.path)
        path.write_text(json.dumps(cues, ensure_ascii=False), encoding="utf-8")
        artifact.checksum = file_checksum(path)
        save_job(job)
        return str(get_job_file(job))

    def test_import_translation_tolerates_unknown_cue_fields(self) -> None:
        """跨版本 cues 产物的未知字段应被忽略，而不是抛裸 TypeError。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job_with_source_cues(
                root,
                [
                    {
                        "id": 1,
                        "start_ms": 0,
                        "end_ms": 2000,
                        "source_text": "Hello.",
                        "words": [],
                        "future_field": "from-newer-version",
                    }
                ],
            )
            translated = root / "zh.srt"
            translated.write_text(
                "1\n00:00:00,000 --> 00:00:02,000\n你好。\n",
                encoding="utf-8",
            )
            with redirect_stdout(StringIO()):
                return_code = main(
                    [
                        "import-translation",
                        job_file,
                        str(translated),
                        "--target-language",
                        "zh-CN",
                    ]
                )

        self.assertEqual(return_code, 0)

    def test_import_translation_reports_malformed_cue_artifact_as_user_error(self) -> None:
        """坏 cues 产物必须走 ValueError → 退出码 2 + cli_failed，而不是裸 traceback。"""
        errors = StringIO()
        output = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job_with_source_cues(root, [{"id": 1, "start_ms": 0, "end_ms": 2000}])
            translated = root / "zh.srt"
            translated.write_text(
                "1\n00:00:00,000 --> 00:00:02,000\n你好。\n",
                encoding="utf-8",
            )
            with redirect_stderr(errors), redirect_stdout(output):
                return_code = main(
                    [
                        "import-translation",
                        job_file,
                        str(translated),
                        "--target-language",
                        "zh-CN",
                        "--events-jsonl",
                    ]
                )

        self.assertEqual(return_code, 2)
        streams = output.getvalue().splitlines() + errors.getvalue().splitlines()
        events = [json.loads(line) for line in streams if line.startswith("{")]
        failure = next(event for event in events if event["type"] == "cli_failed")
        self.assertEqual(failure["data"]["error"]["category"], "invalid_input")
        self.assertIn("invalid cue artifact", failure["data"]["error"]["message"])

    def test_import_translation_rejects_non_object_cue_entries(self) -> None:
        errors = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job_with_source_cues(root, ["not-a-cue"])
            translated = root / "zh.srt"
            translated.write_text(
                "1\n00:00:00,000 --> 00:00:02,000\n你好。\n",
                encoding="utf-8",
            )
            with redirect_stderr(errors), redirect_stdout(StringIO()):
                return_code = main(
                    [
                        "import-translation",
                        job_file,
                        str(translated),
                        "--target-language",
                        "zh-CN",
                    ]
                )

        self.assertEqual(return_code, 2)
        self.assertIn("invalid cue artifact", errors.getvalue())

    def test_run_reports_non_utf8_source_subtitle_as_user_error(self) -> None:
        """GBK 源字幕必须走 UserInputError → 退出码 2 + cli_failed(invalid_input)，不是裸栈。

        不 mock 管线：错误消息必须指向用户传入的原始路径，指向 Job 内部副本
        （artifacts/input-subtitles.srt）对用户没有任何可操作性。
        """

        output = StringIO()
        errors = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "源字幕.srt"
            source.write_bytes("1\n00:00:01,000 --> 00:00:02,500\n你好世界\n".encode("gbk"))
            with redirect_stdout(output), redirect_stderr(errors):
                return_code = main(
                    [
                        "run",
                        str(source),
                        "--output",
                        str(root / "out"),
                        "--target-language",
                        "zh-CN",
                        "--provider",
                        "agent",
                        "--events-jsonl",
                    ]
                )

            self.assertEqual(return_code, 2)
            events = [
                json.loads(line) for line in output.getvalue().splitlines() if line.startswith("{")
            ]
            failure = next(event for event in events if event["type"] == "cli_failed")
            stage_failure = next(event for event in events if event["type"] == "stage_failed")
            self.assertEqual(failure["data"]["error"]["category"], "invalid_input")
            # stage 与 CLI 两处 category 必须同口径（此前 UnicodeDecodeError 记成 workflow）。
            self.assertEqual(stage_failure["data"]["error"]["category"], "invalid_input")
            self.assertEqual(stage_failure["data"]["stage"], "import_subtitles")
            message = failure["data"]["error"]["message"]
            self.assertIn("not UTF-8 encoded", message)
            self.assertIn("convert it to UTF-8", message)
            self.assertIn(str(source), message)
            self.assertNotIn("input-subtitles", message)

        self.assertNotIn("Traceback", errors.getvalue())
        self.assertEqual(errors.getvalue().splitlines(), [f"error: {message}"])

    def test_import_translation_reports_unparsable_subtitle_as_user_error(self) -> None:
        """pysubs2 的解析异常不被顶层兜底认得，必须包装成 UserInputError。"""

        output = StringIO()
        errors = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job_with_source_cues(
                root,
                [{"id": 1, "start_ms": 0, "end_ms": 2000, "source_text": "Hello."}],
            )
            translated = root / "zh.srt"
            translated.write_text("这不是字幕\n只是一段散文\n", encoding="utf-8")
            with redirect_stdout(output), redirect_stderr(errors):
                return_code = main(
                    [
                        "import-translation",
                        job_file,
                        str(translated),
                        "--target-language",
                        "zh-CN",
                        "--events-jsonl",
                    ]
                )

            self.assertEqual(return_code, 2)
            events = [
                json.loads(line) for line in output.getvalue().splitlines() if line.startswith("{")
            ]
            failure = next(event for event in events if event["type"] == "cli_failed")
            self.assertEqual(failure["data"]["error"]["category"], "invalid_input")
            self.assertIn("could not be parsed as srt", failure["data"]["error"]["message"])
            self.assertIn(str(translated), failure["data"]["error"]["message"])

        self.assertNotIn("Traceback", errors.getvalue())

    def test_import_translation_reports_foreign_format_parse_failures_as_user_error(self) -> None:
        """pysubs2 按内容嗅探格式：`.srt` 里装 SAMI/TTML 会落到别的 parser。

        那些 parser 抛的是裸 KeyError / NotImplementedError，既不是 Pysubs2Error 也不是
        ValueError——只捕获 Pysubs2Error 时 SAMI 会穿透成裸 traceback（退出码 1、JSONL 无
        cli_failed 终态），TTML 的 NotImplementedError 是 RuntimeError 子类，会被错分成
        workflow（退出码 4）且消息不含路径。
        """

        cases = {
            "sami": "<SAMI><BODY><SYNC><P>hi</SYNC></BODY></SAMI>",
            "ttml": (
                '<tt xmlns="http://www.w3.org/ns/ttml"><body><div>'
                '<p begin="banana" end="orange">hi</p></div></body></tt>'
            ),
        }
        for label, body in cases.items():
            with self.subTest(content=label):
                output = StringIO()
                errors = StringIO()
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    job_file = self._job_with_source_cues(
                        root,
                        [{"id": 1, "start_ms": 0, "end_ms": 2000, "source_text": "Hello."}],
                    )
                    translated = root / "zh.srt"
                    translated.write_text(body, encoding="utf-8")
                    with redirect_stdout(output), redirect_stderr(errors):
                        return_code = main(
                            [
                                "import-translation",
                                job_file,
                                str(translated),
                                "--target-language",
                                "zh-CN",
                                "--events-jsonl",
                            ]
                        )

                    self.assertEqual(return_code, 2)
                    events = [
                        json.loads(line)
                        for line in output.getvalue().splitlines()
                        if line.startswith("{")
                    ]
                    failure = next(event for event in events if event["type"] == "cli_failed")
                    self.assertEqual(failure["data"]["error"]["category"], "invalid_input")
                    message = failure["data"]["error"]["message"]
                    self.assertIn("could not be parsed as srt", message)
                    self.assertIn(str(translated), message)

                self.assertNotIn("Traceback", errors.getvalue())

    def test_import_translation_jsonl_review_waiting_has_no_cli_completed(self) -> None:
        output = StringIO()
        job = Job("job-1", "", "", "input", "/tmp", {})
        job.stages[StageName.SUBTITLE_QA].status = StageStatus.AWAITING_INPUT
        with (
            patch(
                "xvt.cli.apply_subtitle_translation",
                return_value=(Path("/tmp/qa-translated.json"),),
            ),
            patch("xvt.cli.load_job", return_value=job),
            redirect_stdout(output),
        ):
            return_code = main(
                [
                    "import-translation",
                    "/tmp/job.json",
                    "/tmp/zh.srt",
                    "--target-language",
                    "zh-CN",
                    "--events-jsonl",
                ]
            )

        self.assertEqual(return_code, 0)
        self.assertNotIn("cli_completed", output.getvalue())

    def test_apply_cue_retranslation_jsonl_review_waiting_has_no_cli_completed(self) -> None:
        output = StringIO()
        job = Job("job-1", "", "", "input", "/tmp", {})
        job.stages[StageName.SUBTITLE_QA].status = StageStatus.AWAITING_INPUT
        with (
            patch(
                "xvt.cli.apply_cue_retranslation",
                return_value=(Path("/tmp/qa-translated.json"),),
            ),
            patch("xvt.cli.load_job", return_value=job),
            redirect_stdout(output),
        ):
            return_code = main(
                [
                    "apply-cue-retranslation",
                    "/tmp/job.json",
                    "/tmp/response.json",
                    "--cue",
                    "1",
                    "--target-language",
                    "zh-CN",
                    "--events-jsonl",
                ]
            )

        self.assertEqual(return_code, 0)
        self.assertNotIn("cli_completed", output.getvalue())

    def test_doctor_json_returns_dependency_exit_code(self) -> None:
        output = StringIO()
        report = {"status": "fail", "platform": "darwin", "architecture": "arm64", "checks": []}
        with (
            patch("xvt.cli.run_doctor", return_value=report),
            redirect_stdout(output),
        ):
            return_code = main(["doctor", "--json"])

        self.assertEqual(return_code, 3)
        self.assertEqual(json.loads(output.getvalue())["status"], "fail")

    def test_jsonl_mode_has_structured_terminal_event(self) -> None:
        output = StringIO()
        error_output = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            result = WorkflowResult("completed", Path(directory) / "job.json")

            def run_with_event(_options, *, event_sink):
                event_sink(create_event(EventType.STAGE_STARTED, stage="inspect", status="running"))
                return result

            with (
                patch("xvt.cli.run_workflow", side_effect=run_with_event),
                redirect_stdout(output),
                redirect_stderr(error_output),
            ):
                return_code = main(
                    ["run", "/tmp/input.mp4", "--output", directory, "--events-jsonl"]
                )

        lines = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(return_code, 0)
        self.assertEqual(lines[0]["type"], "stage_started")
        self.assertEqual(lines[-1]["type"], "cli_completed")
        self.assertEqual(lines[-1]["data"]["job_file"], str(result.job_file))
        self.assertEqual(error_output.getvalue(), "")

    def test_jsonl_error_is_structured_and_keeps_stderr_diagnostic(self) -> None:
        output = StringIO()
        error_output = StringIO()
        with (
            patch("xvt.cli.run_workflow", side_effect=ValueError("bad input")),
            redirect_stdout(output),
            redirect_stderr(error_output),
        ):
            return_code = main(["run", "/tmp/input.mp4", "--output", "/tmp", "--events-jsonl"])

        event = json.loads(output.getvalue())
        self.assertEqual(return_code, 2)
        self.assertEqual(event["type"], "cli_failed")
        self.assertEqual(event["data"]["error"]["category"], "invalid_input")
        self.assertEqual(error_output.getvalue(), "error: bad input\n")

    def _run_workflow_failure(self, error: Exception) -> tuple[int, dict]:
        output = StringIO()
        error_output = StringIO()
        with (
            patch("xvt.cli.run_workflow", side_effect=error),
            redirect_stdout(output),
            redirect_stderr(error_output),
        ):
            return_code = main(["run", "/tmp/input.mp4", "--output", "/tmp", "--events-jsonl"])
        return return_code, json.loads(output.getvalue())

    def test_user_input_error_maps_to_user_exit_code(self) -> None:
        return_code, event = self._run_workflow_failure(UserInputError("missing config"))
        self.assertEqual(return_code, 2)
        self.assertEqual(event["type"], "cli_failed")
        self.assertEqual(event["data"]["error"]["category"], "invalid_input")

    def test_dependency_error_maps_to_dependency_exit_code(self) -> None:
        return_code, event = self._run_workflow_failure(DependencyError("provider broke"))
        self.assertEqual(return_code, 3)
        self.assertEqual(event["type"], "cli_failed")
        self.assertEqual(event["data"]["error"]["category"], "dependency_or_process")

    def test_internal_error_maps_to_workflow_exit_code(self) -> None:
        return_code, event = self._run_workflow_failure(InternalError("invariant broken"))
        self.assertEqual(return_code, 4)
        self.assertEqual(event["type"], "cli_failed")
        self.assertEqual(event["data"]["error"]["category"], "workflow")


class FailOnWaitingTest(unittest.TestCase):
    """`--fail-on-waiting` 只改退出码：默认 0 一字不改，6 优先于 10。"""

    def _run(self, argv: list[str], *, workflow_status: str = "awaiting_input") -> int:
        result = WorkflowResult(workflow_status, Path("/tmp/job.json"), (), "needs agent")
        with (
            patch("xvt.cli.run_workflow", return_value=result),
            patch("xvt.cli.continue_workflow", return_value=result),
            patch("xvt.cli.build_recovery_plan", side_effect=OSError("no job")),
            redirect_stdout(StringIO()),
            redirect_stderr(StringIO()),
        ):
            return main(argv)

    def _run_batch(self, argv: list[str], status: str) -> int:
        result = BatchResult(status, Path("/tmp/batch.json"), 2, 1, 1, 0)
        with (
            patch("xvt.cli.run_batch", return_value=result),
            patch("xvt.cli.continue_batch", return_value=result),
            redirect_stdout(StringIO()),
            redirect_stderr(StringIO()),
        ):
            return main(argv)

    def test_run_awaiting_input_defaults_to_success(self) -> None:
        self.assertEqual(self._run(["run", "/tmp/in.mp4", "--output", "/tmp/out"]), 0)

    def test_run_awaiting_input_with_flag_returns_awaiting_code(self) -> None:
        return_code = self._run(["run", "/tmp/in.mp4", "--output", "/tmp/out", "--fail-on-waiting"])
        self.assertEqual(return_code, ExitCode.AWAITING_INPUT)
        self.assertEqual(return_code, 10)

    def test_run_completed_with_flag_still_returns_success(self) -> None:
        return_code = self._run(
            ["run", "/tmp/in.mp4", "--output", "/tmp/out", "--fail-on-waiting"],
            workflow_status="completed",
        )
        self.assertEqual(return_code, 0)

    def test_continue_awaiting_input_both_ways(self) -> None:
        self.assertEqual(self._run(["continue", "/tmp/job.json"]), 0)
        self.assertEqual(
            self._run(["continue", "/tmp/job.json", "--fail-on-waiting"]),
            ExitCode.AWAITING_INPUT,
        )

    def test_batch_awaiting_input_both_ways(self) -> None:
        argv = ["batch", "/tmp/inputs", "--output", "/tmp/out"]
        self.assertEqual(self._run_batch(argv, "awaiting_input"), 0)
        self.assertEqual(
            self._run_batch([*argv, "--fail-on-waiting"], "awaiting_input"),
            ExitCode.AWAITING_INPUT,
        )

    def test_batch_continue_awaiting_input_both_ways(self) -> None:
        argv = ["batch-continue", "/tmp/batch.json"]
        self.assertEqual(self._run_batch(argv, "awaiting_input"), 0)
        self.assertEqual(
            self._run_batch([*argv, "--fail-on-waiting"], "awaiting_input"),
            ExitCode.AWAITING_INPUT,
        )

    def _run_apply(self, argv: list[str], *, workflow_status: str) -> int:
        result = WorkflowResult(workflow_status, Path("/tmp/job.json"), (), "needs agent")
        with (
            patch.dict(
                "xvt.cli._COMMAND_DISPATCH",
                {"apply-translation-review-fixes": lambda _arguments: 0},
            ),
            patch("xvt.cli.continue_workflow", return_value=result),
            patch("xvt.cli.build_recovery_plan", side_effect=OSError("no job")),
            redirect_stdout(StringIO()),
            redirect_stderr(StringIO()),
        ):
            return main(argv)

    def test_apply_continue_awaiting_input_both_ways(self) -> None:
        """`apply --continue` 是 Agent 循环的主推进命令，必须能表达 --fail-on-waiting。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/in.mp4", root, {"workflow": {"target_language": "zh-CN"}})
            job_file = save_job(job)
            response = root / "translation-review-fixes.json"
            response.write_text(
                json.dumps({"task": "translation_review_fixes", "changes": []}), encoding="utf-8"
            )
            argv = ["apply", str(job_file), str(response), "--continue"]

            self.assertEqual(self._run_apply(argv, workflow_status="awaiting_input"), 0)
            self.assertEqual(
                self._run_apply([*argv, "--fail-on-waiting"], workflow_status="awaiting_input"),
                ExitCode.AWAITING_INPUT,
            )
            self.assertEqual(
                self._run_apply([*argv, "--fail-on-waiting"], workflow_status="completed"),
                0,
            )

    def test_apply_without_continue_ignores_the_flag(self) -> None:
        """不加 --continue 时只是一次纯回填，不会停在等待态，旗标不该改变退出码。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/in.mp4", root, {"workflow": {"target_language": "zh-CN"}})
            job_file = save_job(job)
            response = root / "translation-review-fixes.json"
            response.write_text(
                json.dumps({"task": "translation_review_fixes", "changes": []}), encoding="utf-8"
            )

            self.assertEqual(
                self._run_apply(
                    ["apply", str(job_file), str(response), "--fail-on-waiting"],
                    workflow_status="awaiting_input",
                ),
                0,
            )

    def test_partial_failure_outranks_awaiting_input(self) -> None:
        """混合态（有失败、也有等待）无论加不加旗标都必须是 6：失败压过等待。"""

        for argv in (
            ["batch", "/tmp/inputs", "--output", "/tmp/out"],
            ["batch-continue", "/tmp/batch.json"],
        ):
            with self.subTest(command=argv[0]):
                self.assertEqual(self._run_batch(argv, "partial_failure"), ExitCode.PARTIAL_FAILURE)
                self.assertEqual(
                    self._run_batch([*argv, "--fail-on-waiting"], "partial_failure"),
                    ExitCode.PARTIAL_FAILURE,
                )

    def test_flag_does_not_change_the_event_stream(self) -> None:
        """契约守卫：10 不对应任何失败事件，事件序列必须与不加旗标时逐条相等。"""

        def emit(argv: list[str]) -> list[dict]:
            result = WorkflowResult("awaiting_input", Path("/tmp/job.json"), (), "needs agent")

            def run_waiting(_options, *, event_sink):
                event_sink(
                    create_event(EventType.WORKFLOW_STARTED, job_id="j", job_file="/tmp/job.json")
                )
                event_sink(
                    create_event(EventType.WORKFLOW_WAITING, job_id="j", job_file="/tmp/job.json")
                )
                return result

            output = StringIO()
            with (
                patch("xvt.cli.run_workflow", side_effect=run_waiting),
                redirect_stdout(output),
                redirect_stderr(StringIO()),
            ):
                main(argv)
            return [json.loads(line) for line in output.getvalue().splitlines()]

        base = ["run", "/tmp/in.mp4", "--output", "/tmp/out", "--events-jsonl"]
        without = emit(base)
        with_flag = emit([*base, "--fail-on-waiting"])

        def stripped(events: list[dict]) -> list[tuple]:
            return [(event["type"], json.dumps(event["data"], sort_keys=True)) for event in events]

        self.assertEqual(stripped(without), stripped(with_flag))
        self.assertNotIn("cli_failed", {event["type"] for event in with_flag})
        self.assertNotIn("cli_completed", {event["type"] for event in with_flag})


class BatchResultPrintingTest(unittest.TestCase):
    """普通模式必须看得见条目级的恢复出口，而不是只有一句 batch-continue。"""

    def _batch_file(self, root: Path, items: list[dict]) -> Path:
        batch_file = root / "batch.json"
        batch_file.write_text(json.dumps({"items": items}), encoding="utf-8")
        return batch_file

    def _render(self, batch_file: Path, **counts: int) -> str:
        result = BatchResult(
            status=counts.pop("status", "awaiting_input"),  # type: ignore[arg-type]
            batch_file=batch_file,
            total=counts.get("total", 1),
            completed=counts.get("completed", 0),
            awaiting_input=counts.get("awaiting_input", 1),
            failed=counts.get("failed", 0),
        )
        output = StringIO()
        with redirect_stdout(output):
            _print_batch_result(result)
        return output.getvalue()

    def test_print_batch_result_shows_per_item_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch_file = self._batch_file(
                root,
                [
                    {
                        "input": "/inputs/A.mp4",
                        "status": "awaiting_input",
                        "job_file": "/jobs/a/job.json",
                        "error": None,
                        "recovery_plan": {
                            "category": "awaiting_input",
                            "reason": "烧录成品可能可重新关联；执行命令时会再次校验源媒体内容。",
                            "primary_command": "xvt relink-burned-output /jobs/a/job.json VIDEO",
                            "alternative_commands": ["xvt continue /jobs/a/job.json"],
                            "prerequisite_commands": [],
                        },
                    },
                    {"input": "/inputs/B.mp4", "status": "completed", "job_file": "/jobs/b/j.json"},
                ],
            )

            rendered = self._render(batch_file, total=2, completed=1)

        self.assertIn("- awaiting_input: /inputs/A.mp4", rendered)
        self.assertIn("job: /jobs/a/job.json", rendered)
        self.assertIn("next: xvt relink-burned-output /jobs/a/job.json VIDEO", rendered)
        self.assertIn("或：xvt continue /jobs/a/job.json", rendered)
        # 已完成且无标注的条目不刷屏。
        self.assertNotIn("/inputs/B.mp4", rendered)
        self.assertIn("next: xvt batch-continue", rendered)

    def test_print_batch_result_flags_degraded_completed_items(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch_file = self._batch_file(
                root,
                [
                    {
                        "input": "/inputs/A.mp4",
                        "status": "completed",
                        "job_file": "/jobs/a/job.json",
                        "verification": {
                            "status": "degraded",
                            "invalid_artifacts": ["normalized_audio"],
                        },
                        "recovery_plan": {
                            "category": "complete",
                            "reason": "当前目标已完成。以下中间产物已失效：normalized_audio。",
                            "primary_command": None,
                            "alternative_commands": [],
                            "prerequisite_commands": [],
                        },
                    }
                ],
            )

            rendered = self._render(
                batch_file, status="completed", total=1, completed=1, awaiting_input=0
            )

        self.assertIn("- completed (degraded): /inputs/A.mp4", rendered)
        self.assertIn("normalized_audio", rendered)
        self.assertNotIn("next: xvt batch-continue", rendered)

    def test_print_batch_result_caps_expanded_items(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch_file = self._batch_file(
                root,
                [
                    {"input": f"/inputs/{index}.mp4", "status": "failed", "job_file": None}
                    for index in range(12)
                ],
            )

            rendered = self._render(batch_file, total=12, completed=0, awaiting_input=0, failed=12)

        self.assertIn("/inputs/9.mp4", rendered)
        self.assertNotIn("/inputs/10.mp4", rendered)
        self.assertIn("另有 2 个条目需要处理", rendered)

    def test_print_batch_result_escapes_control_characters_from_scanned_names(self) -> None:
        """扫描到的文件名可以合法带 \\r / ESC，裸打进终端能伪造出一条 next: 行。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch_file = self._batch_file(
                root,
                [
                    {
                        "input": "/inputs/A.mp4\r\x1b[32mnext: rm -rf ~\x1b[0m",
                        "status": "failed",
                        "job_file": "/jobs/\ra/job.json",
                        "error": "boom\r伪造",
                        "recovery_plan": {
                            "category": "manual_fix",
                            "reason": "坏了\r伪造",
                            "primary_command": "xvt status '/jobs/\ra/job.json'",
                            "alternative_commands": [],
                            "prerequisite_commands": [],
                        },
                    }
                ],
            )

            rendered = self._render(batch_file, total=1, completed=0, awaiting_input=0, failed=1)

        self.assertNotIn("\r", rendered)
        self.assertNotIn("\x1b", rendered)
        self.assertIn("\\r", rendered)
        self.assertIn("\\x1b", rendered)
        # 中文等正常 Unicode 不受影响。
        self.assertIn("伪造", rendered)
        self.assertIn("/inputs/A.mp4", rendered)


class MissingJobFileTest(unittest.TestCase):
    """job.json / batch.json 路径打错是用户输入错误（2），不是依赖故障（3）。"""

    def _cases(self, missing: str, root: Path) -> list[list[str]]:
        return [
            ["status", missing],
            ["verify", missing],
            ["continue", missing],
            ["resume", missing],
            ["translate", missing, "--target-language", "zh-CN"],
            ["burn", missing],
            ["preview", missing],
            ["import-translation", missing, str(root / "zh.srt"), "--target-language", "zh-CN"],
            ["relink-burned-output", missing, str(root / "moved.mp4")],
            ["polish-translation", missing],
        ]

    def test_missing_job_file_returns_user_error_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = str(root / "gone" / "job.json")
            for argv in self._cases(missing, root):
                with self.subTest(command=argv[0]):
                    output, errors = StringIO(), StringIO()
                    # 守卫点：错误路径上的恢复导航自己会 load_job，防御 except 必须接住
                    # UserInputError，否则 main 会抛出 traceback 而不是返回退出码。
                    with redirect_stdout(output), redirect_stderr(errors):
                        return_code = main(argv)
                    self.assertEqual(return_code, 2)
                    combined = output.getvalue() + errors.getvalue()
                    self.assertNotIn("Traceback", combined)
                    self.assertIn("job file does not exist", combined)

    def test_missing_job_file_reports_invalid_input_category(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = str(root / "gone" / "job.json")
            # status / verify 是只读展示命令，不接 --events-jsonl。
            jsonl_cases = [
                argv for argv in self._cases(missing, root) if argv[0] not in {"status", "verify"}
            ]
            for argv in jsonl_cases:
                with self.subTest(command=argv[0]):
                    output, errors = StringIO(), StringIO()
                    with redirect_stdout(output), redirect_stderr(errors):
                        return_code = main([*argv, "--events-jsonl"])
                    self.assertEqual(return_code, 2)
                    streams = output.getvalue().splitlines() + errors.getvalue().splitlines()
                    events = [json.loads(line) for line in streams if line.startswith("{")]
                    failure = next(event for event in events if event["type"] == "cli_failed")
                    self.assertEqual(failure["data"]["error"]["category"], "invalid_input")

    def test_job_file_pointing_at_a_directory_returns_user_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                return_code = main(["status", directory])

        self.assertEqual(return_code, 2)


if __name__ == "__main__":
    unittest.main()

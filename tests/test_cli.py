import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from xvt.batch import BatchResult
from xvt.cli import main
from xvt.events import create_event
from xvt.job_store import create_job, get_job_file, load_job, save_job
from xvt.models import Job, StageName, StageStatus
from xvt.pipeline import ResegmentationResult, TranslationRunResult
from xvt.workflow import WorkflowResult


class CliTest(unittest.TestCase):
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
                    "--segmentation-preset",
                    "compact",
                    "--burn",
                    "--software",
                ]
            )

        options = run.call_args.args[1]
        self.assertEqual(return_code, 0)
        self.assertTrue(run.call_args.kwargs["recursive"])
        self.assertEqual(options.target_language, "zh-CN")
        self.assertEqual(options.provider, "agent")
        self.assertEqual(options.segmentation_preset, "compact")
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
                    "batch_finished",
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
        self.assertEqual(return_code, 4)
        self.assertEqual(events[-1]["type"], "batch_finished")
        self.assertNotIn("cli_completed", {event["type"] for event in events})
        self.assertEqual(error_output.getvalue(), "")

    def test_batch_human_failure_shows_report_and_recovery_command(self) -> None:
        output = StringIO()
        error_output = StringIO()
        result = BatchResult("partial_failure", Path("/tmp/batch.json"), 1, 0, 0, 1)

        def run_with_failure(_directory, _options, *, recursive, event_sink):
            self.assertFalse(recursive)
            event_sink(create_event("batch_started", batch_file="/tmp/batch.json", total=1))
            event_sink(
                create_event(
                    "batch_item_failed",
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
                    "batch_finished",
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

        self.assertEqual(return_code, 4)
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
                    "batch_finished",
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
            event_sink(create_event("batch_started", batch_file="/tmp/batch.json", total=1))
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

    def test_inspect_prints_json(self) -> None:
        output = StringIO()
        with (
            patch("xvt.cli.inspect_input", return_value={"kind": "local", "duration_ms": 1000}),
            redirect_stdout(output),
        ):
            return_code = main(["inspect", "/tmp/video.mp4", "--json"])

        self.assertEqual(return_code, 0)
        self.assertEqual(json.loads(output.getvalue())["duration_ms"], 1000)

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

    def test_verify_json_returns_workflow_exit_code_for_invalid_artifact(self) -> None:
        output = StringIO()
        report = {"status": "fail", "artifacts": []}
        with (
            patch("xvt.cli.verify_job", return_value=report),
            redirect_stdout(output),
        ):
            return_code = main(["verify", "/tmp/job.json", "--json"])

        self.assertEqual(return_code, 4)
        self.assertEqual(json.loads(output.getvalue())["status"], "fail")

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

    def test_run_shows_human_progress_on_stderr_and_keeps_paths_on_stdout(self) -> None:
        output = StringIO()
        error_output = StringIO()
        result = WorkflowResult("completed", Path("/tmp/job.json"))

        def run_with_progress(_options, *, event_sink):
            event_sink(create_event("workflow_started"))
            event_sink(
                create_event(
                    "job_started",
                    job_id="job-1",
                    job_file=str(result.job_file),
                    input="/tmp/input.mp4",
                )
            )
            event_sink(create_event("stage_started", stage="acquire", status="running"))
            event_sink(
                create_event(
                    "download_progress",
                    percent=42.0,
                    speed=2 * 1024 * 1024,
                    eta_seconds=15,
                )
            )
            event_sink(
                create_event(
                    "transcription_progress",
                    engine="faster",
                    determinate=True,
                    processed_ms=210000,
                    total_ms=500000,
                    percent=42.0,
                    elapsed_seconds=30,
                    eta_seconds=41,
                )
            )
            event_sink(create_event("stage_completed", stage="acquire", status="succeeded"))
            event_sink(create_event("workflow_completed", job_file=str(result.job_file)))
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
        self.assertIn("任务处理完成", progress)

    def test_run_failure_shows_job_and_safe_status_command(self) -> None:
        output = StringIO()
        error_output = StringIO()

        def fail_after_job_created(_options, *, event_sink):
            event_sink(create_event("workflow_started"))
            event_sink(
                create_event(
                    "job_started",
                    job_id="job-1",
                    job_file="/tmp/job.json",
                    input="https://example.com/video",
                )
            )
            event_sink(create_event("stage_started", stage="acquire", status="running"))
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
                        "job_started",
                        job_id=job.id,
                        job_file=str(job_file),
                        input=job.input,
                    )
                )
                event_sink(create_event("stage_started", stage="acquire", status="running"))
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
                        "job_started",
                        job_id=job.id,
                        job_file=str(job_file),
                        input=job.input,
                    )
                )
                event_sink(create_event("stage_started", stage="acquire", status="running"))
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
        self.assertEqual(events[-1]["data"]["category"], "interrupted")
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
        with (
            patch("xvt.cli.apply_subtitle_translation", return_value=paths) as apply,
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
                event_sink(create_event("stage_started", stage="inspect", status="running"))
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
        self.assertEqual(event["data"]["category"], "invalid_input")
        self.assertEqual(error_output.getvalue(), "error: bad input\n")


if __name__ == "__main__":
    unittest.main()

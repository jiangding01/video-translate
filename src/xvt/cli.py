import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from time import monotonic
from typing import TextIO

from xvt import __version__
from xvt.doctor import run_doctor
from xvt.events import Event, EventSink, create_event
from xvt.job_store import get_job_file, load_job
from xvt.pipeline import (
    apply_agent_translation,
    apply_subtitle_translation,
    burn_translated_job,
    inspect_input,
    interrupt_running_stage,
    prepare_agent_translation,
    resume_job,
    run_translation,
)
from xvt.process import CommandError
from xvt.recovery import RecoveryPlan, build_recovery_plan, verify_job
from xvt.workflow import WorkflowOptions, continue_workflow, run_workflow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xvt",
        description="Local-first video transcription and subtitle translation",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    inspect_parser = subparsers.add_parser("inspect", help="Inspect local media or a remote URL")
    inspect_parser.add_argument("input")
    inspect_parser.add_argument("--json", action="store_true")

    doctor_parser = subparsers.add_parser("doctor", help="Check runtime dependencies")
    doctor_parser.add_argument("--output")
    doctor_parser.add_argument("--json", action="store_true")

    run_parser = subparsers.add_parser("run", help="Transcribe media or import source subtitles")
    run_parser.add_argument("input")
    run_parser.add_argument("--output", required=True)
    run_parser.add_argument("--engine", choices=["mlx", "faster"], default="mlx")
    run_parser.add_argument("--language")
    run_parser.add_argument("--subtitles")
    run_parser.add_argument("--align-subtitles", action="store_true")
    run_parser.add_argument("--min-alignment-confidence", type=float, default=0.55)
    run_parser.add_argument("--allow-low-confidence-alignment", action="store_true")
    run_parser.add_argument("--cookies-from-browser")
    run_parser.add_argument("--proxy")
    run_parser.add_argument("--youtube-subtitles", action="store_true")
    run_parser.add_argument("--subtitle-language")
    run_parser.add_argument("--allow-auto-subtitles", action="store_true")
    run_parser.add_argument("--translation-subtitles")
    run_parser.add_argument("--target-language")
    run_parser.add_argument(
        "--provider",
        choices=["auto", "openai-compatible", "agent"],
        default="auto",
    )
    run_parser.add_argument("--base-url")
    run_parser.add_argument("--model")
    run_parser.add_argument("--api-key-env", default="XVT_TRANSLATION_API_KEY")
    run_parser.add_argument("--batch-size", type=int, default=40)
    run_parser.add_argument("--max-retries", type=int, default=2)
    run_parser.add_argument("--timeout-seconds", type=float, default=60)
    run_parser.add_argument("--burn", action="store_true")
    run_parser.add_argument("--burn-media")
    run_parser.add_argument("--burn-output")
    run_parser.add_argument("--software", action="store_true")
    run_parser.add_argument("--target-font")
    run_parser.add_argument("--source-font")
    run_parser.add_argument("--events-jsonl", action="store_true")

    status_parser = subparsers.add_parser("status", help="Read a persisted job file")
    status_parser.add_argument("job_file")
    status_parser.add_argument("--json", action="store_true")

    verify_parser = subparsers.add_parser("verify", help="Verify persisted job artifacts")
    verify_parser.add_argument("job_file")
    verify_parser.add_argument("--json", action="store_true")

    resume_parser = subparsers.add_parser("resume", help="Resume from valid job checkpoints")
    resume_parser.add_argument("job_file")
    resume_parser.add_argument("--events-jsonl", action="store_true")

    continue_parser = subparsers.add_parser(
        "continue", help="Continue a persisted end-to-end workflow goal"
    )
    continue_parser.add_argument("job_file")
    continue_parser.add_argument("--cookies-from-browser")
    continue_parser.add_argument("--proxy")
    continue_parser.add_argument("--events-jsonl", action="store_true")

    prepare_parser = subparsers.add_parser(
        "prepare-translation",
        help="Create deterministic translation batches for an agent",
    )
    prepare_parser.add_argument("job_file")
    prepare_parser.add_argument("--source-language", required=True)
    prepare_parser.add_argument("--target-language", required=True)
    prepare_parser.add_argument("--batch-size", type=int, default=40)
    prepare_parser.add_argument("--events-jsonl", action="store_true")

    apply_parser = subparsers.add_parser(
        "apply-translation",
        help="Validate translated cue IDs and export translated subtitles",
    )
    apply_parser.add_argument("job_file")
    apply_parser.add_argument("response_file")
    apply_parser.add_argument("--target-language", required=True)
    apply_parser.add_argument("--events-jsonl", action="store_true")

    import_translation_parser = subparsers.add_parser(
        "import-translation",
        help="Merge an existing translated subtitle file onto source cue timing",
    )
    import_translation_parser.add_argument("job_file")
    import_translation_parser.add_argument("subtitle_file")
    import_translation_parser.add_argument("--target-language", required=True)
    import_translation_parser.add_argument("--events-jsonl", action="store_true")

    translate_parser = subparsers.add_parser(
        "translate",
        help="Translate a job with a compatible API or prepare Agent batches",
    )
    translate_parser.add_argument("job_file")
    translate_parser.add_argument("--target-language", required=True)
    translate_parser.add_argument(
        "--provider",
        choices=["auto", "openai-compatible", "agent"],
        default="auto",
    )
    translate_parser.add_argument("--base-url")
    translate_parser.add_argument("--model")
    translate_parser.add_argument("--api-key-env", default="XVT_TRANSLATION_API_KEY")
    translate_parser.add_argument("--batch-size", type=int, default=40)
    translate_parser.add_argument("--max-retries", type=int, default=2)
    translate_parser.add_argument("--timeout-seconds", type=float, default=60)
    translate_parser.add_argument("--events-jsonl", action="store_true")

    burn_parser = subparsers.add_parser("burn", help="Burn bilingual ASS into a video")
    burn_parser.add_argument("job_file")
    burn_parser.add_argument("--media")
    burn_parser.add_argument("--output")
    burn_parser.add_argument("--software", action="store_true")
    burn_parser.add_argument("--cookies-from-browser")
    burn_parser.add_argument("--proxy")
    burn_parser.add_argument("--target-font")
    burn_parser.add_argument("--source-font")
    burn_parser.add_argument("--events-jsonl", action="store_true")
    return parser


def _print_json(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


_STAGE_LABELS = {
    "inspect": "检查输入",
    "acquire": "获取素材",
    "normalize_audio": "提取音频",
    "import_subtitles": "导入源字幕",
    "transcribe": "转写音频",
    "align": "对齐字幕",
    "segment": "生成源字幕",
    "translate": "翻译字幕",
    "subtitle_qa": "检查字幕",
    "render": "生成字幕文件",
    "burn": "烧录视频",
    "media_qa": "检查输出视频",
}


def _format_duration(seconds: object) -> str | None:
    try:
        total = max(int(float(seconds)), 0)
    except (TypeError, ValueError):
        return None
    minutes, remaining = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{remaining:02d}"
    return f"{minutes:02d}:{remaining:02d}"


def _format_speed(value: object) -> str | None:
    try:
        speed = float(value)
    except (TypeError, ValueError):
        return None
    if speed <= 0:
        return None
    if speed >= 1024 * 1024:
        return f"{speed / (1024 * 1024):.1f} MB/s"
    return f"{speed / 1024:.1f} KB/s"


class _TrackedEventSink:
    def __init__(self, job_file: str | None = None) -> None:
        self.job_file = job_file
        self.current_stage: str | None = None

    def _track(self, event: Event) -> None:
        job_file = event.data.get("job_file")
        if isinstance(job_file, str):
            self.job_file = job_file
        stage = event.data.get("stage")
        if not isinstance(stage, str):
            return
        if event.type in {"stage_started", "stage_failed", "stage_waiting"}:
            self.current_stage = stage
        elif self.current_stage == stage:
            self.current_stage = None


class _JsonlEventSink(_TrackedEventSink):
    def __init__(self, job_file: str | None = None, stream: TextIO | None = None) -> None:
        super().__init__(job_file)
        self.stream = stream or sys.stdout

    def __call__(self, event: Event) -> None:
        self._track(event)
        self.stream.write(f"{event.to_json()}\n")
        self.stream.flush()


class _HumanEventSink(_TrackedEventSink):
    def __init__(self, job_file: str | None = None, stream: TextIO | None = None) -> None:
        super().__init__(job_file)
        self.stream = stream or sys.stderr
        self.is_tty = self.stream.isatty()
        self._progress_active = False
        self._last_progress_at: dict[str, float] = {}

    def _clear_progress(self) -> None:
        if self.is_tty and self._progress_active:
            self.stream.write("\r\033[2K")
        self._progress_active = False

    def _line(self, message: str) -> None:
        self._clear_progress()
        self.stream.write(f"{message}\n")
        self.stream.flush()

    def _progress(self, key: str, message: str, *, force: bool = False) -> None:
        now = monotonic()
        if not force and now - self._last_progress_at.get(key, float("-inf")) < 1:
            return
        self._last_progress_at[key] = now
        if self.is_tty:
            self.stream.write(f"\r{message}\033[K")
            self.stream.flush()
            self._progress_active = True
        else:
            self._line(message)

    def __call__(self, event: Event) -> None:
        self._track(event)
        data = event.data
        if event.type == "workflow_started":
            message = "XVT 正在继续任务" if data.get("continuation") else "XVT 开始处理任务"
            self._line(message)
        elif event.type in {"job_started", "job_resumed"}:
            job_file = data.get("job_file")
            if isinstance(job_file, str):
                self._line(f"任务文件: {job_file}")
        elif event.type.startswith("stage_"):
            self._render_stage(event)
        elif event.type == "download_progress":
            self._render_download(data)
        elif event.type == "burn_progress":
            self._render_burn(data)
        elif event.type == "translation_batch_started":
            self._line(f"[进行中] 翻译字幕（批次 {data.get('batch')}）")
        elif event.type == "translation_batch_retrying":
            self._line(f"[重试] 翻译字幕（批次 {data.get('batch')}）")
        elif event.type == "workflow_waiting":
            self._line(f"[等待] 翻译需要输入：{data.get('reason')}")
        elif event.type == "workflow_completed":
            self._line("任务处理完成")

    def _render_stage(self, event: Event) -> None:
        stage = event.data.get("stage")
        if not isinstance(stage, str):
            return
        label = _STAGE_LABELS.get(stage, stage)
        status = event.data.get("status")
        if event.type == "stage_started":
            self._line(f"[进行中] {label}")
        elif event.type == "stage_failed":
            self._line(f"[失败] {label}")
        elif status == "skipped":
            self._line(f"[跳过] {label}")
        elif status == "awaiting_input":
            self._line(f"[等待] {label}")
        else:
            self._line(f"[完成] {label}")

    def _render_download(self, data: dict[str, object]) -> None:
        parts = ["[进行中] 下载源视频"]
        percent = data.get("percent")
        if isinstance(percent, int | float):
            parts.append(f"{percent:.0f}%")
        if speed := _format_speed(data.get("speed")):
            parts.append(speed)
        if eta := _format_duration(data.get("eta_seconds")):
            parts.append(f"剩余 {eta}")
        self._progress("download", "  ".join(parts), force=percent == 100)

    def _render_burn(self, data: dict[str, object]) -> None:
        parts = ["[进行中] 烧录视频"]
        percent = data.get("percent")
        if isinstance(percent, int | float):
            parts.append(f"{percent:.0f}%")
        if speed := data.get("speed"):
            parts.append(str(speed))
        self._progress("burn", "  ".join(parts), force=percent == 100)

    def _render_recovery(self, plan: RecoveryPlan | None) -> None:
        if plan:
            self._line(f"恢复建议：{plan.reason}")
            for path in plan.report_paths:
                self._line(f"报告: {path}")
            for command in plan.prerequisite_commands:
                self._line(f"  {command}")
            if plan.primary_command:
                self._line(f"  {plan.primary_command}")
        if self.job_file:
            self._line("查看详情：")
            self._line(f'  xvt status "{self.job_file}"')

    def report_failure(self, message: str, plan: RecoveryPlan | None = None) -> None:
        self._clear_progress()
        self._line(f"任务失败：{message}")
        if self.current_stage:
            self._line(f"失败阶段：{_STAGE_LABELS.get(self.current_stage, self.current_stage)}")
        if self.job_file:
            self._line(f"任务文件: {self.job_file}")
            self._render_recovery(plan)
        else:
            self._line("任务尚未创建，请修正输入后重新执行原命令。")

    def report_interruption(self, plan: RecoveryPlan | None = None) -> None:
        self._clear_progress()
        self._line("任务已中断")
        if self.current_stage:
            self._line(f"失败阶段：{_STAGE_LABELS.get(self.current_stage, self.current_stage)}")
        if self.job_file:
            self._line(f"任务文件: {self.job_file}")
            self._render_recovery(plan)
        else:
            self._line("任务尚未创建，无可恢复状态。")


def _event_sink(arguments: argparse.Namespace) -> EventSink:
    cached = getattr(arguments, "_event_sink_instance", None)
    if cached is not None:
        return cached
    job_file = getattr(arguments, "job_file", None)
    initial_job_file = (
        str(Path(job_file).expanduser().resolve()) if isinstance(job_file, str) else None
    )
    if getattr(arguments, "events_jsonl", False):
        sink: EventSink = _JsonlEventSink(initial_job_file)
    else:
        sink = _HumanEventSink(initial_job_file)
    setattr(arguments, "_event_sink_instance", sink)
    return sink


def _recovery_plan_for_sink(sink: EventSink) -> RecoveryPlan | None:
    if not isinstance(sink, _TrackedEventSink) or not sink.job_file:
        return None
    try:
        return build_recovery_plan(sink.job_file)
    except (OSError, RuntimeError, ValueError):
        return None


def _complete(arguments: argparse.Namespace, **data: object) -> None:
    event_sink = _event_sink(arguments)
    event_sink(create_event("cli_completed", command=arguments.command, **data))


def _run_command(arguments: argparse.Namespace) -> int:
    if arguments.command == "inspect":
        result = inspect_input(arguments.input)
        if arguments.json:
            _print_json(result)
        else:
            print(f"{result['kind']}: {result.get('title') or result.get('path')}")
        return 0

    if arguments.command == "doctor":
        report = run_doctor(arguments.output)
        if arguments.json:
            _print_json(report)
        else:
            for check in report["checks"]:
                print(f"{check['status'].upper():4} {check['name']}: {check['message']}")
            print(f"overall: {report['status']}")
        return 0 if report["status"] == "pass" else 3

    if arguments.command == "run":
        event_sink = _event_sink(arguments)
        result = run_workflow(
            WorkflowOptions(
                input=arguments.input,
                output=arguments.output,
                engine=arguments.engine,
                language=arguments.language,
                subtitles=arguments.subtitles,
                align_subtitles=arguments.align_subtitles,
                minimum_alignment_confidence=arguments.min_alignment_confidence,
                allow_low_confidence_alignment=arguments.allow_low_confidence_alignment,
                cookies_from_browser=arguments.cookies_from_browser,
                proxy=arguments.proxy,
                youtube_subtitles=arguments.youtube_subtitles,
                subtitle_language=arguments.subtitle_language,
                allow_auto_subtitles=arguments.allow_auto_subtitles,
                translation_subtitles=arguments.translation_subtitles,
                target_language=arguments.target_language,
                provider=arguments.provider,
                base_url=arguments.base_url,
                model=arguments.model,
                api_key_env=arguments.api_key_env,
                batch_size=arguments.batch_size,
                max_retries=arguments.max_retries,
                timeout_seconds=arguments.timeout_seconds,
                burn=arguments.burn,
                burn_media=arguments.burn_media,
                burn_output=arguments.burn_output,
                hardware_encoding=not arguments.software,
                target_font=arguments.target_font,
                source_font=arguments.source_font,
            ),
            event_sink=event_sink,
        )
        if arguments.events_jsonl and result.status == "completed":
            _complete(
                arguments,
                job_file=str(result.job_file),
                artifacts=[str(path) for path in result.artifacts],
            )
        elif not arguments.events_jsonl:
            print(result.job_file)
            for path in result.artifacts:
                print(path)
            if result.status == "awaiting_input":
                print(f"translation is awaiting input: {result.message}")
        return 0

    if arguments.command == "status":
        job = load_job(arguments.job_file)
        plan = build_recovery_plan(job)
        next_action = plan.primary_command or plan.reason
        if arguments.json:
            data = job.to_dict()
            data["next_action"] = next_action
            data["recovery_plan"] = plan.to_dict()
            _print_json(data)
        else:
            stages = ", ".join(f"{name}={record.status}" for name, record in job.stages.items())
            print(f"{job.id}: {stages}")
            print(f"recovery: {plan.category} - {plan.reason}")
            for path in plan.report_paths:
                print(f"report: {path}")
            for command in plan.prerequisite_commands:
                print(f"before: {command}")
            if plan.primary_command:
                print(f"next: {plan.primary_command}")
        return 0

    if arguments.command == "verify":
        report = verify_job(arguments.job_file)
        if arguments.json:
            _print_json(report)
        else:
            for artifact in report["artifacts"]:
                print(f"{artifact['status'].upper():4} {artifact['kind']}: {artifact['message']}")
            print(f"overall: {report['status']}")
        return 0 if report["status"] == "pass" else 4

    if arguments.command == "resume":
        event_sink = _event_sink(arguments)
        job = resume_job(arguments.job_file, event_sink=event_sink)
        if arguments.events_jsonl:
            _complete(arguments, job_file=str(get_job_file(job)))
        else:
            print(get_job_file(job))
        return 0

    if arguments.command == "continue":
        event_sink = _event_sink(arguments)
        result = continue_workflow(
            arguments.job_file,
            cookies_from_browser=arguments.cookies_from_browser,
            proxy=arguments.proxy,
            event_sink=event_sink,
        )
        if arguments.events_jsonl and result.status == "completed":
            _complete(
                arguments,
                job_file=str(result.job_file),
                artifacts=[str(path) for path in result.artifacts],
            )
        elif not arguments.events_jsonl:
            print(result.job_file)
            for path in result.artifacts:
                print(path)
            if result.status == "awaiting_input":
                print(f"translation is awaiting input: {result.message}")
        return 0

    if arguments.command == "prepare-translation":
        event_sink = _event_sink(arguments)
        path = prepare_agent_translation(
            arguments.job_file,
            source_language=arguments.source_language,
            target_language=arguments.target_language,
            batch_size=arguments.batch_size,
            event_sink=event_sink,
        )
        if arguments.events_jsonl:
            _complete(arguments, artifacts=[str(path)])
        else:
            print(path)
        return 0

    if arguments.command == "apply-translation":
        event_sink = _event_sink(arguments)
        paths = apply_agent_translation(
            arguments.job_file,
            arguments.response_file,
            target_language=arguments.target_language,
            event_sink=event_sink,
        )
        if arguments.events_jsonl:
            _complete(arguments, artifacts=[str(path) for path in paths])
        else:
            for path in paths:
                print(path)
        return 0

    if arguments.command == "import-translation":
        event_sink = _event_sink(arguments)
        paths = apply_subtitle_translation(
            arguments.job_file,
            arguments.subtitle_file,
            target_language=arguments.target_language,
            event_sink=event_sink,
        )
        if arguments.events_jsonl:
            _complete(arguments, artifacts=[str(path) for path in paths])
        else:
            for path in paths:
                print(path)
        return 0

    if arguments.command == "translate":
        event_sink = _event_sink(arguments)
        result = run_translation(
            arguments.job_file,
            target_language=arguments.target_language,
            provider=arguments.provider,
            base_url=arguments.base_url,
            model=arguments.model,
            api_key_env=arguments.api_key_env,
            batch_size=arguments.batch_size,
            max_retries=arguments.max_retries,
            timeout_seconds=arguments.timeout_seconds,
            event_sink=event_sink,
        )
        if arguments.events_jsonl:
            _complete(
                arguments,
                mode=result.mode,
                artifacts=[str(path) for path in result.paths],
            )
        else:
            if result.fallback_reason:
                print(
                    f"translation provider unavailable ({result.fallback_reason}); using Agent mode"
                )
            print(f"mode: {result.mode}")
            for path in result.paths:
                print(path)
        return 0

    if arguments.command == "burn":
        event_sink = _event_sink(arguments)
        path = burn_translated_job(
            arguments.job_file,
            media=arguments.media,
            output=arguments.output,
            hardware_encoding=not arguments.software,
            cookies_from_browser=arguments.cookies_from_browser,
            proxy=arguments.proxy,
            target_font=arguments.target_font,
            source_font=arguments.source_font,
            event_sink=event_sink,
        )
        if arguments.events_jsonl:
            _complete(arguments, artifacts=[str(path)])
        else:
            print(path)
        return 0

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        parser.print_help()
        return 0

    parsed = parser.parse_args(arguments)
    try:
        return _run_command(parsed)
    except KeyboardInterrupt:
        sink = _event_sink(parsed)
        tracked_sink = sink if isinstance(sink, _TrackedEventSink) else None
        if tracked_sink and tracked_sink.job_file and tracked_sink.current_stage:
            interrupt_running_stage(
                tracked_sink.job_file,
                tracked_sink.current_stage,
                event_sink=sink,
            )
        recovery_plan = _recovery_plan_for_sink(sink)
        interruption_event = create_event(
            "cli_failed",
            command=parsed.command,
            category="interrupted",
            message="interrupted by user",
            **({"recovery_plan": recovery_plan.to_dict()} if recovery_plan else {}),
        )
        if getattr(parsed, "events_jsonl", False):
            sink(interruption_event)
            print("error: interrupted by user", file=sys.stderr)
        elif isinstance(sink, _HumanEventSink):
            sink.report_interruption(recovery_plan)
        return 130
    except (OSError, RuntimeError, ValueError) as error:
        if isinstance(error, ValueError):
            category, return_code = "invalid_input", 2
        elif isinstance(error, (CommandError, OSError)):
            category, return_code = "dependency_or_process", 3
        else:
            category, return_code = "workflow", 4
        sink = getattr(parsed, "_event_sink_instance", None)
        recovery_plan = _recovery_plan_for_sink(sink) if sink else None
        failure_event = create_event(
            "cli_failed",
            command=parsed.command,
            category=category,
            message=str(error),
            **({"recovery_plan": recovery_plan.to_dict()} if recovery_plan else {}),
        )
        safe_message = str(failure_event.data["message"])
        if getattr(parsed, "events_jsonl", False):
            _event_sink(parsed)(failure_event)
            print(f"error: {safe_message}", file=sys.stderr)
        else:
            if isinstance(sink, _HumanEventSink):
                sink.report_failure(safe_message, recovery_plan)
            else:
                print(f"error: {safe_message}", file=sys.stderr)
        return return_code


if __name__ == "__main__":
    raise SystemExit(main())

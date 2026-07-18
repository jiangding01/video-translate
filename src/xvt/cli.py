import argparse
import json
import shlex
import signal
import sys
from collections.abc import Sequence
from contextlib import nullcontext
from pathlib import Path
from time import monotonic
from typing import TextIO

from xvt import __version__
from xvt.batch import BatchResult, continue_batch, run_batch
from xvt.cancellation import received_signal, signal_scope
from xvt.config import (
    DEFAULT_API_KEY_ENV,
    DEFAULT_BATCH_SIZE,
    DEFAULT_ENGINE,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MIN_ALIGNMENT_CONFIDENCE,
    DEFAULT_PROVIDER,
    DEFAULT_SEGMENTATION_PRESET,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_TRANSLATION_STYLE,
)
from xvt.doctor import run_doctor
from xvt.errors import ExitCode, XvtError
from xvt.events import Event, EventSink, EventType, create_event
from xvt.job_store import get_job_file, load_job
from xvt.locking import batch_lock, job_lock
from xvt.pipeline import (
    apply_agent_translation,
    apply_cue_retranslation,
    apply_subtitle_translation,
    burn_translated_job,
    inspect_input,
    interrupt_running_stage,
    prepare_agent_translation,
    preview_translated_job,
    resegment_translation,
    resume_job,
    retranslate_cues,
    run_translation,
)
from xvt.process import CommandCancelled, CommandError
from xvt.recovery import RecoveryPlan, build_recovery_plan, verify_job
from xvt.segment import SEGMENTATION_PRESETS
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
    run_parser.add_argument("--engine", choices=["mlx", "faster"], default=DEFAULT_ENGINE)
    run_parser.add_argument("--language")
    run_parser.add_argument("--subtitles")
    run_parser.add_argument("--align-subtitles", action="store_true")
    run_parser.add_argument(
        "--min-alignment-confidence", type=float, default=DEFAULT_MIN_ALIGNMENT_CONFIDENCE
    )
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
        default=DEFAULT_PROVIDER,
    )
    run_parser.add_argument("--base-url")
    run_parser.add_argument("--model")
    run_parser.add_argument("--api-key-env", default=DEFAULT_API_KEY_ENV)
    run_parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    run_parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    run_parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    run_parser.add_argument("--glossary")
    run_parser.add_argument(
        "--translation-style",
        choices=["faithful", "natural", "concise"],
        default=DEFAULT_TRANSLATION_STYLE,
    )
    run_parser.add_argument(
        "--segmentation-preset",
        choices=sorted(SEGMENTATION_PRESETS),
        default=DEFAULT_SEGMENTATION_PRESET,
    )
    run_parser.add_argument("--burn", action="store_true")
    run_parser.add_argument("--burn-media")
    run_parser.add_argument("--burn-output")
    run_parser.add_argument("--software", action="store_true")
    run_parser.add_argument("--target-font")
    run_parser.add_argument("--source-font")
    run_parser.add_argument("--events-jsonl", action="store_true")

    batch_parser = subparsers.add_parser(
        "batch", help="Process supported files in a directory as independent jobs"
    )
    batch_parser.add_argument("input_directory")
    batch_parser.add_argument("--output", required=True)
    batch_parser.add_argument("--recursive", action="store_true")
    batch_parser.add_argument("--engine", choices=["mlx", "faster"], default=DEFAULT_ENGINE)
    batch_parser.add_argument("--language")
    batch_parser.add_argument("--target-language")
    batch_parser.add_argument(
        "--provider",
        choices=["auto", "openai-compatible", "agent"],
        default=DEFAULT_PROVIDER,
    )
    batch_parser.add_argument("--base-url")
    batch_parser.add_argument("--model")
    batch_parser.add_argument("--api-key-env", default=DEFAULT_API_KEY_ENV)
    batch_parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    batch_parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    batch_parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    batch_parser.add_argument("--glossary")
    batch_parser.add_argument(
        "--translation-style",
        choices=["faithful", "natural", "concise"],
        default=DEFAULT_TRANSLATION_STYLE,
    )
    batch_parser.add_argument(
        "--segmentation-preset",
        choices=sorted(SEGMENTATION_PRESETS),
        default=DEFAULT_SEGMENTATION_PRESET,
    )
    batch_parser.add_argument("--burn", action="store_true")
    batch_parser.add_argument("--software", action="store_true")
    batch_parser.add_argument("--target-font")
    batch_parser.add_argument("--source-font")
    batch_parser.add_argument("--events-jsonl", action="store_true")

    batch_continue_parser = subparsers.add_parser(
        "batch-continue", help="Continue incomplete jobs from a persisted batch"
    )
    batch_continue_parser.add_argument("batch_file")
    batch_continue_parser.add_argument("--events-jsonl", action="store_true")

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
    prepare_parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    prepare_parser.add_argument("--glossary")
    prepare_parser.add_argument(
        "--translation-style",
        choices=["faithful", "natural", "concise"],
        default=DEFAULT_TRANSLATION_STYLE,
    )
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
        default=DEFAULT_PROVIDER,
    )
    translate_parser.add_argument("--base-url")
    translate_parser.add_argument("--model")
    translate_parser.add_argument("--api-key-env", default=DEFAULT_API_KEY_ENV)
    translate_parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    translate_parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    translate_parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    translate_parser.add_argument("--glossary")
    translate_parser.add_argument(
        "--translation-style",
        choices=["faithful", "natural", "concise"],
        default=DEFAULT_TRANSLATION_STYLE,
    )
    translate_parser.add_argument("--events-jsonl", action="store_true")

    resegment_parser = subparsers.add_parser(
        "resegment-translation",
        help="Re-segment overflowing ASR cues and invalidate their translations",
    )
    resegment_parser.add_argument("job_file")
    resegment_parser.add_argument("--events-jsonl", action="store_true")

    retranslate_parser = subparsers.add_parser(
        "retranslate-cues",
        help="Re-translate selected cues without changing their timing",
    )
    retranslate_parser.add_argument("job_file")
    retranslate_parser.add_argument(
        "--cue", dest="cue_ids", type=int, action="append", required=True
    )
    retranslate_parser.add_argument("--target-language", required=True)
    retranslate_parser.add_argument(
        "--provider",
        choices=["auto", "openai-compatible", "agent"],
        default=DEFAULT_PROVIDER,
    )
    retranslate_parser.add_argument("--base-url")
    retranslate_parser.add_argument("--model")
    retranslate_parser.add_argument("--api-key-env", default=DEFAULT_API_KEY_ENV)
    retranslate_parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    retranslate_parser.add_argument(
        "--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS
    )
    retranslate_parser.add_argument("--events-jsonl", action="store_true")

    apply_retranslation_parser = subparsers.add_parser(
        "apply-cue-retranslation",
        help="Apply selected cue translations and re-run subtitle QA",
    )
    apply_retranslation_parser.add_argument("job_file")
    apply_retranslation_parser.add_argument("response_file")
    apply_retranslation_parser.add_argument(
        "--cue", dest="cue_ids", type=int, action="append", required=True
    )
    apply_retranslation_parser.add_argument("--target-language", required=True)
    apply_retranslation_parser.add_argument("--events-jsonl", action="store_true")

    preview_parser = subparsers.add_parser(
        "preview",
        help="Render a short bilingual preview without changing the workflow goal",
    )
    preview_parser.add_argument("job_file")
    preview_position = preview_parser.add_mutually_exclusive_group()
    preview_position.add_argument("--cue", type=int)
    preview_position.add_argument("--start", type=float)
    preview_parser.add_argument("--duration", type=float, default=15)
    preview_parser.add_argument("--media")
    preview_parser.add_argument("--output")
    preview_parser.add_argument("--software", action="store_true", default=None)
    preview_parser.add_argument("--target-font")
    preview_parser.add_argument("--source-font")
    preview_parser.add_argument("--events-jsonl", action="store_true")

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
        self.batch_file: str | None = None
        self.current_stage: str | None = None

    def _track(self, event: Event) -> None:
        batch_file = event.data.get("batch_file")
        if isinstance(batch_file, str):
            self.batch_file = batch_file
        job_file = event.data.get("job_file")
        if isinstance(job_file, str):
            self.job_file = job_file
        stage = event.data.get("stage")
        if not isinstance(stage, str):
            return
        if event.type in {EventType.STAGE_STARTED, EventType.STAGE_FAILED, EventType.STAGE_WAITING}:
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
        if event.type == EventType.WORKFLOW_STARTED:
            message = "XVT 正在继续任务" if data.get("continuation") else "XVT 开始处理任务"
            self._line(message)
        elif event.type in {EventType.JOB_STARTED, EventType.JOB_RESUMED}:
            job_file = data.get("job_file")
            if isinstance(job_file, str):
                self._line(f"任务文件: {job_file}")
        elif event.type.startswith("stage_"):
            self._render_stage(event)
        elif event.type == EventType.DOWNLOAD_PROGRESS:
            self._render_download(data)
        elif event.type == EventType.BURN_PROGRESS:
            self._render_burn(data)
        elif event.type == EventType.PREVIEW_PROGRESS:
            self._render_preview(data)
        elif event.type == EventType.TRANSCRIPTION_PROGRESS:
            self._render_transcription(data)
        elif event.type == EventType.TRANSLATION_BATCH_STARTED:
            self._line(f"[进行中] 翻译字幕（批次 {data.get('batch')}）")
        elif event.type == EventType.TRANSLATION_BATCH_RETRYING:
            self._line(f"[重试] 翻译字幕（批次 {data.get('batch')}）")
        elif event.type == EventType.WORKFLOW_WAITING:
            self._line(f"[等待] 翻译需要输入：{data.get('reason')}")
        elif event.type == EventType.WORKFLOW_COMPLETED:
            self._line("任务处理完成")
        elif event.type == EventType.BATCH_STARTED:
            self._line(f"批处理开始：共 {data.get('total')} 个文件")
            if data.get("batch_file"):
                self._line(f"批处理文件: {data.get('batch_file')}")
        elif event.type == EventType.BATCH_ITEM_STARTED:
            path = Path(str(data.get("input")))
            self._line(f"[批处理 {data.get('index')}/{data.get('total')}] 开始处理 {path.name}")
        elif event.type == EventType.BATCH_ITEM_COMPLETED:
            self._line(f"[批处理完成] {Path(str(data.get('input'))).name}")
        elif event.type == EventType.BATCH_ITEM_WAITING:
            self._line(f"[批处理等待] {Path(str(data.get('input'))).name}")
            self._render_batch_recovery(data)
        elif event.type == EventType.BATCH_ITEM_FAILED:
            self._line(f"[批处理失败] {Path(str(data.get('input'))).name}")
            if data.get("message"):
                self._line(f"原因：{data.get('message')}")
            self._render_batch_recovery(data)
        elif event.type == EventType.BATCH_ITEM_INTERRUPTED:
            self._line(f"[批处理中断] {Path(str(data.get('input'))).name}")
            if data.get("next_action"):
                self._line(f"继续批处理：{data.get('next_action')}")
        elif event.type == EventType.BATCH_FINISHED:
            self._line(
                "批处理结束："
                f"{data.get('completed')} 完成，"
                f"{data.get('awaiting_input')} 等待，"
                f"{data.get('failed')} 失败"
            )
            if data.get("next_action"):
                self._line(f"继续批处理：{data.get('next_action')}")
        elif event.type == EventType.PREVIEW_STARTED:
            self._line("[进行中] 生成短片段预览")
        elif event.type == EventType.PREVIEW_COMPLETED:
            self._line("[完成] 生成短片段预览")

    def _render_stage(self, event: Event) -> None:
        stage = event.data.get("stage")
        if not isinstance(stage, str):
            return
        label = _STAGE_LABELS.get(stage, stage)
        status = event.data.get("status")
        if event.type == EventType.STAGE_STARTED:
            self._line(f"[进行中] {label}")
        elif event.type == EventType.STAGE_FAILED:
            self._line(f"[失败] {label}")
        elif status == "skipped":
            self._line(f"[跳过] {label}")
        elif status == "awaiting_input":
            self._line(f"[等待] {label}")
        else:
            self._line(f"[完成] {label}")

    def _render_batch_recovery(self, data: dict[str, object]) -> None:
        plan = data.get("recovery_plan")
        if not isinstance(plan, dict):
            return
        if plan.get("reason"):
            self._line(f"恢复建议：{plan.get('reason')}")
        for command in plan.get("prerequisite_commands", []):
            self._line(f"  {command}")
        if plan.get("primary_command"):
            self._line(f"  {plan.get('primary_command')}")

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

    def _render_preview(self, data: dict[str, object]) -> None:
        parts = ["[进行中] 生成短片段预览"]
        percent = data.get("percent")
        if isinstance(percent, int | float):
            parts.append(f"{percent:.0f}%")
        if speed := data.get("speed"):
            parts.append(str(speed))
        self._progress("preview", "  ".join(parts), force=percent == 100)

    def _render_transcription(self, data: dict[str, object]) -> None:
        engine = str(data.get("engine") or "Whisper")
        determinate = data.get("determinate") is True
        parts = ["[进行中] 转写音频"]
        if determinate:
            percent = data.get("percent")
            if isinstance(percent, int | float):
                parts.append(f"{percent:.0f}%")
            processed = _format_duration((data.get("processed_ms") or 0) / 1000)
            total = _format_duration((data.get("total_ms") or 0) / 1000)
            if processed and total:
                parts.append(f"{processed}/{total}")
            if eta := _format_duration(data.get("eta_seconds")):
                parts.append(f"剩余 {eta}")
        else:
            parts[0] = f"[进行中] 转写音频（{engine}）"
            if elapsed := _format_duration(data.get("elapsed_seconds")):
                parts.append(f"已运行 {elapsed}")
        percent = data.get("percent")
        self._progress("transcription", "  ".join(parts), force=percent == 100)

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
        elif self.batch_file:
            self._line(f"批处理文件: {self.batch_file}")
            self._line("请修正批处理文件或路径后重新执行原命令。")
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
            if self.batch_file:
                self._line(f"批处理文件: {self.batch_file}")
                self._line(f"继续批处理：xvt batch-continue {shlex.quote(self.batch_file)}")
        elif self.batch_file:
            self._line(f"批处理文件: {self.batch_file}")
            self._line(f"继续批处理：xvt batch-continue {shlex.quote(self.batch_file)}")
        else:
            self._line("任务尚未创建，无可恢复状态。")

    def report_preview_interruption(self) -> None:
        self._clear_progress()
        self._line("预览已中断")
        self._line("正式任务和已有产物不受影响，请重新执行原 preview 命令。")


def _event_sink(arguments: argparse.Namespace) -> EventSink:
    cached = getattr(arguments, "_event_sink_instance", None)
    if cached is not None:
        return cached
    job_file = getattr(arguments, "job_file", None)
    initial_job_file = (
        str(Path(job_file).expanduser().resolve()) if isinstance(job_file, str) else None
    )
    batch_file = getattr(arguments, "batch_file", None)
    initial_batch_file = (
        str(Path(batch_file).expanduser().resolve()) if isinstance(batch_file, str) else None
    )
    if getattr(arguments, "events_jsonl", False):
        sink: EventSink = _JsonlEventSink(initial_job_file)
    else:
        sink = _HumanEventSink(initial_job_file)
    if isinstance(sink, _TrackedEventSink):
        sink.batch_file = initial_batch_file
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
    event_sink(create_event(EventType.CLI_COMPLETED, command=arguments.command, **data))


def _print_batch_result(result: BatchResult) -> None:
    print(result.batch_file)
    print(
        f"batch: {result.completed} completed, "
        f"{result.awaiting_input} awaiting input, {result.failed} failed"
    )
    if result.status != "completed":
        print(f"next: xvt batch-continue {shlex.quote(str(result.batch_file))}")


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
        return ExitCode.OK if report["status"] == "pass" else ExitCode.DEPENDENCY_ERROR

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
                glossary=arguments.glossary,
                translation_style=arguments.translation_style,
                segmentation_preset=arguments.segmentation_preset,
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

    if arguments.command == "batch":
        event_sink = _event_sink(arguments)
        result = run_batch(
            arguments.input_directory,
            WorkflowOptions(
                input="",
                output=arguments.output,
                engine=arguments.engine,
                language=arguments.language,
                target_language=arguments.target_language,
                provider=arguments.provider,
                base_url=arguments.base_url,
                model=arguments.model,
                api_key_env=arguments.api_key_env,
                batch_size=arguments.batch_size,
                max_retries=arguments.max_retries,
                timeout_seconds=arguments.timeout_seconds,
                glossary=arguments.glossary,
                translation_style=arguments.translation_style,
                segmentation_preset=arguments.segmentation_preset,
                burn=arguments.burn,
                hardware_encoding=not arguments.software,
                target_font=arguments.target_font,
                source_font=arguments.source_font,
            ),
            recursive=arguments.recursive,
            event_sink=event_sink,
        )
        if arguments.events_jsonl:
            if result.status == "completed":
                _complete(arguments, batch_file=str(result.batch_file), status=result.status)
        else:
            _print_batch_result(result)
        return ExitCode.PARTIAL_FAILURE if result.status == "partial_failure" else ExitCode.OK

    if arguments.command == "batch-continue":
        event_sink = _event_sink(arguments)
        result = continue_batch(arguments.batch_file, event_sink=event_sink)
        if arguments.events_jsonl:
            if result.status == "completed":
                _complete(arguments, batch_file=str(result.batch_file), status=result.status)
        else:
            _print_batch_result(result)
        return ExitCode.PARTIAL_FAILURE if result.status == "partial_failure" else ExitCode.OK

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
        return ExitCode.OK if report["status"] == "pass" else ExitCode.VERIFY_FAILED

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
            glossary=arguments.glossary,
            translation_style=arguments.translation_style,
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
            glossary=arguments.glossary,
            translation_style=arguments.translation_style,
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

    if arguments.command == "resegment-translation":
        event_sink = _event_sink(arguments)
        result = resegment_translation(arguments.job_file, event_sink=event_sink)
        if arguments.events_jsonl:
            _complete(
                arguments,
                changed=result.changed,
                artifacts=[
                    str(path)
                    for path in (result.source_cues_path, result.report_path)
                    if path is not None
                ],
            )
        elif not result.changed:
            print("没有译文 cue 超过硬显示阈值，任务未改动。")
        else:
            print(f"已重切源字幕: {result.source_cues_path}")
            print(f"审计报告: {result.report_path}")
            job = load_job(arguments.job_file)
            if isinstance(job.config.get("workflow"), dict):
                print(f'继续重翻译: xvt continue "{arguments.job_file}"')
            else:
                target_language = job.config.get("target_language")
                if isinstance(target_language, str) and target_language:
                    print(
                        f'重新翻译: xvt translate "{arguments.job_file}" '
                        f"--target-language {target_language}"
                    )
        return 0

    if arguments.command == "retranslate-cues":
        event_sink = _event_sink(arguments)
        result = retranslate_cues(
            arguments.job_file,
            cue_ids=tuple(arguments.cue_ids),
            target_language=arguments.target_language,
            provider=arguments.provider,
            base_url=arguments.base_url,
            model=arguments.model,
            api_key_env=arguments.api_key_env,
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
            print(f"mode: {result.mode}")
            for path in result.paths:
                print(path)
            if result.mode == "agent":
                cues = " ".join(f"--cue {cue_id}" for cue_id in arguments.cue_ids)
                print("完成 Agent 翻译后执行：")
                print(
                    "xvt apply-cue-retranslation "
                    f'"{arguments.job_file}" RESPONSE {cues} '
                    f"--target-language {arguments.target_language}"
                )
            else:
                job = load_job(arguments.job_file)
                workflow = job.config.get("workflow")
                if isinstance(workflow, dict) and workflow.get("burn"):
                    print(f'继续烧录: xvt continue "{arguments.job_file}"')
        return 0

    if arguments.command == "apply-cue-retranslation":
        event_sink = _event_sink(arguments)
        paths = apply_cue_retranslation(
            arguments.job_file,
            arguments.response_file,
            cue_ids=tuple(arguments.cue_ids),
            target_language=arguments.target_language,
            event_sink=event_sink,
        )
        if arguments.events_jsonl:
            _complete(arguments, artifacts=[str(path) for path in paths])
        else:
            for path in paths:
                print(path)
            job = load_job(arguments.job_file)
            workflow = job.config.get("workflow")
            if isinstance(workflow, dict) and workflow.get("burn"):
                print(f'继续烧录: xvt continue "{arguments.job_file}"')
        return 0

    if arguments.command == "preview":
        event_sink = _event_sink(arguments)
        path = preview_translated_job(
            arguments.job_file,
            cue_id=arguments.cue,
            start_seconds=arguments.start,
            duration_seconds=arguments.duration,
            media=arguments.media,
            output=arguments.output,
            hardware_encoding=(None if arguments.software is None else False),
            target_font=arguments.target_font,
            source_font=arguments.source_font,
            event_sink=event_sink,
        )
        if arguments.events_jsonl:
            _complete(arguments, artifacts=[str(path)])
        else:
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


# 会写入既有 Job 的子命令：在 CLI 入口取 Job 目录锁，保证独占执行。
# `run` 与 `batch` 每次创建全新 Job/batch 目录，创建路径无竞争；在途 `run` 与并发
# `continue` 的窗口将在 run 统一为 continue 特例后由同一锁点收口。
_JOB_MUTATING_COMMANDS = frozenset(
    {
        "continue",
        "resume",
        "translate",
        "prepare-translation",
        "apply-translation",
        "import-translation",
        "retranslate-cues",
        "apply-cue-retranslation",
        "resegment-translation",
        "preview",
        "burn",
    }
)


def _execution_lock(parsed: argparse.Namespace):
    if parsed.command in _JOB_MUTATING_COMMANDS:
        return job_lock(parsed.job_file)
    if parsed.command == "batch-continue":
        return batch_lock(parsed.batch_file)
    return nullcontext()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        parser.print_help()
        return 0

    parsed = parser.parse_args(arguments)
    with signal_scope():
        return _execute(parsed)


def _execute(parsed: argparse.Namespace) -> int:
    try:
        with _execution_lock(parsed):
            return _run_command(parsed)
    except (KeyboardInterrupt, CommandCancelled):
        sink = _event_sink(parsed)
        tracked_sink = sink if isinstance(sink, _TrackedEventSink) else None
        if tracked_sink and tracked_sink.job_file and tracked_sink.current_stage:
            interrupt_running_stage(
                tracked_sink.job_file,
                tracked_sink.current_stage,
                event_sink=sink,
            )
        recovery_plan = None if parsed.command == "preview" else _recovery_plan_for_sink(sink)
        batch_file = tracked_sink.batch_file if tracked_sink else None
        interruption_event = create_event(
            EventType.CLI_FAILED,
            command=parsed.command,
            category="interrupted",
            message="interrupted by user",
            **({"next_action": "rerun_preview"} if parsed.command == "preview" else {}),
            **(
                {
                    "batch_file": batch_file,
                    "next_action": f"xvt batch-continue {shlex.quote(batch_file)}",
                }
                if parsed.command in {"batch", "batch-continue"} and batch_file
                else {}
            ),
            **({"recovery_plan": recovery_plan.to_dict()} if recovery_plan else {}),
        )
        if getattr(parsed, "events_jsonl", False):
            sink(interruption_event)
            print("error: interrupted by user", file=sys.stderr)
        elif isinstance(sink, _HumanEventSink):
            if parsed.command == "preview":
                sink.report_preview_interruption()
            else:
                sink.report_interruption(recovery_plan)
        return ExitCode.TERMINATED if received_signal() == signal.SIGTERM else ExitCode.INTERRUPTED
    except (XvtError, OSError, RuntimeError, ValueError) as error:
        if isinstance(error, XvtError):
            category, return_code = error.category, error.exit_code
        elif isinstance(error, ValueError):
            category, return_code = "invalid_input", ExitCode.USER_ERROR
        elif isinstance(error, (CommandError, OSError)):
            category, return_code = "dependency_or_process", ExitCode.DEPENDENCY_ERROR
        else:
            category, return_code = "workflow", ExitCode.WORKFLOW_ERROR
        sink = getattr(parsed, "_event_sink_instance", None)
        recovery_plan = None
        if parsed.command != "preview" and sink:
            recovery_plan = _recovery_plan_for_sink(sink)
        failure_event = create_event(
            EventType.CLI_FAILED,
            command=parsed.command,
            category=category,
            message=str(error),
            **(
                {"batch_file": sink.batch_file}
                if isinstance(sink, _TrackedEventSink) and sink.batch_file
                else {}
            ),
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

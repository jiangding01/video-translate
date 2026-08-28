import argparse
import json
import re
import shlex
import signal
import sys
from collections.abc import Callable, Sequence
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
from xvt.errors import ExitCode, UserInputError, XvtError
from xvt.events import Event, EventSink, EventType, create_event, error_event_fields
from xvt.job_store import get_job_file, load_job
from xvt.locking import hold_batch_lock, hold_job_lock, lock_scope
from xvt.models import Job, StageName, StageStatus
from xvt.pipeline import (
    REVISION_FINGERPRINT_LENGTH,
    SOURCE_REVIEW_STEM_PREFIX,
    TRANSLATION_REVIEW_STEM_PREFIX,
    TranslationRunResult,
    acknowledge_subtitle_review,
    acknowledge_translation_review,
    apply_agent_semantic_review,
    apply_agent_translation,
    apply_agent_translation_polish,
    apply_cue_retranslation,
    apply_source_review_fixes,
    apply_subtitle_translation,
    apply_translation_review_fixes,
    burn_translated_job,
    complete_acknowledged_semantic_review,
    cue_retranslation_waiting,
    inspect_input,
    interrupt_running_stage,
    prepare_agent_translation,
    prepare_source_review,
    preview_translated_job,
    rebuild_source,
    resegment_translation,
    resume_job,
    retranslate_cues,
    run_atomic_translation_polish,
    run_translation,
    run_translation_polish,
    run_translation_semantic_review,
    semantic_review_waiting,
)
from xvt.process import CommandCancelled, CommandError
from xvt.recovery import (
    RecoveryPlan,
    build_recovery_plan,
    subtitle_qa_failure_guidance,
    valid_artifact_path,
    verify_job,
)
from xvt.segment import SEGMENTATION_PRESETS
from xvt.translate import translation_policy_from_dict
from xvt.workflow import (
    WorkflowOptions,
    continue_workflow,
    plan_advance_steps,
    relink_burned_output,
    run_workflow,
)
from xvt.workflow_config import (
    WorkflowConfig,
    get_workflow_config,
    resolve_target_language,
    resolve_translation_quality,
)

_CLI_EPILOG = """\
recommended path:
  xvt run INPUT --output DIR [--target-language LANG]
    [--translation-quality standard|polished] [--burn]
  xvt status JOB.json
  xvt continue JOB.json

when status says awaiting Agent input:
  xvt apply JOB.json RESPONSE --continue  # auto-route, apply, and keep going
  # explicit alternatives: apply-translation / apply-polish-translation /
  # apply-semantic-review / apply-translation-review-fixes / apply-source-review-fixes

batch:
  xvt batch DIR --output DIR [--target-language LANG] [--translation-quality standard|polished]
  xvt batch-continue BATCH.json

Core commands: run, status, continue, batch, batch-continue, doctor, verify.
Commands marked [advanced] are atomic repair tools; prefer status → continue
for end-to-end jobs (resume ≠ continue).
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xvt",
        description=(
            "Local-first video transcription and subtitle translation. "
            "Default path: run → status → continue."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_CLI_EPILOG,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    inspect_parser = subparsers.add_parser(
        "inspect",
        help="[advanced] Inspect local media or a remote URL",
    )
    inspect_parser.add_argument("input")
    inspect_parser.add_argument("--json", action="store_true")

    doctor_parser = subparsers.add_parser("doctor", help="Check runtime dependencies")
    doctor_parser.add_argument("--output")
    doctor_parser.add_argument("--json", action="store_true")

    run_parser = subparsers.add_parser(
        "run",
        help="Create a job and run the end-to-end workflow (transcribe / translate / burn)",
    )
    run_parser.add_argument("input")
    run_parser.add_argument("--output", required=True)
    run_parser.add_argument("--engine", choices=["mlx", "faster"], default=DEFAULT_ENGINE)
    run_parser.add_argument("--language")
    run_parser.add_argument("--subtitles")
    run_parser.add_argument(
        "--source-strategy", choices=["auto", "subtitles", "asr"], default="auto"
    )
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
        "--translation-quality", choices=["standard", "polished"], default="standard"
    )
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
    batch_parser.add_argument(
        "--source-strategy", choices=["auto", "subtitles", "asr"], default="auto"
    )
    batch_parser.add_argument("--target-language")
    batch_parser.add_argument(
        "--translation-quality", choices=["standard", "polished"], default="standard"
    )
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

    status_parser = subparsers.add_parser(
        "status",
        help="Show job stages and the next recovery command (primary navigation)",
    )
    status_parser.add_argument("job_file")
    status_parser.add_argument("--json", action="store_true")

    verify_parser = subparsers.add_parser(
        "verify",
        help="Recompute artifact checksums and report integrity",
    )
    verify_parser.add_argument("job_file")
    verify_parser.add_argument("--json", action="store_true")

    relink_burned_parser = subparsers.add_parser(
        "relink-burned-output",
        help="Relink a moved burned video after verifying its original checksum",
    )
    relink_burned_parser.add_argument("job_file")
    relink_burned_parser.add_argument("existing_video")
    relink_burned_parser.add_argument("--events-jsonl", action="store_true")

    resume_parser = subparsers.add_parser(
        "resume",
        help="[advanced] Resume transcription checkpoints only (not end-to-end goals)",
    )
    resume_parser.add_argument("job_file")
    resume_parser.add_argument("--events-jsonl", action="store_true")

    rebuild_source_parser = subparsers.add_parser(
        "rebuild-source",
        help="[advanced] Rebuild source cues with ASR and invalidate downstream outputs",
    )
    rebuild_source_parser.add_argument("job_file")
    rebuild_source_parser.add_argument("--strategy", choices=["asr"], required=True)
    rebuild_source_parser.add_argument("--cookies-from-browser")
    rebuild_source_parser.add_argument("--proxy")
    rebuild_source_parser.add_argument("--events-jsonl", action="store_true")

    continue_parser = subparsers.add_parser(
        "continue",
        help="Finish a persisted end-to-end goal (translation / burn); use after status",
    )
    continue_parser.add_argument("job_file")
    continue_parser.add_argument("--cookies-from-browser")
    continue_parser.add_argument("--proxy")
    continue_parser.add_argument(
        "--acknowledge-subtitle-review",
        action="store_true",
        help="Acknowledge the current needs_review subtitle QA report and proceed",
    )
    continue_parser.add_argument(
        "--acknowledge-translation-review",
        action="store_true",
        help="Acknowledge the current semantic translation review report and proceed",
    )
    continue_parser.add_argument("--events-jsonl", action="store_true")

    unified_apply_parser = subparsers.add_parser(
        "apply",
        help="Auto-route an Agent response to the correct apply command",
    )
    unified_apply_parser.add_argument("job_file")
    unified_apply_parser.add_argument("response_file")
    unified_apply_parser.add_argument(
        "--continue",
        dest="continue_after_apply",
        action="store_true",
        help="Apply the response and continue the same end-to-end workflow",
    )
    unified_apply_parser.add_argument("--events-jsonl", action="store_true")

    prepare_parser = subparsers.add_parser(
        "prepare-translation",
        help="[advanced] Create deterministic translation batches for an agent",
    )
    prepare_parser.add_argument("job_file")
    prepare_parser.add_argument("--source-language", required=True)
    prepare_parser.add_argument("--target-language", required=True)
    prepare_parser.add_argument("--translation-quality", choices=["standard", "polished"])
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
        help="Apply Agent translation JSON, then run continue (needed after awaiting_input)",
    )
    apply_parser.add_argument("job_file")
    apply_parser.add_argument("response_file")
    apply_parser.add_argument("--target-language", required=True)
    apply_parser.add_argument("--events-jsonl", action="store_true")

    import_translation_parser = subparsers.add_parser(
        "import-translation",
        help="[advanced] Merge an existing translated subtitle file onto source cue timing",
    )
    import_translation_parser.add_argument("job_file")
    import_translation_parser.add_argument("subtitle_file")
    import_translation_parser.add_argument("--target-language", required=True)
    import_translation_parser.add_argument("--events-jsonl", action="store_true")

    translate_parser = subparsers.add_parser(
        "translate",
        help="[advanced] Atomic translate entry (prefer run/continue for end-to-end jobs)",
    )
    translate_parser.add_argument("job_file")
    translate_parser.add_argument("--target-language", required=True)
    translate_parser.add_argument("--translation-quality", choices=["standard", "polished"])
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

    polish_parser = subparsers.add_parser(
        "polish-translation",
        help="[advanced] Polish valid translated cues with a recoverable checkpoint",
    )
    polish_parser.add_argument("job_file")
    polish_parser.add_argument(
        "--provider",
        choices=["auto", "openai-compatible", "agent"],
    )
    polish_parser.add_argument("--base-url")
    polish_parser.add_argument("--model")
    polish_parser.add_argument("--api-key-env")
    polish_parser.add_argument("--batch-size", type=int)
    polish_parser.add_argument("--max-retries", type=int)
    polish_parser.add_argument("--timeout-seconds", type=float)
    polish_parser.add_argument("--events-jsonl", action="store_true")

    apply_polish_parser = subparsers.add_parser(
        "apply-polish-translation",
        help="Apply Agent polish JSON, then run continue",
    )
    apply_polish_parser.add_argument("job_file")
    apply_polish_parser.add_argument("response_file")
    apply_polish_parser.add_argument("--events-jsonl", action="store_true")

    apply_semantic_parser = subparsers.add_parser(
        "apply-semantic-review",
        help="Apply Agent semantic review and one-repair JSON, then run continue",
    )
    apply_semantic_parser.add_argument("job_file")
    apply_semantic_parser.add_argument("response_file")
    apply_semantic_parser.add_argument("--events-jsonl", action="store_true")

    apply_review_fixes_parser = subparsers.add_parser(
        "apply-translation-review-fixes",
        help="Apply human translation review edits, then run continue",
    )
    apply_review_fixes_parser.add_argument("job_file")
    apply_review_fixes_parser.add_argument("response_file")
    apply_review_fixes_parser.add_argument("--events-jsonl", action="store_true")

    prepare_source_review_parser = subparsers.add_parser(
        "prepare-source-review",
        help="Prepare an editable source cue review file",
    )
    prepare_source_review_parser.add_argument("job_file")
    prepare_source_review_parser.add_argument(
        "--cue", dest="cue_ids", type=int, action="append", required=True
    )
    prepare_source_review_parser.add_argument("--events-jsonl", action="store_true")

    apply_source_review_parser = subparsers.add_parser(
        "apply-source-review-fixes",
        help="Apply human source cue edits, re-run source QA, then inspect status",
    )
    apply_source_review_parser.add_argument("job_file")
    apply_source_review_parser.add_argument("response_file")
    apply_source_review_parser.add_argument("--events-jsonl", action="store_true")

    acknowledge_translation_parser = subparsers.add_parser(
        "acknowledge-translation-review",
        help="[advanced] Acknowledge the current semantic translation review report",
    )
    acknowledge_translation_parser.add_argument("job_file")
    acknowledge_translation_parser.add_argument("--events-jsonl", action="store_true")

    resegment_parser = subparsers.add_parser(
        "resegment-translation",
        help="[advanced] Re-segment overflowing ASR cues and invalidate their translations",
    )
    resegment_parser.add_argument("job_file")
    resegment_parser.add_argument("--events-jsonl", action="store_true")

    retranslate_parser = subparsers.add_parser(
        "retranslate-cues",
        help="[advanced] Re-translate selected cues without changing their timing",
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
        help="[advanced] Apply selected cue translations and re-run subtitle QA",
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
        help="[advanced] Render a short bilingual preview without changing the workflow goal",
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

    burn_parser = subparsers.add_parser(
        "burn",
        help="[advanced] Atomic burn entry (prefer run/continue --burn for end-to-end jobs)",
    )
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

# 转写入口会提前标记为 skipped、稍后由 workflow 再激活；人类进度不必逐条刷出。
_SILENT_PLAN_SKIPS = frozenset({"translate", "burn", "media_qa"})

_VIDEO_ARTIFACT_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov"}
_SUBTITLE_ARTIFACT_SUFFIXES = {".srt", ".ass", ".vtt"}


def _stage_display_label(
    stage: str,
    *,
    detail: object = None,
    phase: object = None,
    translated_context: bool = False,
) -> str:
    if stage == "translate":
        return {
            "polish": "润色字幕",
            "semantic_review": "语义校对",
            "repair": "定向修复",
        }.get(str(phase), "翻译字幕")
    if stage == "acquire":
        if detail == "subtitles":
            return "下载平台字幕"
        if detail == "media":
            return "下载源视频"
        return "获取素材"
    if stage == "subtitle_qa":
        if detail == "translated" or (detail is None and translated_context):
            return "检查译文字幕"
        if detail == "source":
            return "检查源字幕"
        return "检查字幕"
    if stage == "render":
        if detail == "translated" or (detail is None and translated_context):
            return "生成译文字幕文件"
        if detail == "source":
            return "生成源字幕文件"
        return "生成字幕文件"
    return _STAGE_LABELS.get(stage, stage)


def _format_stage_status_for_display(job: Job, stage_name: str, status: StageStatus) -> str:
    if stage_name == StageName.SUBTITLE_QA.value and status is StageStatus.FAILED:
        guidance = subtitle_qa_failure_guidance(job)
        if guidance is not None and guidance.is_needs_review:
            return "needs_review"
    return status.value


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


# 重试原因的契约字符串是英文（docs/EVENTS.md），仅在人类可读渲染层映射为中文；
# 未知原因回退显示契约原文。
_TRANSLATION_RETRY_REASON_LABELS = {
    "provider temporarily unavailable": "翻译服务暂时不可用",
    "invalid provider response": "翻译服务响应无效",
}

_SOURCE_SELECTION_LABELS = {
    "subtitles": "采用字幕候选",
    "aligned_subtitles": "采用已对齐字幕",
    "asr": "采用 ASR 字幕",
}


def _format_batch_position(data: dict[str, object]) -> str:
    batch = data.get("batch_index")
    if not isinstance(batch, int) or batch <= 0:
        batch = data.get("batch")
    batch_total = data.get("batch_total")
    if isinstance(batch_total, int) and batch_total > 0:
        return f"{batch}/{batch_total}"
    return f"{batch}"


class _HumanEventSink(_TrackedEventSink):
    def __init__(self, job_file: str | None = None, stream: TextIO | None = None) -> None:
        super().__init__(job_file)
        self.stream = stream or sys.stderr
        self.is_tty = self.stream.isatty()
        self._progress_active = False
        self._last_progress_at: dict[str, float] = {}
        self._pending_skips: list[str] = []
        self._last_failed_stage: str | None = None
        self._workflow_started_at: float | None = None
        self._seen_translation = False

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

    def _flush_pending_skips(self) -> None:
        if not self._pending_skips:
            return
        labels = list(dict.fromkeys(self._pending_skips))
        self._pending_skips.clear()
        self._line(f"[跳过] {'、'.join(labels)}")

    def __call__(self, event: Event) -> None:
        self._track(event)
        data = event.data
        if event.type == EventType.WORKFLOW_STARTED:
            self._workflow_started_at = monotonic()
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
        elif event.type == EventType.SOURCE_SELECTED:
            self._flush_pending_skips()
            selected = _SOURCE_SELECTION_LABELS.get(
                str(data.get("selected_kind")), str(data.get("selected_kind"))
            )
            self._line(f"[选择] 源字幕：{selected}（{data.get('reason')}）")
        elif event.type == EventType.TRANSLATION_BATCH_STARTED:
            self._seen_translation = True
            self._flush_pending_skips()
            self._last_failed_stage = None
            action = {
                "polish": "润色字幕",
                "semantic_review": "语义校对",
                "repair": "定向修复",
            }.get(data.get("phase"), "翻译字幕")
            self._line(f"[进行中] {action}（批次 {_format_batch_position(data)}）")
        elif event.type == EventType.TRANSLATION_BATCH_RETRYING:
            self._seen_translation = True
            self._flush_pending_skips()
            reason = data.get("reason")
            reason_label = _TRANSLATION_RETRY_REASON_LABELS.get(str(reason), reason)
            parts = [f"批次 {_format_batch_position(data)}"]
            attempt = data.get("attempt")
            if isinstance(attempt, int) and attempt > 0:
                parts.append(f"第 {attempt} 次尝试")
            delay = data.get("delay_seconds")
            if isinstance(delay, int | float) and delay > 0:
                parts.append(f"等待 {delay:g}s")
            if reason_label:
                parts.append(f"原因：{reason_label}")
            action = {
                "polish": "润色字幕",
                "semantic_review": "语义校对",
                "repair": "定向修复",
            }.get(data.get("phase"), "翻译字幕")
            self._line(f"[重试] {action}（{'，'.join(parts)}）")
        elif event.type == EventType.WORKFLOW_WAITING:
            self._flush_pending_skips()
            self._line(f"[等待] 翻译需要输入：{data.get('reason')}")
        elif event.type == EventType.WORKFLOW_COMPLETED:
            self._flush_pending_skips()
            elapsed = None
            if self._workflow_started_at is not None:
                elapsed = _format_duration(monotonic() - self._workflow_started_at)
            if elapsed:
                self._line(f"任务处理完成（耗时 {elapsed}）")
            else:
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
        status = event.data.get("status")
        detail = event.data.get("detail")
        phase = event.data.get("phase")
        if stage == "translate" and status in {"running", "succeeded", "awaiting_input"}:
            self._seen_translation = True
        label = _stage_display_label(
            stage,
            detail=detail,
            phase=phase,
            translated_context=self._seen_translation,
        )

        if status == "skipped":
            if stage in _SILENT_PLAN_SKIPS:
                return
            self._pending_skips.append(label)
            return

        self._flush_pending_skips()
        if event.type == EventType.STAGE_STARTED:
            self._last_failed_stage = None
            self._line(f"[进行中] {label}")
        elif event.type == EventType.STAGE_FAILED:
            if self._last_failed_stage == stage:
                return
            self._last_failed_stage = stage
            self._line(f"[失败] {label}")
        elif status == "awaiting_input":
            self._line(f"[等待] {label}")
        else:
            self._last_failed_stage = None
            self._line(f"[完成] {label}")

    def _render_batch_recovery(self, data: dict[str, object]) -> None:
        plan = data.get("recovery_plan")
        if not isinstance(plan, dict):
            return
        if plan.get("reason"):
            self._line(f"恢复建议：{plan.get('reason')}")
        for path in plan.get("input_paths", []):
            self._line(f"待编辑: {path}")
        for command in plan.get("prerequisite_commands", []):
            self._line(f"  {command}")
        if plan.get("primary_command"):
            self._line(f"  {plan.get('primary_command')}")
        for command in plan.get("alternative_commands", []):
            self._line(f"  或：{command}")

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
            for path in plan.input_paths:
                self._line(f"待编辑: {path}")
            for command in plan.prerequisite_commands:
                self._line(f"  {command}")
            if plan.primary_command:
                self._line(f"  {plan.primary_command}")
            for command in plan.alternative_commands:
                self._line(f"  或：{command}")
        if self.job_file:
            self._line("查看详情：")
            self._line(f'  xvt status "{self.job_file}"')

    def report_failure(self, message: str, plan: RecoveryPlan | None = None) -> None:
        self._clear_progress()
        self._flush_pending_skips()
        self._line(f"任务失败：{message}")
        if self.current_stage:
            self._line(f"失败阶段：{_stage_display_label(self.current_stage)}")
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
        self._flush_pending_skips()
        self._line("任务已中断")
        if self.current_stage:
            self._line(f"失败阶段：{_stage_display_label(self.current_stage)}")
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


def _human_review_waiting(job: Job) -> bool:
    stages = getattr(job, "stages", None)
    if not isinstance(stages, dict):
        return False
    qa = stages.get(StageName.SUBTITLE_QA)
    return bool(qa and qa.status is StageStatus.AWAITING_INPUT) or semantic_review_waiting(job)


def _print_recovery_navigation(job_file: str | Path) -> None:
    try:
        plan = build_recovery_plan(job_file)
    except (OSError, RuntimeError, ValueError):
        print(f"next: xvt status {shlex.quote(str(job_file))}")
        return
    print(f"recovery: {plan.reason}")
    for path in plan.report_paths:
        print(f"report: {path}")
    for path in plan.input_paths:
        print(f"input: {path}")
    if plan.primary_command:
        print(f"next: {plan.primary_command}")
    for command in plan.alternative_commands:
        print(f"alternative: {command}")


def _print_batch_result(result: BatchResult) -> None:
    print(result.batch_file)
    print(
        f"batch: {result.completed} completed, "
        f"{result.awaiting_input} awaiting input, {result.failed} failed"
    )
    if result.status != "completed":
        print(f"next: xvt batch-continue {shlex.quote(str(result.batch_file))}")


def _print_workflow_result(result: object) -> None:
    """stdout 交付物摘要：Job → 视频 → 字幕 → 其他，避免扁平路径列表难读。"""

    job_file = getattr(result, "job_file", None)
    artifacts = tuple(getattr(result, "artifacts", ()) or ())
    message = getattr(result, "message", None)
    status = getattr(result, "status", None)
    if job_file is not None:
        print(job_file)
        try:
            job = load_job(job_file)
            if resolve_target_language(job):
                print(f"翻译质量: {resolve_translation_quality(job)}")
        except (OSError, ValueError):
            pass
    videos: list[Path] = []
    subtitles: list[Path] = []
    others: list[Path] = []
    for item in artifacts:
        path = Path(item)
        suffix = path.suffix.lower()
        name = path.name.lower()
        if "preview" in name:
            continue
        if suffix in _VIDEO_ARTIFACT_SUFFIXES:
            videos.append(path)
        elif suffix in _SUBTITLE_ARTIFACT_SUFFIXES:
            subtitles.append(path)
        else:
            others.append(path)
    if videos:
        print("视频:")
        for path in videos:
            print(f"  {path}")
    if subtitles:
        print("字幕:")
        for path in subtitles:
            print(f"  {path}")
    if others:
        print("其他:")
        for path in others:
            print(f"  {path}")
    if status == "awaiting_input":
        try:
            plan = build_recovery_plan(job_file) if job_file is not None else None
        except (OSError, RuntimeError, ValueError):
            plan = None
        if plan is not None:
            print(f"恢复建议：{plan.reason}")
            for path in plan.report_paths:
                print(f"报告: {path}")
            for path in plan.input_paths:
                print(f"待编辑: {path}")
            for command in plan.prerequisite_commands:
                print(f"  {command}")
            if plan.primary_command:
                print(f"  {plan.primary_command}")
            for command in plan.alternative_commands:
                print(f"  或：{command}")
        elif message:
            print(f"translation is awaiting input: {message}")


def _cmd_inspect(arguments: argparse.Namespace) -> int:
    result = inspect_input(arguments.input)
    if arguments.json:
        _print_json(result)
    else:
        print(f"{result['kind']}: {result.get('title') or result.get('path')}")
    return 0


def _cmd_doctor(arguments: argparse.Namespace) -> int:
    report = run_doctor(arguments.output)
    if arguments.json:
        _print_json(report)
    else:
        for check in report["checks"]:
            print(f"{check['status'].upper():4} {check['name']}: {check['message']}")
        print(f"overall: {report['status']}")
    return ExitCode.OK if report["status"] == "pass" else ExitCode.DEPENDENCY_ERROR


def _cmd_run(arguments: argparse.Namespace) -> int:
    event_sink = _event_sink(arguments)
    result = run_workflow(
        WorkflowOptions(
            input=arguments.input,
            output=arguments.output,
            engine=arguments.engine,
            language=arguments.language,
            subtitles=arguments.subtitles,
            source_strategy=arguments.source_strategy,
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
            translation_quality=arguments.translation_quality,
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
        _print_workflow_result(result)
    return 0


def _cmd_batch(arguments: argparse.Namespace) -> int:
    event_sink = _event_sink(arguments)
    result = run_batch(
        arguments.input_directory,
        WorkflowOptions(
            input="",
            output=arguments.output,
            engine=arguments.engine,
            language=arguments.language,
            source_strategy=arguments.source_strategy,
            target_language=arguments.target_language,
            translation_quality=arguments.translation_quality,
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


def _cmd_batch_continue(arguments: argparse.Namespace) -> int:
    event_sink = _event_sink(arguments)
    result = continue_batch(arguments.batch_file, event_sink=event_sink)
    if arguments.events_jsonl:
        if result.status == "completed":
            _complete(arguments, batch_file=str(result.batch_file), status=result.status)
    else:
        _print_batch_result(result)
    return ExitCode.PARTIAL_FAILURE if result.status == "partial_failure" else ExitCode.OK


def _cmd_status(arguments: argparse.Namespace) -> int:
    job = load_job(arguments.job_file)
    plan = build_recovery_plan(job)
    next_action = plan.primary_command or plan.reason
    if arguments.json:
        data = job.to_dict()
        data["next_action"] = next_action
        data["recovery_plan"] = plan.to_dict()
        _print_json(data)
    else:
        stages = ", ".join(
            f"{name}={_format_stage_status_for_display(job, name, record.status)}"
            for name, record in job.stages.items()
        )
        print(f"{job.id}: {stages}")
        print(f"recovery: {plan.category} - {plan.reason}")
        if plan.advance_steps:
            print(f"steps: {', '.join(plan.advance_steps)}")
        for path in plan.report_paths:
            print(f"report: {path}")
        for path in plan.input_paths:
            print(f"input: {path}")
        for command in plan.prerequisite_commands:
            print(f"before: {command}")
        if plan.primary_command:
            print(f"next: {plan.primary_command}")
        elif plan.category in {"manual_fix", "awaiting_input"}:
            # 仅在需要人工处理后回看导航时提示；complete/unrecoverable 不刷多余 hint。
            print(f'hint: re-check with xvt status "{arguments.job_file}" after repairs')
        for command in plan.alternative_commands:
            print(f"alternative: {command}")
    return 0


def _cmd_verify(arguments: argparse.Namespace) -> int:
    report = verify_job(arguments.job_file)
    if arguments.json:
        _print_json(report)
    else:
        for artifact in report["artifacts"]:
            print(f"{artifact['status'].upper():4} {artifact['kind']}: {artifact['message']}")
        print(f"overall: {report['status']}")
    return ExitCode.OK if report["status"] == "pass" else ExitCode.VERIFY_FAILED


def _cmd_relink_burned_output(arguments: argparse.Namespace) -> int:
    event_sink = _event_sink(arguments)
    path = relink_burned_output(
        arguments.job_file,
        arguments.existing_video,
        event_sink=event_sink,
    )
    if arguments.events_jsonl:
        _complete(
            arguments,
            job_file=str(Path(arguments.job_file).expanduser().resolve()),
            artifacts=[str(path)],
        )
    else:
        print(f"已重新关联烧录成品: {path}")
    return 0


def _cmd_resume(arguments: argparse.Namespace) -> int:
    event_sink = _event_sink(arguments)
    job = resume_job(arguments.job_file, event_sink=event_sink)
    waiting = _human_review_waiting(job)
    if arguments.events_jsonl:
        if not waiting:
            _complete(arguments, job_file=str(get_job_file(job)))
    else:
        print(get_job_file(job))
        if waiting:
            _print_recovery_navigation(get_job_file(job))
    return 0


def _cmd_rebuild_source(arguments: argparse.Namespace) -> int:
    event_sink = _event_sink(arguments)
    result = rebuild_source(
        arguments.job_file,
        strategy=arguments.strategy,
        cookies_from_browser=arguments.cookies_from_browser,
        proxy=arguments.proxy,
        event_sink=event_sink,
    )
    artifacts = [
        result.source_cues_path,
        result.source_srt_path,
        result.qa_path,
        result.selection_path,
    ]
    if arguments.events_jsonl:
        _complete(arguments, artifacts=[str(path) for path in artifacts])
    else:
        print(f"已使用 ASR 重建源字幕: {result.source_cues_path}")
        print(f"源字幕文件: {result.source_srt_path}")
        print(f"质量报告: {result.qa_path}")
        job = load_job(arguments.job_file)
        quoted_job_file = shlex.quote(str(Path(arguments.job_file).expanduser().resolve()))
        if get_workflow_config(job) is not None:
            print(f"继续任务: xvt continue {quoted_job_file}")
        else:
            target_language = resolve_target_language(job)
            if target_language:
                print(
                    f"继续翻译: xvt translate {quoted_job_file} "
                    f"--target-language {shlex.quote(target_language)}"
                )
    return 0


def _cmd_continue(arguments: argparse.Namespace) -> int:
    event_sink = _event_sink(arguments)
    if arguments.acknowledge_subtitle_review:
        report_path = acknowledge_subtitle_review(arguments.job_file, event_sink=event_sink)
        if not arguments.events_jsonl:
            print(f"已确认字幕 QA 复核报告: {report_path}", file=sys.stderr)
    if arguments.acknowledge_translation_review:
        report_path = acknowledge_translation_review(arguments.job_file, event_sink=event_sink)
        if not arguments.events_jsonl:
            print(f"已确认译文语义复核报告: {report_path}", file=sys.stderr)
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
        _print_workflow_result(result)
    return 0


def _cmd_prepare_translation(arguments: argparse.Namespace) -> int:
    event_sink = _event_sink(arguments)
    path = prepare_agent_translation(
        arguments.job_file,
        source_language=arguments.source_language,
        target_language=arguments.target_language,
        batch_size=arguments.batch_size,
        glossary=arguments.glossary,
        translation_style=arguments.translation_style,
        translation_quality=arguments.translation_quality,
        event_sink=event_sink,
    )
    if arguments.events_jsonl:
        _complete(arguments, artifacts=[str(path)])
    else:
        print(path)
    return 0


def _cmd_apply_translation(arguments: argparse.Namespace) -> int:
    event_sink = _event_sink(arguments)
    paths = apply_agent_translation(
        arguments.job_file,
        arguments.response_file,
        target_language=arguments.target_language,
        event_sink=event_sink,
    )
    job = load_job(arguments.job_file)
    if get_workflow_config(job) is None and resolve_translation_quality(job) == "polished":
        semantic = run_translation_semantic_review(
            arguments.job_file,
            event_sink=event_sink,
        )
        paths = semantic.paths
        job = load_job(arguments.job_file)
    waiting = _human_review_waiting(job)
    defer = bool(getattr(arguments, "_defer_cli_complete", False))
    if arguments.events_jsonl:
        if not waiting and not defer:
            _complete(arguments, artifacts=[str(path) for path in paths])
    elif not defer:
        for path in paths:
            print(path)
        if waiting:
            _print_recovery_navigation(arguments.job_file)
    return 0


def _cmd_import_translation(arguments: argparse.Namespace) -> int:
    event_sink = _event_sink(arguments)
    paths = apply_subtitle_translation(
        arguments.job_file,
        arguments.subtitle_file,
        target_language=arguments.target_language,
        event_sink=event_sink,
    )
    waiting = _human_review_waiting(load_job(arguments.job_file))
    if arguments.events_jsonl:
        if not waiting:
            _complete(arguments, artifacts=[str(path) for path in paths])
    else:
        for path in paths:
            print(path)
        if waiting:
            _print_recovery_navigation(arguments.job_file)
    return 0


def _cmd_translate(arguments: argparse.Namespace) -> int:
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
        translation_quality=arguments.translation_quality,
        event_sink=event_sink,
    )
    if (
        result.mode != "agent"
        and resolve_translation_quality(load_job(arguments.job_file)) == "polished"
    ):
        job = load_job(arguments.job_file)
        if get_workflow_config(job) is not None:
            result = run_translation_polish(arguments.job_file, event_sink=event_sink)
        else:
            result = run_atomic_translation_polish(
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
        if result.mode != "agent":
            job = load_job(arguments.job_file)
            workflow = get_workflow_config(job)
            semantic_config = workflow or WorkflowConfig(
                target_language=arguments.target_language,
                provider=arguments.provider,
                base_url=arguments.base_url,
                model=arguments.model,
                api_key_env=arguments.api_key_env,
                batch_size=arguments.batch_size,
                max_retries=arguments.max_retries,
                timeout_seconds=arguments.timeout_seconds,
                translation_policy=translation_policy_from_dict(
                    job.config.get("translation_policy")
                ),
                translation_quality="polished",
            )
            semantic = run_translation_semantic_review(
                arguments.job_file,
                config=semantic_config,
                event_sink=event_sink,
            )
            result = TranslationRunResult(
                semantic.mode,
                semantic.paths,
                semantic.fallback_reason,
            )
    try:
        job = load_job(arguments.job_file)
    except OSError:
        waiting = False
    else:
        waiting = _human_review_waiting(job)
    if arguments.events_jsonl:
        if not waiting:
            _complete(
                arguments,
                mode=result.mode,
                artifacts=[str(path) for path in result.paths],
            )
    else:
        if result.fallback_reason:
            print(f"translation provider unavailable ({result.fallback_reason}); using Agent mode")
        print(f"mode: {result.mode}")
        for path in result.paths:
            print(path)
        if waiting:
            _print_recovery_navigation(arguments.job_file)
    return 0


def _cmd_polish_translation(arguments: argparse.Namespace) -> int:
    event_sink = _event_sink(arguments)
    result = run_translation_polish(
        arguments.job_file,
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
            print(f"translation provider unavailable ({result.fallback_reason}); using Agent mode")
        print(f"mode: {result.mode}")
        for path in result.paths:
            print(path)
    return 0


def _cmd_apply_polish_translation(arguments: argparse.Namespace) -> int:
    event_sink = _event_sink(arguments)
    paths = apply_agent_translation_polish(
        arguments.job_file,
        arguments.response_file,
        event_sink=event_sink,
    )
    defer = bool(getattr(arguments, "_defer_cli_complete", False))
    if arguments.events_jsonl:
        if not defer:
            _complete(arguments, artifacts=[str(path) for path in paths])
    elif not defer:
        for path in paths:
            print(path)
    return 0


def _cmd_apply_semantic_review(arguments: argparse.Namespace) -> int:
    event_sink = _event_sink(arguments)
    result = apply_agent_semantic_review(
        arguments.job_file,
        arguments.response_file,
        event_sink=event_sink,
    )
    defer = bool(getattr(arguments, "_defer_cli_complete", False))
    if arguments.events_jsonl:
        if not defer:
            _complete(
                arguments,
                status=result.status,
                artifacts=[str(path) for path in result.paths],
            )
    elif not defer:
        print(f"status: {result.status}")
        for path in result.paths:
            print(path)
    return 0


def _cmd_apply_translation_review_fixes(arguments: argparse.Namespace) -> int:
    event_sink = _event_sink(arguments)
    paths = apply_translation_review_fixes(
        arguments.job_file,
        arguments.response_file,
        event_sink=event_sink,
    )
    defer = bool(getattr(arguments, "_defer_cli_complete", False))
    if arguments.events_jsonl:
        if not defer:
            _complete(arguments, artifacts=[str(path) for path in paths])
    elif not defer:
        for path in paths:
            print(path)
        _print_recovery_navigation(arguments.job_file)
    return 0


def _cmd_prepare_source_review(arguments: argparse.Namespace) -> int:
    event_sink = _event_sink(arguments)
    path = prepare_source_review(
        arguments.job_file,
        arguments.cue_ids,
        event_sink=event_sink,
    )
    paths = (path, path.with_suffix(".srt"))
    if arguments.events_jsonl:
        _complete(arguments, artifacts=[str(item) for item in paths])
    else:
        for item in paths:
            print(item)
        print(
            "next: xvt apply-source-review-fixes "
            f"{shlex.quote(str(arguments.job_file))} {shlex.quote(str(path))}"
        )
    return 0


def _cmd_apply_source_review_fixes(arguments: argparse.Namespace) -> int:
    event_sink = _event_sink(arguments)
    paths = apply_source_review_fixes(
        arguments.job_file,
        arguments.response_file,
        event_sink=event_sink,
    )
    if arguments.events_jsonl:
        if not getattr(arguments, "_defer_cli_complete", False):
            _complete(arguments, artifacts=[str(path) for path in paths])
    else:
        for path in paths:
            print(path)
        _print_recovery_navigation(arguments.job_file)
    return 0


def _cmd_acknowledge_translation_review(arguments: argparse.Namespace) -> int:
    event_sink = _event_sink(arguments)
    report_path = acknowledge_translation_review(
        arguments.job_file,
        event_sink=event_sink,
    )
    paths = (report_path,)
    if get_workflow_config(load_job(arguments.job_file)) is None:
        paths = (
            report_path,
            *complete_acknowledged_semantic_review(
                arguments.job_file,
                event_sink=event_sink,
            ),
        )
    if arguments.events_jsonl:
        _complete(arguments, artifacts=[str(path) for path in paths])
    else:
        for path in paths:
            print(path)
    return 0


def _cmd_resegment_translation(arguments: argparse.Namespace) -> int:
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
        if get_workflow_config(job) is not None:
            print(f'继续重翻译: xvt continue "{arguments.job_file}"')
        else:
            target_language = resolve_target_language(job)
            if target_language:
                print(
                    f'重新翻译: xvt translate "{arguments.job_file}" '
                    f"--target-language {target_language}"
                )
    return 0


def _cmd_retranslate_cues(arguments: argparse.Namespace) -> int:
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
            workflow = get_workflow_config(job)
            if workflow is not None and workflow.burn:
                print(f'继续烧录: xvt continue "{arguments.job_file}"')
    return 0


def _cmd_apply_cue_retranslation(arguments: argparse.Namespace) -> int:
    event_sink = _event_sink(arguments)
    paths = apply_cue_retranslation(
        arguments.job_file,
        arguments.response_file,
        cue_ids=tuple(arguments.cue_ids),
        target_language=arguments.target_language,
        event_sink=event_sink,
    )
    job = load_job(arguments.job_file)
    waiting = _human_review_waiting(job)
    if arguments.events_jsonl:
        if not waiting:
            _complete(arguments, artifacts=[str(path) for path in paths])
    else:
        for path in paths:
            print(path)
        if waiting:
            _print_recovery_navigation(arguments.job_file)
        elif (workflow := get_workflow_config(job)) is not None and workflow.burn:
            print(f'继续烧录: xvt continue "{arguments.job_file}"')
    return 0


def _cmd_preview(arguments: argparse.Namespace) -> int:
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


# response JSON 的 task 字段路由表。两类 key 的真实来源不同：
# - source_review_fixes / translation_review_fixes：CLI 生成修订模板时写入 task，用户编辑后
#   原样提交，因此这两个 key 会真实命中（模板写入点见 xvt.pipeline.revision）。
# - semantic_review_only / semantic_review_and_repair / translate_and_polish：当前只出现在
#   CLI 生成的 manifest 里；Agent 回填响应契约
#   （skills/video-translate/references/translation-protocol.md）只要求 fingerprint + items，
#   不含 task 字段，所以协议一致的真实 Agent 响应走下面的 _STEP_ROUTES 步骤回退。保留这三个
#   key 只为前向兼容（用户手抄 manifest 的 task 字段时仍能命中）。
# 还有一类 task 有真实响应但无法自动路由（目标子命令需要 xvt apply 拿不到的参数），它们登记
# 在 _MANUAL_TASK_GUIDANCE 里换成可复制的手工命令提示，而不是落到「unknown task」。同一份
# 指引在步骤回退分支里也要能给出：retranslate_cues 的协议一致响应没有 task 字段，只能靠 Job
# 状态识别（见 _cmd_apply 的 cue-retranslation 分支）。
# 不变量：_MANUAL_TASK_GUIDANCE 与 _TASK_ROUTES 不得出现同名 key——同一个 task 既给手工指引
# 又自动路由是自相矛盾的，见 _assert_disjoint_task_tables()。
_TASK_ROUTES: dict[str, str] = {
    "source_review_fixes": "apply-source-review-fixes",
    "translation_review_fixes": "apply-translation-review-fixes",
    "semantic_review_only": "apply-semantic-review",
    "semantic_review_and_repair": "apply-semantic-review",
    "translate_and_polish": "apply-translation",
}

# 手工命令指引的共用正文：apply-cue-retranslation 必须显式收 --cue 与 --target-language，
# 而 xvt apply 的命令行里没有这两个参数，也不能从响应里安全地推出来。
# 事实口径：只有人类可读模式的 xvt retranslate-cues 会打印现成命令，--events-jsonl 模式不打
# 印，所以指引只能承诺 manifest 里有拼命令所需的字段，不能承诺「上一条命令已经打印过」。
_RETRANSLATE_CUES_MANUAL_COMMAND = (
    "applying one needs the cue ids and the target language as explicit arguments; run "
    "xvt apply-cue-retranslation JOB RESPONSE --cue ID [--cue ID ...] --target-language LANG "
    "instead — cue-retranslation-batches.json carries everything that command needs: "
    "batches[].items[].id are the cue ids and target_language is the language"
)

# 有响应但不可自动路由的 task。
_MANUAL_TASK_GUIDANCE: dict[str, str] = {
    "retranslate_cues": (
        f"xvt apply cannot route retranslate_cues responses: {_RETRANSLATE_CUES_MANUAL_COMMAND}"
    ),
}


def _assert_disjoint_task_tables() -> None:
    """同一个 task 不能既登记手工指引又登记自动路由：先命中的那张表会静默吃掉另一张。"""

    overlap = _MANUAL_TASK_GUIDANCE.keys() & _TASK_ROUTES.keys()
    if overlap:
        raise AssertionError(f"task appears in both routing tables: {sorted(overlap)}")


_assert_disjoint_task_tables()

# 步骤回退表。Agent 回填响应按协议不带 task 字段，绝大多数情况靠这里的下一步识别。
_STEP_ROUTES: dict[str, str] = {
    "await_agent_translation": "apply-translation",
    "await_agent_polish": "apply-polish-translation",
    "await_agent_semantic_review": "apply-semantic-review",
    "await_translation_review": "apply-translation-review-fixes",
}

# 刻意不进 _STEP_ROUTES 的等待步骤，以及不进的理由。原子重译现在是
# plan_advance_steps 的一个正式等待步骤（见 workflow.AWAIT_STEPS），但目标子命令
# apply-cue-retranslation 必须显式收 --cue 与 --target-language，而 xvt apply 的命令行
# 里没有这两个参数、也不能从响应里安全地推出来（AGENTS.md 已定调）。
# 这张表存在的意义是让「新增等待步骤」必须表态：要么进路由表，要么在这里写明理由，
# 否则守卫测试会红。
_UNROUTABLE_AWAIT_STEPS: frozenset[str] = frozenset({"await_cue_retranslation"})

# 人工修订 SRT 的固定命名形状。命名规则的唯一来源在 xvt.pipeline.revision（模板写入点与
# 回填校验点共用），这里只按同样的前缀与 fingerprint 长度派生形状正则，不再独立硬编码。
_SOURCE_REVIEW_SRT_NAME = re.compile(
    rf"^{re.escape(SOURCE_REVIEW_STEM_PREFIX)}[0-9a-f]{{{REVISION_FINGERPRINT_LENGTH}}}$"
)
_TRANSLATION_REVIEW_SRT_NAME = re.compile(
    rf"^{re.escape(TRANSLATION_REVIEW_STEM_PREFIX)}[0-9a-f]{{{REVISION_FINGERPRINT_LENGTH}}}$"
)


def _cmd_apply(arguments: argparse.Namespace) -> int:
    # 先在原 Namespace 上建 sink，路由副本才能继承它，让失败路径仍能取到恢复计划。
    _event_sink(arguments)
    response_path = Path(arguments.response_file)
    route = None
    job = None
    if response_path.suffix.lower() == ".srt":
        if _SOURCE_REVIEW_SRT_NAME.match(response_path.stem):
            route = "apply-source-review-fixes"
        elif _TRANSLATION_REVIEW_SRT_NAME.match(response_path.stem):
            route = "apply-translation-review-fixes"
    if route is None:
        # 读不出/解析不了都是用户输入错误（含把普通 SRT 当 response 喂进来），不能逃逸成
        # OSError/JSONDecodeError 被顶层归到 dependency_or_process。
        try:
            response_data = json.loads(response_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise UserInputError(
                f"cannot read response file: {error}; "
                "xvt apply only accepts a JSON response file — "
                "for subtitle files (SRT etc.) use xvt import-translation"
            ) from error
        if not isinstance(response_data, dict):
            raise UserInputError("response file must contain a JSON object")

        task = response_data.get("task")
        if isinstance(task, str):
            guidance = _MANUAL_TASK_GUIDANCE.get(task)
            if guidance is not None:
                raise UserInputError(guidance)
            route = _TASK_ROUTES.get(task)
            if route is None:
                raise ValueError(f"unknown task in response file: {task}")
        else:
            job = load_job(arguments.job_file)
            if cue_retranslation_waiting(job):
                # 协议一致的重译响应不带 task，而 apply-cue-retranslation 不可自动路由，
                # 两条识别路径同时落空：此前这里报「no persisted workflow goal」或「job is
                # not awaiting Agent input」，与 TRANSLATE stage 明明在 awaiting_input 的
                # 事实直接冲突，用户无路可走。谓词与 planner / status 共用同一份严格实现，
                # 因此过期的残留 manifest 不会再把合法的润色回填顶掉。
                raise UserInputError(
                    "this job is awaiting a cue retranslation response, and per the Agent "
                    "protocol that response carries no task field: "
                    f"{_RETRANSLATE_CUES_MANUAL_COMMAND}"
                )
            workflow = get_workflow_config(job)
            if workflow is None:
                raise ValueError(
                    "response file has no task field, and this job has no persisted workflow goal; "
                    "run xvt status to see the explicit apply-* command for it"
                )
            steps = plan_advance_steps(job, workflow)
            route = _STEP_ROUTES.get(steps[0]) if steps else None
            if route is None:
                if valid_artifact_path(job, "cue_retranslation_batches") is not None:
                    # 宽松兜底：manifest 还在、checksum 也有效，但已经对不上当前译文
                    # （多半是回填后的残留，或升级后需要重建）。严格谓词在上面已经判假，
                    # 这里只能给出「重新生成 manifest」这条真正可执行的路径。
                    raise UserInputError(
                        "a cue retranslation manifest exists but no longer matches the current "
                        "translation (it was most likely already applied, or needs regenerating "
                        "after an upgrade); rerun xvt retranslate-cues to regenerate it, or "
                        f"{_RETRANSLATE_CUES_MANUAL_COMMAND}"
                    )
                raise ValueError(
                    "response file has no task field, and the job is not awaiting Agent input; "
                    "run xvt status to see the current next step"
                )

    # 不改写 command：cli_completed 与 cli_failed 都必须报用户实际执行的子命令名
    # （EVENTS.md 契约），否则同一次调用在成功与失败两条路径上报出两个身份。
    routed = argparse.Namespace(**vars(arguments))
    if route == "apply-translation":
        target_language = resolve_target_language(job or load_job(arguments.job_file))
        if target_language is None:
            raise ValueError("job has no target language configured")
        routed.target_language = target_language
    continue_after_apply = bool(getattr(arguments, "continue_after_apply", False))
    if continue_after_apply and route == "apply-source-review-fixes":
        raise UserInputError(
            "--continue is not supported for source review fixes: source edits invalidate "
            "translation and downstream artifacts; apply the source review first, then run status"
        )
    if continue_after_apply:
        continuation_job = job or load_job(arguments.job_file)
        if get_workflow_config(continuation_job) is None:
            raise UserInputError(
                "--continue requires a persisted end-to-end workflow goal; "
                "apply the response without --continue, then run status"
            )
    if not continue_after_apply:
        return _COMMAND_DISPATCH[route](routed)
    routed._defer_cli_complete = True
    _COMMAND_DISPATCH[route](routed)
    continuation = argparse.Namespace(**vars(arguments))
    continuation.command = "apply"
    continuation.cookies_from_browser = None
    continuation.proxy = None
    continuation.acknowledge_subtitle_review = False
    continuation.acknowledge_translation_review = False
    return _cmd_continue(continuation)


def _cmd_burn(arguments: argparse.Namespace) -> int:
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


_COMMAND_DISPATCH: dict[str, Callable[[argparse.Namespace], int]] = {
    "inspect": _cmd_inspect,
    "doctor": _cmd_doctor,
    "run": _cmd_run,
    "batch": _cmd_batch,
    "batch-continue": _cmd_batch_continue,
    "status": _cmd_status,
    "verify": _cmd_verify,
    "relink-burned-output": _cmd_relink_burned_output,
    "resume": _cmd_resume,
    "rebuild-source": _cmd_rebuild_source,
    "continue": _cmd_continue,
    "prepare-translation": _cmd_prepare_translation,
    "apply-translation": _cmd_apply_translation,
    "import-translation": _cmd_import_translation,
    "translate": _cmd_translate,
    "polish-translation": _cmd_polish_translation,
    "apply-polish-translation": _cmd_apply_polish_translation,
    "apply-semantic-review": _cmd_apply_semantic_review,
    "apply-translation-review-fixes": _cmd_apply_translation_review_fixes,
    "prepare-source-review": _cmd_prepare_source_review,
    "apply-source-review-fixes": _cmd_apply_source_review_fixes,
    "acknowledge-translation-review": _cmd_acknowledge_translation_review,
    "resegment-translation": _cmd_resegment_translation,
    "retranslate-cues": _cmd_retranslate_cues,
    "apply-cue-retranslation": _cmd_apply_cue_retranslation,
    "apply": _cmd_apply,
    "preview": _cmd_preview,
    "burn": _cmd_burn,
}


def _run_command(arguments: argparse.Namespace) -> int:
    handler = _COMMAND_DISPATCH.get(arguments.command)
    if handler is not None:
        return handler(arguments)
    return 0


# 会写入既有 Job 的子命令：在 CLI 入口取 Job 目录锁，保证独占执行。
# `run` 与 `batch` 每次创建全新 Job/batch 目录，创建路径无竞争；在途 `run` 与并发
# `continue` 的窗口将在 run 统一为 continue 特例后由同一锁点收口。
_JOB_MUTATING_COMMANDS = frozenset(
    {
        "apply",
        "continue",
        "relink-burned-output",
        "resume",
        "rebuild-source",
        "translate",
        "polish-translation",
        "apply-polish-translation",
        "apply-semantic-review",
        "apply-translation-review-fixes",
        "prepare-source-review",
        "apply-source-review-fixes",
        "acknowledge-translation-review",
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


def _hold_execution_locks(parsed: argparse.Namespace) -> None:
    if parsed.command in _JOB_MUTATING_COMMANDS:
        hold_job_lock(parsed.job_file)
    elif parsed.command == "batch-continue":
        hold_batch_lock(parsed.batch_file)
    # `run` 与 `batch` 的锁在资源首次持久化点获取（pipeline.create_job 后 /
    # run_batch 写出 batch.json 前），同样挂在本命令的锁作用域上直至终态。


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        parser.print_help()
        return 0

    parsed = parser.parse_args(arguments)
    with signal_scope(), lock_scope():
        return _execute(parsed)


def _execute(parsed: argparse.Namespace) -> int:
    try:
        _hold_execution_locks(parsed)
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
            **error_event_fields("interrupted", "interrupted by user"),
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
            **error_event_fields(category, str(error)),
            **(
                {"batch_file": sink.batch_file}
                if isinstance(sink, _TrackedEventSink) and sink.batch_file
                else {}
            ),
            **({"recovery_plan": recovery_plan.to_dict()} if recovery_plan else {}),
        )
        safe_message = str(failure_event.data["error"]["message"])
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

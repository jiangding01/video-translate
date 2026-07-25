from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from xvt.config import (
    DEFAULT_API_KEY_ENV,
    DEFAULT_BATCH_SIZE,
    DEFAULT_ENGINE,
    DEFAULT_HARDWARE_ENCODING,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MIN_ALIGNMENT_CONFIDENCE,
    DEFAULT_PROVIDER,
    DEFAULT_SEGMENTATION_PRESET,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_TRANSLATION_STYLE,
)
from xvt.events import EventSink, EventType, create_event, error_event_fields, job_event_fields
from xvt.job_store import get_job_directory, get_job_file, load_job, save_job
from xvt.models import Artifact, Job, StageName, StageStatus
from xvt.pipeline import (
    apply_subtitle_translation,
    burn_translated_job,
    complete_translated_outputs,
    is_subtitle_input,
    resume_job,
    resume_remote_transcription,
    run_transcription,
    run_translation,
)
from xvt.recovery import file_checksum, subtitle_qa_failure_guidance, valid_artifact_path
from xvt.segment import SEGMENTATION_PRESETS
from xvt.translate import TranslationPolicy, load_translation_policy
from xvt.workflow_config import (
    WorkflowConfig,
    clear_burn_fingerprint,
    get_workflow_config,
    require_workflow_config,
)


def _translation_policy(config: WorkflowConfig) -> TranslationPolicy:
    """缺失策略快照时与历史行为一致，回退为默认 TranslationPolicy。"""

    if config.translation_policy is not None:
        return config.translation_policy
    return TranslationPolicy()


@dataclass(frozen=True, slots=True)
class WorkflowOptions:
    input: str
    output: str | Path
    engine: str = DEFAULT_ENGINE
    language: str | None = None
    subtitles: str | Path | None = None
    source_strategy: str = "auto"
    align_subtitles: bool = False
    minimum_alignment_confidence: float = DEFAULT_MIN_ALIGNMENT_CONFIDENCE
    allow_low_confidence_alignment: bool = False
    cookies_from_browser: str | None = None
    proxy: str | None = None
    youtube_subtitles: bool = False
    subtitle_language: str | None = None
    allow_auto_subtitles: bool = False
    translation_subtitles: str | Path | None = None
    target_language: str | None = None
    provider: str = DEFAULT_PROVIDER
    base_url: str | None = None
    model: str | None = None
    api_key_env: str = DEFAULT_API_KEY_ENV
    batch_size: int = DEFAULT_BATCH_SIZE
    max_retries: int = DEFAULT_MAX_RETRIES
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    glossary: str | Path | None = None
    translation_style: str = DEFAULT_TRANSLATION_STYLE
    segmentation_preset: str = DEFAULT_SEGMENTATION_PRESET
    burn: bool = False
    burn_media: str | Path | None = None
    burn_output: str | Path | None = None
    hardware_encoding: bool = DEFAULT_HARDWARE_ENCODING
    target_font: str | None = None
    source_font: str | None = None

    def validate(self) -> None:
        subtitle_input = is_subtitle_input(self.input)
        if subtitle_input and self.subtitles:
            raise ValueError("subtitle input cannot be combined with --subtitles")
        if subtitle_input and self.align_subtitles:
            raise ValueError("--align-subtitles requires media input")
        if subtitle_input and self.source_strategy == "asr":
            raise ValueError("--source-strategy asr requires media input")
        if subtitle_input and self.burn:
            raise ValueError("--burn requires media input")
        if self.align_subtitles and not self.subtitles:
            raise ValueError("--align-subtitles requires --subtitles")
        if self.source_strategy == "asr" and self.align_subtitles:
            raise ValueError("--source-strategy asr cannot be combined with --align-subtitles")
        if self.allow_low_confidence_alignment and not self.align_subtitles:
            raise ValueError("--allow-low-confidence-alignment requires --align-subtitles")
        if not 0 <= self.minimum_alignment_confidence <= 1:
            raise ValueError("--min-alignment-confidence must be between 0 and 1")
        if self.youtube_subtitles and self.subtitles:
            raise ValueError("--youtube-subtitles cannot be combined with --subtitles")
        if self.youtube_subtitles and not self.subtitle_language:
            raise ValueError("--youtube-subtitles requires --subtitle-language")
        if self.allow_auto_subtitles and not self.youtube_subtitles:
            raise ValueError("--allow-auto-subtitles requires --youtube-subtitles")
        if self.translation_subtitles and not self.target_language:
            raise ValueError("--translation-subtitles requires --target-language")
        if self.burn and not self.target_language:
            raise ValueError("--burn requires --target-language")
        if self.burn_media and not self.burn:
            raise ValueError("--burn-media requires --burn")
        if self.burn_output and not self.burn:
            raise ValueError("--burn-output requires --burn")
        if not self.hardware_encoding and not self.burn:
            raise ValueError("--software requires --burn")
        if (self.target_font or self.source_font) and not self.burn:
            raise ValueError("--target-font and --source-font require --burn")
        if (self.base_url or self.model) and not self.target_language:
            raise ValueError("--base-url and --model require --target-language")
        if self.glossary and not self.target_language:
            raise ValueError("--glossary requires --target-language")
        if self.glossary and self.translation_subtitles:
            raise ValueError("--glossary cannot be combined with --translation-subtitles")
        if self.segmentation_preset not in SEGMENTATION_PRESETS:
            raise ValueError("--segmentation-preset must be conservative, balanced, or compact")
        if self.source_strategy not in {"auto", "subtitles", "asr"}:
            raise ValueError("--source-strategy must be auto, subtitles, or asr")

    def persisted_config(self, policy: TranslationPolicy | None = None) -> WorkflowConfig:
        requested_outputs: list[str] = []
        if self.target_language:
            requested_outputs.extend(["translated_subtitles", "bilingual_subtitles"])
        if self.burn:
            requested_outputs.append("burned_video")
        return WorkflowConfig(
            target_language=self.target_language,
            requested_outputs=tuple(requested_outputs),
            translation_subtitles=(
                str(Path(self.translation_subtitles).expanduser().resolve())
                if self.translation_subtitles
                else None
            ),
            provider=self.provider,
            base_url=self.base_url,
            model=self.model,
            api_key_env=self.api_key_env,
            batch_size=self.batch_size,
            max_retries=self.max_retries,
            timeout_seconds=self.timeout_seconds,
            translation_policy=policy,
            segmentation_preset=self.segmentation_preset,
            burn=self.burn,
            burn_media=(
                str(Path(self.burn_media).expanduser().resolve()) if self.burn_media else None
            ),
            burn_output=(
                str(Path(self.burn_output).expanduser().resolve()) if self.burn_output else None
            ),
            hardware_encoding=self.hardware_encoding,
            target_font=self.target_font,
            source_font=self.source_font,
        )


@dataclass(frozen=True, slots=True)
class WorkflowResult:
    status: str
    job_file: Path
    artifacts: tuple[Path, ...] = ()
    message: str | None = None


def _error_category(error: Exception) -> str:
    return getattr(error, "category", "workflow")


def _artifact_checksum(job: Job, kind: str) -> str | None:
    artifact = next((item for item in job.artifacts if item.kind == kind), None)
    return artifact.checksum if artifact else None


def _source_selection_satisfied(job: Job) -> bool:
    path = valid_artifact_path(job, "source_selection")
    source_checksum = _artifact_checksum(job, "source_cues")
    if path is None or source_checksum is None:
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict) or data.get("source_cues_checksum") != source_checksum:
        return False
    return (
        data.get("candidate_kind") == "none"
        or job.config.get("input_kind") == "subtitle"
        or valid_artifact_path(job, "source_candidate_qa") is not None
    )


def _burn_fingerprint(job: Job, config: WorkflowConfig) -> str | None:
    translated_checksum = _artifact_checksum(job, "translated_cues")
    if not translated_checksum:
        return None

    media_value = config.burn_media or job.input
    parsed = urlparse(str(media_value))
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        media_identity: object = _artifact_checksum(job, "source_video") or str(media_value)
    else:
        media_path = Path(str(media_value)).expanduser()
        if not media_path.is_file():
            return None
        stat = media_path.stat()
        media_identity = {
            "path": str(media_path.resolve()),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
    payload = {
        "translated_cues_checksum": translated_checksum,
        "media": media_identity,
        "hardware_encoding": config.hardware_encoding,
        "target_font": config.target_font,
        "source_font": config.source_font,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _stored_burn_fingerprint(job: Job) -> str | None:
    """读取已登记的烧录指纹：优先 checksum 有效的 artifact，回退旧 workflow 字段。"""

    path = valid_artifact_path(job, "burn_fingerprint")
    if path is not None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            return None
        if not isinstance(data, dict):
            return None
        value = data.get("fingerprint")
        return value if isinstance(value, str) and value else None
    config = get_workflow_config(job)
    if config is not None and config.burn_fingerprint:
        return config.burn_fingerprint
    return None


def _record_burn_fingerprint(job_file: Path) -> None:
    """将派生指纹登记为 ``burn_fingerprint`` artifact，并清除历史 workflow 旁路字段。"""

    job = load_job(job_file)
    try:
        config = require_workflow_config(job)
    except ValueError:
        return
    fingerprint = _burn_fingerprint(job, config)
    if not fingerprint:
        return
    artifacts_directory = get_job_directory(job) / "artifacts"
    artifacts_directory.mkdir(parents=True, exist_ok=True)
    path = artifacts_directory / "burn-fingerprint.json"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps({"fingerprint": fingerprint}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    # 先清旧 artifact / workflow 旁路，再登记新文件，避免双写漂移。
    clear_burn_fingerprint(job)
    job.artifacts.append(Artifact("burn_fingerprint", str(path), file_checksum(path)))
    save_job(job)


def _current_artifacts(job: Job, *, waiting: bool = False) -> tuple[Path, ...]:
    kinds = (
        ("translation_batches",)
        if waiting
        else (
            "source_srt",
            "translated_srt",
            "bilingual_srt",
            "bilingual_ass",
            "burned_video",
        )
    )
    return tuple(path for kind in kinds if (path := valid_artifact_path(job, kind)))


@dataclass(frozen=True, slots=True)
class WorkflowGoals:
    """端到端推进的用户目标（从 WorkflowConfig 派生）。

    权威字段仍是 ``target_language`` / ``burn``，与历史 Job 和 continue 语义一致。
    ``requested_outputs`` 目前是与这两项同步写入的派生清单，供文档与未来 planner
    消费；决策不单独依赖它，避免无语言却要求译文的不一致状态。
    """

    needs_translation: bool
    needs_burn: bool


def workflow_goals(config: WorkflowConfig) -> WorkflowGoals:
    return WorkflowGoals(
        needs_translation=bool(config.target_language),
        needs_burn=bool(config.burn),
    )


def _translation_satisfied(job: Job) -> bool:
    """译文目标完成：cue 与用户可见的译文字幕文件均 checksum 有效。"""

    return (
        valid_artifact_path(job, "translated_cues") is not None
        and valid_artifact_path(job, "translated_srt") is not None
        and valid_artifact_path(job, "bilingual_srt") is not None
    )


def _burn_satisfied(job: Job, config: WorkflowConfig) -> bool:
    """烧录目标完成：成品 checksum 有效、媒体 QA 通过、派生指纹匹配。"""

    if valid_artifact_path(job, "burned_video") is None:
        return False
    if job.stages[StageName.MEDIA_QA].status is not StageStatus.SUCCEEDED:
        return False
    return _stored_burn_fingerprint(job) == _burn_fingerprint(job, config)


def plan_advance_steps(job: Job, config: WorkflowConfig) -> tuple[str, ...]:
    """根据当前 Job 与持久化目标，列出仍需执行的推进步骤。

    返回值为有序步骤名：
    - ``await_agent_translation``：已进入 Agent 等待，调用方应停止并提示回填
    - ``import_translation`` / ``translate``：补齐译文 cue
    - ``render``：已有译文 cue，补齐 SRT/ASS 渲染产物
    - ``burn``：补齐或重做烧录

    空元组表示翻译/烧录目标已满足（转写腿由 continue 在进入本函数前处理）。
    """

    goals = workflow_goals(config)
    steps: list[str] = []
    if goals.needs_translation and not _translation_satisfied(job):
        if valid_artifact_path(job, "translated_cues") is not None:
            steps.append("render")
        elif job.stages[StageName.TRANSLATE].status is StageStatus.AWAITING_INPUT:
            return ("await_agent_translation",)
        else:
            steps.append("import_translation" if config.translation_subtitles else "translate")
    if goals.needs_burn and not _burn_satisfied(job, config):
        steps.append("burn")
    return tuple(steps)


def planned_run_steps(options: WorkflowOptions) -> list[str]:
    """``xvt run`` 首跑的完整计划（含转写腿），用于 WORKFLOW_PLANNED 事件。"""

    steps = ["import_subtitles" if is_subtitle_input(options.input) else "transcribe"]
    if options.target_language:
        steps.append("import_translation" if options.translation_subtitles else "translate")
    if options.burn:
        steps.append("burn")
    return steps


def _emit_waiting(
    *,
    job: Job,
    path: Path,
    message: str,
    artifacts: tuple[Path, ...],
    event_sink: EventSink | None,
) -> WorkflowResult:
    if event_sink:
        event_sink(
            create_event(
                EventType.WORKFLOW_WAITING,
                **job_event_fields(job.id, path),
                reason=message,
                artifacts=[str(item) for item in artifacts],
            )
        )
    return WorkflowResult("awaiting_input", path, artifacts, message)


def _ensure_translation(
    path: Path,
    job: Job,
    config: WorkflowConfig,
    *,
    event_sink: EventSink | None,
) -> WorkflowResult | None:
    """补齐译文。返回 WorkflowResult 表示应停止（Agent 等待）；None 表示已推进。"""

    target_language = config.target_language
    if not target_language:
        return None

    # cues 已落盘但缺 SRT：只补 QA+渲染，禁止再次调用翻译 API。
    if valid_artifact_path(job, "translated_cues") is not None:
        complete_translated_outputs(path, event_sink=event_sink)
        return None

    if job.stages[StageName.TRANSLATE].status is StageStatus.AWAITING_INPUT:
        return _emit_waiting(
            job=job,
            path=path,
            message="translation requires Agent input",
            artifacts=_current_artifacts(job, waiting=True),
            event_sink=event_sink,
        )

    translation_subtitles = (
        valid_artifact_path(job, "input_translation_subtitles") or config.translation_subtitles
    )
    if translation_subtitles:
        apply_subtitle_translation(
            path,
            translation_subtitles,
            target_language=target_language,
            event_sink=event_sink,
        )
        return None

    translation = run_translation(
        path,
        target_language=target_language,
        provider=config.provider,
        base_url=config.base_url,
        model=config.model,
        api_key_env=config.api_key_env,
        batch_size=config.batch_size,
        max_retries=config.max_retries,
        timeout_seconds=config.timeout_seconds,
        policy=_translation_policy(config),
        event_sink=event_sink,
    )
    if translation.mode == "agent":
        return _emit_waiting(
            job=job,
            path=path,
            message=translation.fallback_reason or "translation requires Agent input",
            artifacts=translation.paths,
            event_sink=event_sink,
        )
    return None


def _ensure_burn(
    path: Path,
    job: Job,
    config: WorkflowConfig,
    *,
    cookies_from_browser: str | None,
    proxy: str | None,
    event_sink: EventSink | None,
) -> None:
    burn_translated_job(
        path,
        media=config.burn_media,
        output=config.burn_output,
        hardware_encoding=config.hardware_encoding,
        cookies_from_browser=cookies_from_browser or job.config.get("cookies_from_browser"),
        proxy=proxy,
        target_font=config.target_font,
        source_font=config.source_font,
        event_sink=event_sink,
    )
    _record_burn_fingerprint(path)


def _advance_workflow(
    path: Path,
    *,
    config: WorkflowConfig | None = None,
    cookies_from_browser: str | None,
    proxy: str | None,
    event_sink: EventSink | None,
) -> WorkflowResult:
    """按持久化目标推进翻译与烧录，只补齐缺失或失效的产物。

    run 首跑与 continue 续跑共用本函数：转写完成后的所有推进决策（Agent 等待、
    译文回填、指纹感知的烧录复用）只有这一份实现。``config`` 为 None 时从
    Job 读取持久化目标（continue 路径）；run 首跑直接传入刚构造的同一份配置。

    满足度判定与 ``plan_advance_steps`` 共用 ``workflow_goals`` /
    ``_translation_satisfied`` / ``_burn_satisfied``。翻译成功后必须重新判定烧录
    （译文 checksum 变化会使旧指纹失效）；因此执行路径不会「冻结」入口时刻的 plan。
    """
    job = load_job(path)
    if config is None:
        config = require_workflow_config(job)
    goals = workflow_goals(config)

    guidance = subtitle_qa_failure_guidance(job, path)
    if guidance is not None:
        raise RuntimeError(guidance.message)

    # 与 plan_advance_steps 一致：Agent 等待是终态快照，不得继续烧录。
    if (
        goals.needs_translation
        and not _translation_satisfied(job)
        and job.stages[StageName.TRANSLATE].status is StageStatus.AWAITING_INPUT
        and valid_artifact_path(job, "translated_cues") is None
    ):
        return _emit_waiting(
            job=job,
            path=path,
            message="translation requires Agent input",
            artifacts=_current_artifacts(job, waiting=True),
            event_sink=event_sink,
        )

    if goals.needs_translation and not _translation_satisfied(job):
        waiting = _ensure_translation(path, job, config, event_sink=event_sink)
        if waiting is not None:
            return waiting
        job = load_job(path)

    # 翻译副作用之后重新判定：不可使用入口处缓存的 plan 步骤表。
    if goals.needs_burn and not _burn_satisfied(job, config):
        _ensure_burn(
            path,
            job,
            config,
            cookies_from_browser=cookies_from_browser,
            proxy=proxy,
            event_sink=event_sink,
        )
        job = load_job(path)

    artifacts = _current_artifacts(job)
    if event_sink:
        event_sink(
            create_event(
                EventType.WORKFLOW_COMPLETED,
                **job_event_fields(job.id, path),
                artifacts=[str(item) for item in artifacts],
            )
        )
    return WorkflowResult("completed", path, artifacts)


def run_workflow(
    options: WorkflowOptions,
    *,
    event_sink: EventSink | None = None,
) -> WorkflowResult:
    options.validate()
    policy = (
        load_translation_policy(options.glossary, style=options.translation_style)
        if options.target_language and not options.translation_subtitles
        else None
    )
    steps = planned_run_steps(options)
    if event_sink:
        event_sink(create_event(EventType.WORKFLOW_STARTED, **job_event_fields(None, None)))
        event_sink(create_event(EventType.WORKFLOW_PLANNED, steps=steps))

    job_file: Path | None = None
    job_id: str | None = None
    workflow_config = options.persisted_config(policy)
    try:
        job = run_transcription(
            options.input,
            options.output,
            engine=options.engine,
            language=options.language,
            subtitles=options.subtitles,
            source_strategy=options.source_strategy,
            align_subtitles=options.align_subtitles,
            minimum_alignment_confidence=options.minimum_alignment_confidence,
            allow_low_confidence_alignment=options.allow_low_confidence_alignment,
            segmentation_preset=options.segmentation_preset,
            cookies_from_browser=options.cookies_from_browser,
            proxy=options.proxy,
            youtube_subtitles=options.youtube_subtitles,
            subtitle_language=options.subtitle_language,
            allow_auto_subtitles=options.allow_auto_subtitles,
            workflow_config=workflow_config.to_dict(),
            event_sink=event_sink,
            emit_job_completed=not options.target_language,
        )
        job_file = get_job_file(job)
        job_id = job.id
        return _advance_workflow(
            job_file,
            config=workflow_config,
            cookies_from_browser=options.cookies_from_browser,
            proxy=options.proxy,
            event_sink=event_sink,
        )
    except Exception as error:
        if event_sink:
            event_sink(
                create_event(
                    EventType.WORKFLOW_FAILED,
                    **job_event_fields(job_id, job_file),
                    **error_event_fields(_error_category(error), str(error)),
                )
            )
        raise


def continue_workflow(
    job_file: str | Path,
    *,
    cookies_from_browser: str | None = None,
    proxy: str | None = None,
    event_sink: EventSink | None = None,
) -> WorkflowResult:
    path = Path(job_file).expanduser().resolve()
    job = load_job(path)
    require_workflow_config(job)

    if event_sink:
        event_sink(
            create_event(
                EventType.WORKFLOW_STARTED,
                **job_event_fields(job.id, path),
                continuation=True,
            )
        )
    try:
        # source_srt 缺失说明转写腿在 QA/渲染处中断（例如 QA needs_review 后
        # 被人工确认放行）：与缺 source_cues 一样先补齐转写腿再推进。
        source_cues = valid_artifact_path(job, "source_cues")
        source_srt = valid_artifact_path(job, "source_srt")
        source_selection_missing = (
            "source_strategy" in job.config and not _source_selection_satisfied(job)
        )
        if source_cues is None or source_srt is None or source_selection_missing:
            if source_cues is not None:
                resume_job(path, event_sink=event_sink)
            else:
                parsed_input = urlparse(job.input)
                media_info = valid_artifact_path(job, "media_info")
                transcription_checkpoint = valid_artifact_path(
                    job, "word_transcript"
                ) or valid_artifact_path(job, "normalized_audio")
                if (
                    parsed_input.scheme in {"http", "https"}
                    and parsed_input.hostname
                    and not (media_info and transcription_checkpoint)
                ):
                    resume_remote_transcription(
                        path,
                        cookies_from_browser=cookies_from_browser,
                        proxy=proxy,
                        event_sink=event_sink,
                    )
                else:
                    resume_job(path, event_sink=event_sink)

        return _advance_workflow(
            path,
            cookies_from_browser=cookies_from_browser,
            proxy=proxy,
            event_sink=event_sink,
        )
    except Exception as error:
        if event_sink:
            event_sink(
                create_event(
                    EventType.WORKFLOW_FAILED,
                    **job_event_fields(job.id, path),
                    **error_event_fields(_error_category(error), str(error)),
                )
            )
        raise

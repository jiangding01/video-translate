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
from xvt.job_store import get_job_file, load_job, save_job
from xvt.models import Job, StageName, StageStatus
from xvt.pipeline import (
    apply_subtitle_translation,
    burn_translated_job,
    is_subtitle_input,
    resume_job,
    resume_remote_transcription,
    run_transcription,
    run_translation,
)
from xvt.recovery import valid_artifact_path
from xvt.segment import SEGMENTATION_PRESETS
from xvt.translate import TranslationPolicy, load_translation_policy, translation_policy_from_dict


@dataclass(frozen=True, slots=True)
class WorkflowOptions:
    input: str
    output: str | Path
    engine: str = DEFAULT_ENGINE
    language: str | None = None
    subtitles: str | Path | None = None
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
        if subtitle_input and self.burn:
            raise ValueError("--burn requires media input")
        if self.align_subtitles and not self.subtitles:
            raise ValueError("--align-subtitles requires --subtitles")
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

    def persisted_config(self, policy: TranslationPolicy | None = None) -> dict[str, object]:
        requested_outputs = []
        if self.target_language:
            requested_outputs.extend(["translated_subtitles", "bilingual_subtitles"])
        if self.burn:
            requested_outputs.append("burned_video")
        return {
            "target_language": self.target_language,
            "requested_outputs": requested_outputs,
            "translation_subtitles": (
                str(Path(self.translation_subtitles).expanduser().resolve())
                if self.translation_subtitles
                else None
            ),
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "api_key_env": self.api_key_env,
            "batch_size": self.batch_size,
            "max_retries": self.max_retries,
            "timeout_seconds": self.timeout_seconds,
            "translation_policy": policy.to_dict() if policy else None,
            "segmentation_preset": self.segmentation_preset,
            "burn": self.burn,
            "burn_media": (
                str(Path(self.burn_media).expanduser().resolve()) if self.burn_media else None
            ),
            "burn_output": (
                str(Path(self.burn_output).expanduser().resolve()) if self.burn_output else None
            ),
            "hardware_encoding": self.hardware_encoding,
            "target_font": self.target_font,
            "source_font": self.source_font,
        }


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


def _burn_fingerprint(job: Job, config: dict[str, object]) -> str | None:
    translated_checksum = _artifact_checksum(job, "translated_cues")
    if not translated_checksum:
        return None

    media_value = config.get("burn_media") or job.input
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
        "hardware_encoding": config.get("hardware_encoding", DEFAULT_HARDWARE_ENCODING),
        "target_font": config.get("target_font"),
        "source_font": config.get("source_font"),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _record_burn_fingerprint(job_file: Path) -> None:
    job = load_job(job_file)
    config = job.config.get("workflow")
    if not isinstance(config, dict):
        return
    fingerprint = _burn_fingerprint(job, config)
    if fingerprint:
        config["burn_fingerprint"] = fingerprint
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
    steps = ["import_subtitles"] if is_subtitle_input(options.input) else ["transcribe"]
    if options.target_language:
        steps.append("import_translation" if options.translation_subtitles else "translate")
    if options.burn:
        steps.append("burn")
    if event_sink:
        event_sink(create_event(EventType.WORKFLOW_STARTED, **job_event_fields(None, None)))
        event_sink(create_event(EventType.WORKFLOW_PLANNED, steps=steps))

    job_file: Path | None = None
    job_id: str | None = None
    artifacts: list[Path] = []
    try:
        job = run_transcription(
            options.input,
            options.output,
            engine=options.engine,
            language=options.language,
            subtitles=options.subtitles,
            align_subtitles=options.align_subtitles,
            minimum_alignment_confidence=options.minimum_alignment_confidence,
            allow_low_confidence_alignment=options.allow_low_confidence_alignment,
            segmentation_preset=options.segmentation_preset,
            cookies_from_browser=options.cookies_from_browser,
            proxy=options.proxy,
            youtube_subtitles=options.youtube_subtitles,
            subtitle_language=options.subtitle_language,
            allow_auto_subtitles=options.allow_auto_subtitles,
            workflow_config=options.persisted_config(policy),
            event_sink=event_sink,
            emit_job_completed=not options.target_language,
        )
        job_file = get_job_file(job)
        job_id = job.id
        artifacts.extend(_current_artifacts(job))

        if options.translation_subtitles:
            artifacts.extend(
                apply_subtitle_translation(
                    job_file,
                    options.translation_subtitles,
                    target_language=options.target_language or "",
                    event_sink=event_sink,
                )
            )
        elif options.target_language:
            translation = run_translation(
                job_file,
                target_language=options.target_language,
                provider=options.provider,
                base_url=options.base_url,
                model=options.model,
                api_key_env=options.api_key_env,
                batch_size=options.batch_size,
                max_retries=options.max_retries,
                timeout_seconds=options.timeout_seconds,
                policy=policy,
                event_sink=event_sink,
            )
            artifacts.extend(translation.paths)
            if translation.mode == "agent":
                message = translation.fallback_reason or "translation requires Agent input"
                if event_sink:
                    event_sink(
                        create_event(
                            EventType.WORKFLOW_WAITING,
                            **job_event_fields(job_id, job_file),
                            reason=message,
                            artifacts=[str(path) for path in artifacts],
                        )
                    )
                return WorkflowResult("awaiting_input", job_file, tuple(artifacts), message)

        if options.burn:
            artifacts.append(
                burn_translated_job(
                    job_file,
                    media=options.burn_media,
                    output=options.burn_output,
                    hardware_encoding=options.hardware_encoding,
                    cookies_from_browser=options.cookies_from_browser,
                    proxy=options.proxy,
                    target_font=options.target_font,
                    source_font=options.source_font,
                    event_sink=event_sink,
                )
            )
            _record_burn_fingerprint(job_file)

        if event_sink:
            event_sink(
                create_event(
                    EventType.WORKFLOW_COMPLETED,
                    **job_event_fields(job_id, job_file),
                    artifacts=[str(path) for path in artifacts],
                )
            )
        return WorkflowResult("completed", job_file, tuple(artifacts))
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
    config = job.config.get("workflow")
    if not isinstance(config, dict):
        raise ValueError("job has no persisted workflow goal; use the atomic resume command")
    target_language = config.get("target_language")
    if target_language is not None and not isinstance(target_language, str):
        raise ValueError("persisted workflow target_language is invalid")
    policy = translation_policy_from_dict(config.get("translation_policy"))

    if event_sink:
        event_sink(
            create_event(
                EventType.WORKFLOW_STARTED,
                **job_event_fields(job.id, path),
                continuation=True,
            )
        )
    try:
        if valid_artifact_path(job, "source_cues") is None:
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
            job = load_job(path)

        if job.stages[StageName.SUBTITLE_QA].status is StageStatus.FAILED:
            raise RuntimeError("subtitle QA failed; inspect and repair subtitles before continuing")

        if target_language and valid_artifact_path(job, "translated_cues") is None:
            if job.stages[StageName.TRANSLATE].status is StageStatus.AWAITING_INPUT:
                artifacts = _current_artifacts(job, waiting=True)
                message = "translation requires Agent input"
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

            translation_subtitles = config.get("translation_subtitles")
            if translation_subtitles:
                apply_subtitle_translation(
                    path,
                    str(translation_subtitles),
                    target_language=target_language,
                    event_sink=event_sink,
                )
            else:
                translation = run_translation(
                    path,
                    target_language=target_language,
                    provider=str(config.get("provider", DEFAULT_PROVIDER)),
                    base_url=str(config["base_url"]) if config.get("base_url") else None,
                    model=str(config["model"]) if config.get("model") else None,
                    api_key_env=str(config.get("api_key_env", DEFAULT_API_KEY_ENV)),
                    batch_size=int(config.get("batch_size", DEFAULT_BATCH_SIZE)),
                    max_retries=int(config.get("max_retries", DEFAULT_MAX_RETRIES)),
                    timeout_seconds=float(config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
                    policy=policy,
                    event_sink=event_sink,
                )
                if translation.mode == "agent":
                    message = translation.fallback_reason or "translation requires Agent input"
                    if event_sink:
                        event_sink(
                            create_event(
                                EventType.WORKFLOW_WAITING,
                                **job_event_fields(job.id, path),
                                reason=message,
                                artifacts=[str(item) for item in translation.paths],
                            )
                        )
                    return WorkflowResult("awaiting_input", path, translation.paths, message)
            job = load_job(path)

        if config.get("burn"):
            burned = valid_artifact_path(job, "burned_video")
            expected_fingerprint = _burn_fingerprint(job, config)
            media_qa_succeeded = job.stages[StageName.MEDIA_QA].status is StageStatus.SUCCEEDED
            if (
                not burned
                or not media_qa_succeeded
                or config.get("burn_fingerprint") != expected_fingerprint
            ):
                burn_translated_job(
                    path,
                    media=str(config["burn_media"]) if config.get("burn_media") else None,
                    output=str(config["burn_output"]) if config.get("burn_output") else None,
                    hardware_encoding=bool(
                        config.get("hardware_encoding", DEFAULT_HARDWARE_ENCODING)
                    ),
                    cookies_from_browser=cookies_from_browser
                    or job.config.get("cookies_from_browser"),
                    proxy=proxy,
                    target_font=str(config["target_font"]) if config.get("target_font") else None,
                    source_font=str(config["source_font"]) if config.get("source_font") else None,
                    event_sink=event_sink,
                )
                _record_burn_fingerprint(path)
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

from __future__ import annotations

import hashlib
import json
import shutil
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from math import ceil, isfinite
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from xvt.artifacts import ArtifactKind, downstream_of
from xvt.burn import burn_subtitles, recommended_video_bitrate
from xvt.config import DEFAULT_HARDWARE_ENCODING
from xvt.download import download_remote_media
from xvt.events import EventSink, EventType, create_event, job_event_fields
from xvt.job_store import get_job_directory, get_job_file, load_job, save_job
from xvt.media import probe_media
from xvt.models import Artifact, Cue, Job, StageName, StageStatus
from xvt.pipeline.common import (
    _add_artifact,
    _artifact_path,
    _checksum,
    _error_category,
    _invalidate_preview_artifacts,
    _invalidate_rendered_artifacts,
    _load_cues,
    _set_stage,
    _write_json,
    inspect_input,
    interrupt_running_stage,
    is_subtitle_input,
)
from xvt.pipeline.review import _complete_qa_stage, acknowledge_subtitle_review
from xvt.pipeline.transcription import (
    resume_job,
    resume_remote_transcription,
    run_local_transcription,
    run_remote_transcription,
    run_subtitle_transcription,
    run_transcription,
)
from xvt.process import raise_if_cancelled
from xvt.qa import (
    check_subtitles,
    display_width,
    display_width_review_limit,
    target_overflow_cue_ids,
)
from xvt.recovery import valid_artifact_path
from xvt.render import AssStyleOptions, save_bilingual_ass
from xvt.segment import split_cue_by_parts
from xvt.subtitles import load_subtitles, normalize_display_text, save_subtitles
from xvt.translate import (
    TranslationPolicy,
    apply_translations,
    build_cue_retranslation_batches,
    build_translation_batches,
    load_translation_policy,
    map_subtitle_translations,
    translation_policy_from_dict,
)
from xvt.translation_provider import (
    TRANSLATION_PROMPT_VERSION,
    ProviderResponseError,
    TranslationProviderError,
    resolve_provider,
    translate_batch,
)

__all__ = [
    "ResegmentationResult",
    "TranslationRunResult",
    "acknowledge_subtitle_review",
    "apply_agent_translation",
    "apply_cue_retranslation",
    "apply_subtitle_translation",
    "burn_translated_job",
    "inspect_input",
    "interrupt_running_stage",
    "is_subtitle_input",
    "prepare_agent_translation",
    "preview_translated_job",
    "resegment_translation",
    "resume_job",
    "resume_remote_transcription",
    "retranslate_cues",
    "run_local_transcription",
    "run_remote_transcription",
    "run_subtitle_transcription",
    "run_transcription",
    "run_translation",
]


@dataclass(frozen=True, slots=True)
class TranslationRunResult:
    mode: str
    paths: tuple[Path, ...]
    fallback_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ResegmentationResult:
    changed: bool
    source_cues_path: Path | None = None
    report_path: Path | None = None


def _required_glossary_terms(job: Job) -> tuple[tuple[str, str], ...]:
    policy = translation_policy_from_dict(job.config.get("translation_policy"))
    return tuple((term.source, term.target) for term in policy.terms)


def _load_translation_response(response_file: str | Path, expected_ids: set[int]) -> dict[int, str]:
    response = json.loads(Path(response_file).read_text(encoding="utf-8"))
    items = response.get("items") if isinstance(response, dict) else None
    if not isinstance(items, list):
        raise ValueError("translation response must contain an items array")
    translations = {}
    for item in items:
        cue_id = item.get("id") if isinstance(item, dict) else None
        text = item.get("text") if isinstance(item, dict) else None
        if not isinstance(cue_id, int) or cue_id in translations:
            raise ValueError("translation response contains an invalid or duplicate cue id")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"translation for cue {cue_id} is empty")
        translations[cue_id] = text.strip()
    if set(translations) != expected_ids:
        raise ValueError("translation response must cover the requested cue ids exactly once")
    return translations


def _finalize_translation(
    job: Job,
    translated: list[Cue],
    event_sink: EventSink | None,
    *,
    invalidate: Callable[[Job], None],
) -> tuple[Path, ...]:
    """翻译成功后的统一收尾：写译文 cue → 失效旧下游产物 → 译文 QA → 渲染。

    三条译文回填路径（局部重译、Agent/API 响应、导入译文字幕）共用本函数，
    保证 stage 记账、QA 参数与渲染行为完全一致；QA/渲染阶段的失败在此持久化。
    """
    current_stage = StageName.TRANSLATE
    try:
        artifacts_directory = get_job_directory(job) / "artifacts"
        translated_cues_path = artifacts_directory / "translated-cues.json"
        _write_json(translated_cues_path, [asdict(cue) for cue in translated])
        invalidate(job)
        _add_artifact(job, "translated_cues", translated_cues_path, event_sink)
        _set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)

        current_stage = StageName.SUBTITLE_QA
        _set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
        media_data = _media_data(job)
        report = check_subtitles(
            translated,
            media_duration_ms=_media_duration_ms(media_data),
            video_width=media_data.get("width"),
            video_height=media_data.get("height"),
            require_translation=True,
            required_terms=_required_glossary_terms(job),
        )
        qa_path = artifacts_directory / "qa-translated.json"
        _write_json(qa_path, asdict(report))
        _add_artifact(job, "translated_subtitle_qa", qa_path, event_sink)
        _complete_qa_stage(job, report, event_sink)

        current_stage = StageName.RENDER
        _set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
        paths = _render_translated_outputs(job, translated, media_data, event_sink)
        _set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)
        return paths
    except Exception as error:
        _set_stage(
            job,
            current_stage,
            StageStatus.FAILED,
            event_sink,
            error=str(error),
            error_category=_error_category(error),
        )
        raise


def apply_cue_retranslation(
    job_file: str | Path,
    response_file: str | Path,
    *,
    cue_ids: tuple[int, ...],
    target_language: str,
    event_sink: EventSink | None = None,
) -> tuple[Path, ...]:
    if not cue_ids:
        raise ValueError("at least one cue id is required")
    if len(set(cue_ids)) != len(cue_ids):
        raise ValueError("cue ids must not contain duplicates")
    job = load_job(job_file)
    current_stage = StageName.TRANSLATE
    _set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
    try:
        translated_cues = _load_cues(_artifact_path(job, "translated_cues"))
        translated_by_id = {cue.id: cue for cue in translated_cues}
        if len(translated_by_id) != len(translated_cues):
            raise ValueError("translated_cues contains duplicate cue ids")
        existing_languages = {cue.target_language for cue in translated_cues if cue.target_language}
        if existing_languages and existing_languages != {target_language}:
            raise ValueError("target language must match the existing translated cues")
        expected_ids = set(cue_ids)
        missing = sorted(expected_ids - translated_by_id.keys())
        if missing:
            raise ValueError(f"translated_cues does not contain ids: {missing}")
        translations = _load_translation_response(response_file, expected_ids)
        repaired = [
            replace(
                cue,
                target_text=translations[cue.id],
                target_language=target_language,
            )
            if cue.id in translations
            else cue
            for cue in translated_cues
        ]
    except Exception as error:
        _set_stage(
            job,
            current_stage,
            StageStatus.FAILED,
            event_sink,
            error=str(error),
            error_category=_error_category(error),
        )
        raise
    return _finalize_translation(
        job, repaired, event_sink, invalidate=_invalidate_rendered_artifacts
    )


def retranslate_cues(
    job_file: str | Path,
    *,
    cue_ids: tuple[int, ...],
    target_language: str,
    provider: str = "auto",
    base_url: str | None = None,
    model: str | None = None,
    api_key_env: str = "XVT_TRANSLATION_API_KEY",
    max_retries: int = 2,
    timeout_seconds: float = 60,
    event_sink: EventSink | None = None,
) -> TranslationRunResult:
    if max_retries < 0:
        raise ValueError("max_retries must not be negative")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    job = load_job(job_file)
    source_cues = _load_cues(_artifact_path(job, "source_cues"))
    translated_cues = _load_cues(_artifact_path(job, "translated_cues"))
    translated_ids = {cue.id for cue in translated_cues}
    missing = sorted(set(cue_ids) - translated_ids)
    if missing:
        raise ValueError(f"translated_cues does not contain ids: {missing}")
    existing_languages = {cue.target_language for cue in translated_cues if cue.target_language}
    if existing_languages and existing_languages != {target_language}:
        raise ValueError("target language must match the existing translated cues")
    source_language = next(
        (cue.source_language for cue in source_cues if cue.source_language),
        job.config.get("source_language", "auto"),
    )
    policy = translation_policy_from_dict(job.config.get("translation_policy"))
    batches = build_cue_retranslation_batches(
        source_cues,
        cue_ids,
        source_language=source_language,
        target_language=target_language,
        policy=policy,
    )
    mode, provider_config, fallback_reason = resolve_provider(
        provider,
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        timeout_seconds=timeout_seconds,
    )
    artifacts_directory = get_job_directory(job) / "artifacts"
    if mode == "agent":
        batches_path = artifacts_directory / "cue-retranslation-batches.json"
        _write_json(batches_path, {"batches": [batch.to_dict() for batch in batches]})
        _add_artifact(job, "cue_retranslation_batches", batches_path, event_sink)
        _set_stage(job, StageName.TRANSLATE, StageStatus.AWAITING_INPUT, event_sink)
        return TranslationRunResult("agent", (batches_path,), fallback_reason)
    if provider_config is None:
        raise RuntimeError("translation provider configuration was not resolved")

    translations = {}
    _set_stage(job, StageName.TRANSLATE, StageStatus.RUNNING, event_sink)
    try:
        for batch in batches:
            raise_if_cancelled()
            for attempt in range(max_retries + 1):
                try:
                    translated_batch = translate_batch(batch, provider_config)
                    break
                except TranslationProviderError as error:
                    if not error.retryable or attempt >= max_retries:
                        raise
                    time.sleep(
                        error.retry_after if error.retry_after is not None else min(2**attempt, 30)
                    )
                except (ValueError, ProviderResponseError):
                    if attempt >= max_retries:
                        raise
                    time.sleep(min(0.5 * (2**attempt), 4))
            translations.update(translated_batch)
    except Exception as error:
        _set_stage(
            job,
            StageName.TRANSLATE,
            StageStatus.FAILED,
            event_sink,
            error=str(error),
            error_category=_error_category(error),
        )
        raise

    response_path = artifacts_directory / "cue-retranslation-response.json"
    _write_json(
        response_path,
        {"items": [{"id": cue_id, "text": text} for cue_id, text in sorted(translations.items())]},
    )
    _add_artifact(job, "cue_retranslation_response", response_path, event_sink)
    paths = apply_cue_retranslation(
        job_file,
        response_path,
        cue_ids=cue_ids,
        target_language=target_language,
        event_sink=event_sink,
    )
    return TranslationRunResult(mode, (response_path, *paths))


def resegment_translation(
    job_file: str | Path,
    *,
    event_sink: EventSink | None = None,
) -> ResegmentationResult:
    job = load_job(job_file)
    if job.config.get("input_kind") == "subtitle":
        raise ValueError(
            "re-segmentation requires ASR word timestamps; subtitle-only jobs are unsupported"
        )
    workflow = job.config.get("workflow")
    if isinstance(workflow, dict) and workflow.get("translation_subtitles"):
        raise ValueError(
            "cannot re-segment a workflow using an imported translation subtitle file; "
            "provide a new translation for the re-segmented source cues"
        )

    source_path = valid_artifact_path(job, "source_cues")
    translated_path = valid_artifact_path(job, "translated_cues")
    media_path = valid_artifact_path(job, "media_info")
    if source_path is None or translated_path is None or media_path is None:
        raise ValueError(
            "re-segmentation requires valid source_cues, translated_cues, and media_info"
        )

    source_cues = _load_cues(source_path)
    translated_cues = _load_cues(translated_path)
    source_ids = {cue.id for cue in source_cues}
    if len(source_ids) != len(source_cues):
        raise ValueError("source_cues contains duplicate cue ids")
    translated_by_id = {cue.id: cue for cue in translated_cues}
    if len(translated_by_id) != len(translated_cues):
        raise ValueError("translated_cues contains duplicate cue ids")
    media_data = json.loads(media_path.read_text(encoding="utf-8"))
    overflow_ids = set(
        target_overflow_cue_ids(
            translated_cues,
            video_width=media_data.get("width"),
            video_height=media_data.get("height"),
        )
    )
    if not overflow_ids:
        return ResegmentationResult(False)
    if missing_source_ids := overflow_ids - source_ids:
        raise ValueError(
            f"translated_cues has no matching source cue ids: {sorted(missing_source_ids)}"
        )

    resegmented: list[Cue] = []
    report_items = []
    for source_cue in source_cues:
        if source_cue.id not in overflow_ids:
            resegmented.append(source_cue)
            continue
        translated_cue = translated_by_id.get(source_cue.id)
        if translated_cue is None or not translated_cue.target_text:
            raise ValueError(f"cue {source_cue.id} has no translated text to re-segment")
        if source_cue.provenance not in {"asr", "asr_resegmented"} or not source_cue.words:
            raise ValueError(
                f"cue {source_cue.id} has no ASR word timestamps; "
                "imported subtitles are not re-segmented"
            )
        target_language = translated_cue.target_language
        hard_limit = display_width_review_limit(
            target_language,
            video_width=media_data.get("width"),
            video_height=media_data.get("height"),
        )
        target_width = display_width(normalize_display_text(translated_cue.target_text))
        part_count = ceil(target_width / hard_limit)
        split_cues = split_cue_by_parts(source_cue, part_count)
        report_items.append(
            {
                "source_cue_id": source_cue.id,
                "new_cue_count": len(split_cues),
                "target_width": target_width,
                "hard_display_limit": hard_limit,
            }
        )
        resegmented.extend(split_cues)

    for cue_id, cue in enumerate(resegmented, 1):
        cue.id = cue_id

    artifacts_directory = get_job_directory(job) / "artifacts"
    source_output = artifacts_directory / "source-cues.json"
    _write_json(source_output, [asdict(cue) for cue in resegmented])
    source_srt_output = save_subtitles(resegmented, artifacts_directory / "source.srt")
    report_output = artifacts_directory / "translation-resegmentation.json"
    _write_json(
        report_output,
        {
            "source_cue_count": len(source_cues),
            "resegmented_cue_count": len(resegmented),
            "items": report_items,
        },
    )
    # source_cues 的下游闭包，扣除本函数原位重建的产物，并入被作废的用户输入。
    # cue_retranslation_batches/cue_retranslation_response 引用重分段前的废弃 cue id，
    # 虽然 apply-cue-retranslation 会因 translated_cues 已失效而拒绝执行（无正确性漏洞），
    # 但若不失效会以 checksum 有效的姿态污染 status/verify 与恢复计划的可复用产物清单
    # （P7e：修正 P6a 遗留的「维持现状」排除项）。
    invalidated_kinds = (
        downstream_of(ArtifactKind.SOURCE_CUES)
        - {
            # source_srt 由本函数在下方立即以新内容原位重建，不在此失效；
            # subtitle_qa 只重置 stage、不立即重建，旧报告引用废弃 cue 必须失效（P7f）。
            ArtifactKind.SOURCE_SRT,
        }
    ) | {
        # input_translation_subtitles 是被 resegment 作废的用户输入而非派生产物，显式并入。
        ArtifactKind.INPUT_TRANSLATION_SUBTITLES
    }
    replacement_artifacts = [
        Artifact("source_cues", str(source_output), _checksum(source_output)),
        Artifact("source_srt", str(source_srt_output), _checksum(source_srt_output)),
        Artifact("translation_resegmentation", str(report_output), _checksum(report_output)),
    ]
    replacement_kinds = {artifact.kind for artifact in replacement_artifacts}
    invalidated = [artifact for artifact in job.artifacts if artifact.kind in invalidated_kinds]
    job.artifacts = [
        artifact
        for artifact in job.artifacts
        if artifact.kind not in invalidated_kinds | replacement_kinds
    ]
    job.artifacts.extend(replacement_artifacts)
    if isinstance(workflow, dict):
        workflow.pop("burn_fingerprint", None)
    for stage in (
        StageName.TRANSLATE,
        StageName.SUBTITLE_QA,
        StageName.RENDER,
        StageName.BURN,
        StageName.MEDIA_QA,
    ):
        record = job.stages[stage]
        record.status = StageStatus.PENDING
        record.started_at = None
        record.completed_at = None
        record.error = None
    save_job(job)
    if event_sink:
        for artifact in replacement_artifacts:
            event_sink(
                create_event(
                    EventType.ARTIFACT_CREATED,
                    **job_event_fields(job.id, get_job_file(job)),
                    kind=artifact.kind,
                    path=artifact.path,
                )
            )
        for artifact in invalidated:
            event_sink(
                create_event(
                    EventType.ARTIFACT_INVALIDATED,
                    **job_event_fields(job.id, get_job_file(job)),
                    kind=artifact.kind,
                    path=artifact.path,
                )
            )
        event_sink(
            create_event(
                EventType.TRANSLATION_RESEGMENTED,
                **job_event_fields(job.id, get_job_file(job)),
                source_cue_count=len(source_cues),
                resegmented_cue_count=len(resegmented),
                report_path=str(report_output),
            )
        )
    return ResegmentationResult(True, source_output, report_output)


def _media_data(job: Job) -> dict[str, Any]:
    if job.config.get("input_kind") == "subtitle":
        return {}
    media_path = _artifact_path(job, "media_info")
    return json.loads(media_path.read_text(encoding="utf-8"))


def _media_duration_ms(media_data: dict[str, Any]) -> int | None:
    value = media_data.get("duration_ms")
    return int(value) if value is not None else None


def _render_translated_outputs(
    job: Job,
    translated: list[Cue],
    media_data: dict[str, Any],
    event_sink: EventSink | None,
) -> tuple[Path, ...]:
    artifacts_directory = get_job_directory(job) / "artifacts"
    translated_path = save_subtitles(
        translated,
        artifacts_directory / "translated.srt",
        mode="translated",
    )
    bilingual_path = save_subtitles(
        translated,
        artifacts_directory / "bilingual.srt",
        mode="bilingual",
    )
    _add_artifact(job, "translated_srt", translated_path, event_sink)
    _add_artifact(job, "bilingual_srt", bilingual_path, event_sink)
    paths = [translated_path, bilingual_path]

    width = int(media_data.get("width") or 0)
    height = int(media_data.get("height") or 0)
    if width > 0 and height > 0:
        ass_path = save_bilingual_ass(
            translated,
            artifacts_directory / "bilingual.ass",
            video_width=width,
            video_height=height,
        )
        _add_artifact(job, "bilingual_ass", ass_path, event_sink)
        paths.append(ass_path)
    return tuple(paths)


def prepare_agent_translation(
    job_file: str | Path,
    *,
    source_language: str,
    target_language: str,
    batch_size: int = 40,
    policy: TranslationPolicy | None = None,
    glossary: str | Path | None = None,
    translation_style: str = "faithful",
    event_sink: EventSink | None = None,
) -> Path:
    job = load_job(job_file)
    resolved_policy = policy or load_translation_policy(glossary, style=translation_style)
    _set_stage(job, StageName.TRANSLATE, StageStatus.RUNNING, event_sink)
    try:
        cues = _load_cues(_artifact_path(job, "source_cues"))
        batches = build_translation_batches(
            cues,
            source_language=source_language,
            target_language=target_language,
            batch_size=batch_size,
            policy=resolved_policy,
        )
        output_path = get_job_directory(job) / "artifacts" / "translation-batches.json"
        _write_json(
            output_path,
            {
                "job_id": job.id,
                "source_language": source_language,
                "target_language": target_language,
                "policy": resolved_policy.to_dict(),
                "batches": [batch.to_dict() for batch in batches],
            },
        )
        _add_artifact(job, "translation_batches", output_path, event_sink)
        job.config["target_language"] = target_language
        job.config["translation_policy"] = resolved_policy.to_dict()
        _set_stage(job, StageName.TRANSLATE, StageStatus.AWAITING_INPUT, event_sink)
        return output_path
    except Exception as error:
        _set_stage(
            job,
            StageName.TRANSLATE,
            StageStatus.FAILED,
            event_sink,
            error=str(error),
            error_category=_error_category(error),
        )
        raise


def run_translation(
    job_file: str | Path,
    *,
    target_language: str,
    provider: str = "auto",
    base_url: str | None = None,
    model: str | None = None,
    api_key_env: str = "XVT_TRANSLATION_API_KEY",
    batch_size: int = 40,
    max_retries: int = 2,
    timeout_seconds: float = 60,
    policy: TranslationPolicy | None = None,
    glossary: str | Path | None = None,
    translation_style: str = "faithful",
    event_sink: EventSink | None = None,
) -> TranslationRunResult:
    if max_retries < 0:
        raise ValueError("max_retries must not be negative")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    job = load_job(job_file)
    resolved_policy = policy or load_translation_policy(glossary, style=translation_style)
    cues = _load_cues(_artifact_path(job, "source_cues"))
    source_language = next(
        (cue.source_language for cue in cues if cue.source_language),
        job.config.get("source_language", "auto"),
    )
    mode, provider_config, fallback_reason = resolve_provider(
        provider,
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        timeout_seconds=timeout_seconds,
    )
    batches_path = prepare_agent_translation(
        job_file,
        source_language=source_language,
        target_language=target_language,
        batch_size=batch_size,
        policy=resolved_policy,
        event_sink=event_sink,
    )
    if mode == "agent":
        if fallback_reason and event_sink:
            event_sink(
                create_event(
                    EventType.TRANSLATION_FALLBACK,
                    **job_event_fields(job.id, get_job_file(job)),
                    mode="agent",
                    reason=fallback_reason,
                    path=str(batches_path),
                )
            )
        return TranslationRunResult("agent", (batches_path,), fallback_reason)

    if provider_config is None:
        raise RuntimeError("translation provider configuration was not resolved")
    job = load_job(job_file)
    batches = build_translation_batches(
        cues,
        source_language=source_language,
        target_language=target_language,
        batch_size=batch_size,
        policy=resolved_policy,
    )
    source_cues_checksum = _checksum(_artifact_path(job, "source_cues"))
    fingerprint_payload = {
        "source_cues_checksum": source_cues_checksum,
        "source_language": source_language,
        "target_language": target_language,
        "provider": mode,
        "base_url": provider_config.base_url,
        "model": provider_config.model,
        "batch_size": batch_size,
        "prompt_version": TRANSLATION_PROMPT_VERSION,
        "translation_policy": resolved_policy.to_dict(),
    }
    translation_fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    _set_stage(job, StageName.TRANSLATE, StageStatus.RUNNING, event_sink)
    if event_sink:
        event_sink(
            create_event(
                EventType.TRANSLATION_PROVIDER_SELECTED,
                **job_event_fields(job.id, get_job_file(job)),
                provider=mode,
                model=provider_config.model,
            )
        )

    progress_path = valid_artifact_path(job, "translation_progress")
    translations: dict[int, str] = {}
    completed_batches: set[int] = set()
    if progress_path:
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        if progress.get("fingerprint") == translation_fingerprint:
            completed_batches = {
                int(batch_id) for batch_id in progress.get("completed_batches", [])
            }
            translations = {
                int(item["id"]): str(item["text"])
                for item in progress.get("items", [])
                if isinstance(item, dict) and isinstance(item.get("id"), int)
            }
        elif event_sink:
            event_sink(
                create_event(
                    EventType.TRANSLATION_PROGRESS_RESET,
                    **job_event_fields(job.id, get_job_file(job)),
                    reason="translation fingerprint changed",
                )
            )

    artifacts_directory = get_job_directory(job) / "artifacts"
    progress_output = artifacts_directory / "translation-progress.json"
    batch_total = len(batches)
    try:
        for batch in batches:
            raise_if_cancelled()
            batch_ids = {item.id for item in batch.items}
            if batch.id in completed_batches and batch_ids <= translations.keys():
                continue
            if event_sink:
                event_sink(
                    create_event(
                        EventType.TRANSLATION_BATCH_STARTED,
                        **job_event_fields(job.id, get_job_file(job)),
                        batch=batch.id,
                        batch_total=batch_total,
                    )
                )
            for attempt in range(max_retries + 1):
                try:
                    if attempt == 0:
                        translated_batch = translate_batch(batch, provider_config)
                    else:
                        translated_batch = translate_batch(
                            batch,
                            provider_config,
                            correction=(
                                "The previous response was invalid. Return every requested id "
                                "exactly once with non-empty translated text."
                            ),
                        )
                    break
                except TranslationProviderError as error:
                    if not error.retryable or attempt >= max_retries:
                        raise
                    delay = (
                        error.retry_after if error.retry_after is not None else min(2**attempt, 30)
                    )
                    if event_sink:
                        event_sink(
                            create_event(
                                EventType.TRANSLATION_BATCH_RETRYING,
                                **job_event_fields(job.id, get_job_file(job)),
                                batch=batch.id,
                                batch_total=batch_total,
                                attempt=attempt + 2,
                                delay_seconds=delay,
                                reason="provider temporarily unavailable",
                            )
                        )
                    time.sleep(delay)
                except (ValueError, ProviderResponseError):
                    if attempt >= max_retries:
                        raise
                    delay = min(0.5 * (2**attempt), 4)
                    if event_sink:
                        event_sink(
                            create_event(
                                EventType.TRANSLATION_BATCH_RETRYING,
                                **job_event_fields(job.id, get_job_file(job)),
                                batch=batch.id,
                                batch_total=batch_total,
                                attempt=attempt + 2,
                                delay_seconds=delay,
                                reason="invalid provider response",
                            )
                        )
                    time.sleep(delay)
            translations.update(translated_batch)
            completed_batches.add(batch.id)
            _write_json(
                progress_output,
                {
                    "fingerprint": translation_fingerprint,
                    "completed_batches": sorted(completed_batches),
                    "items": [
                        {"id": cue_id, "text": text}
                        for cue_id, text in sorted(translations.items())
                    ],
                },
            )
            _add_artifact(job, "translation_progress", progress_output, event_sink)
            if event_sink:
                event_sink(
                    create_event(
                        EventType.TRANSLATION_BATCH_COMPLETED,
                        **job_event_fields(job.id, get_job_file(job)),
                        batch=batch.id,
                        batch_total=batch_total,
                    )
                )
    except Exception as error:
        _set_stage(
            job,
            StageName.TRANSLATE,
            StageStatus.FAILED,
            event_sink,
            error=str(error),
            error_category=_error_category(error),
        )
        raise

    response_path = artifacts_directory / "translation-response.json"
    _write_json(
        response_path,
        {"items": [{"id": cue_id, "text": text} for cue_id, text in sorted(translations.items())]},
    )
    _add_artifact(job, "translation_response", response_path, event_sink)
    rendered = apply_agent_translation(
        job_file,
        response_path,
        target_language=target_language,
        event_sink=event_sink,
    )
    return TranslationRunResult(mode, (response_path, *rendered))


def apply_agent_translation(
    job_file: str | Path,
    response_file: str | Path,
    *,
    target_language: str,
    event_sink: EventSink | None = None,
) -> tuple[Path, ...]:
    job = load_job(job_file)
    current_stage = StageName.TRANSLATE
    _set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
    try:
        cues = _load_cues(_artifact_path(job, "source_cues"))
        response = json.loads(Path(response_file).read_text(encoding="utf-8"))
        items = response.get("items") if isinstance(response, dict) else None
        if not isinstance(items, list):
            raise ValueError("translation response must contain an items array")
        translations = {}
        for item in items:
            cue_id = item.get("id") if isinstance(item, dict) else None
            text = item.get("text") if isinstance(item, dict) else None
            if not isinstance(cue_id, int) or cue_id in translations:
                raise ValueError("translation response contains an invalid or duplicate cue id")
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"translation for cue {cue_id} is empty")
            translations[cue_id] = text.strip()
        translated = apply_translations(cues, translations, target_language=target_language)
    except Exception as error:
        _set_stage(
            job,
            current_stage,
            StageStatus.FAILED,
            event_sink,
            error=str(error),
            error_category=_error_category(error),
        )
        raise
    return _finalize_translation(
        job, translated, event_sink, invalidate=_invalidate_preview_artifacts
    )


def apply_subtitle_translation(
    job_file: str | Path,
    subtitle_file: str | Path,
    *,
    target_language: str,
    event_sink: EventSink | None = None,
) -> tuple[Path, ...]:
    job = load_job(job_file)
    current_stage = StageName.TRANSLATE
    _set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
    try:
        source_cues = _load_cues(_artifact_path(job, "source_cues"))
        subtitle_path = Path(subtitle_file).expanduser()
        translated_cues = load_subtitles(
            subtitle_path,
            role="translated",
            target_language=target_language,
        )
        translations, mapping_report = map_subtitle_translations(source_cues, translated_cues)
        translated = apply_translations(
            source_cues,
            translations,
            target_language=target_language,
        )
        artifacts_directory = get_job_directory(job) / "artifacts"
        imported_path = artifacts_directory / f"input-translation{subtitle_path.suffix.lower()}"
        shutil.copy2(subtitle_path, imported_path)
        _add_artifact(job, "input_translation_subtitles", imported_path, event_sink)
        mapping_path = artifacts_directory / "translation-mapping.json"
        _write_json(mapping_path, mapping_report)
        _add_artifact(job, "translation_mapping", mapping_path, event_sink)
        job.config["target_language"] = target_language
    except Exception as error:
        _set_stage(
            job,
            current_stage,
            StageStatus.FAILED,
            event_sink,
            error=str(error),
            error_category=_error_category(error),
        )
        raise
    return _finalize_translation(
        job, translated, event_sink, invalidate=_invalidate_preview_artifacts
    )


def preview_translated_job(
    job_file: str | Path,
    *,
    cue_id: int | None = None,
    start_seconds: float | None = None,
    duration_seconds: float = 15,
    media: str | Path | None = None,
    output: str | Path | None = None,
    hardware_encoding: bool | None = None,
    target_font: str | None = None,
    source_font: str | None = None,
    event_sink: EventSink | None = None,
) -> Path:
    if cue_id is not None and start_seconds is not None:
        raise ValueError("--cue and --start cannot be used together")
    if cue_id is not None and cue_id < 1:
        raise ValueError("--cue must be a positive integer")
    if start_seconds is not None and (not isfinite(start_seconds) or start_seconds < 0):
        raise ValueError("--start must not be negative")
    if not isfinite(duration_seconds) or duration_seconds <= 0:
        raise ValueError("--duration must be positive")

    job = load_job(job_file)
    if job.config.get("input_kind") == "subtitle":
        raise ValueError("preview requires media input")
    translated_path = valid_artifact_path(job, "translated_cues")
    qa_path = valid_artifact_path(job, "translated_subtitle_qa")
    if translated_path is None or qa_path is None:
        raise ValueError("preview requires checksum-valid translated subtitles and QA")
    qa_data = json.loads(qa_path.read_text(encoding="utf-8"))
    if qa_data.get("status") not in {"pass", "warning"}:
        raise RuntimeError("translated subtitle QA must pass before preview")

    workflow_config = job.config.get("workflow")
    workflow_config = workflow_config if isinstance(workflow_config, dict) else {}
    if hardware_encoding is None:
        hardware_encoding = bool(
            workflow_config.get("hardware_encoding", DEFAULT_HARDWARE_ENCODING)
        )
    if target_font is None and isinstance(workflow_config.get("target_font"), str):
        target_font = workflow_config["target_font"]
    if source_font is None and isinstance(workflow_config.get("source_font"), str):
        source_font = workflow_config["source_font"]

    configured_media = workflow_config.get("burn_media")
    if media is not None:
        media_path = Path(media).expanduser()
    elif isinstance(configured_media, str) and configured_media:
        media_path = Path(configured_media).expanduser()
    else:
        parsed = urlparse(job.input)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            media_path = valid_artifact_path(job, "source_video")
            if media_path is None:
                raise ValueError(
                    "remote preview requires a checksum-valid source_video or explicit --media"
                )
        else:
            media_path = Path(job.input).expanduser()
    if not media_path.is_file():
        raise ValueError(f"video file does not exist: {media_path}")
    media_path = media_path.resolve()
    media_info = probe_media(media_path)
    if not media_info.has_video or not media_info.width or not media_info.height:
        raise ValueError("preview requires a video stream with known dimensions")
    if media_info.duration_ms <= 0:
        raise ValueError("preview requires media with a known positive duration")

    translated = _load_cues(translated_path)
    selected_cue = None
    if start_seconds is None:
        if cue_id is None:
            selected_cue = next((cue for cue in translated if cue.target_text), None)
            if selected_cue is None:
                raise ValueError("translated subtitles contain no previewable cues")
        else:
            selected_cue = next((cue for cue in translated if cue.id == cue_id), None)
            if selected_cue is None:
                raise ValueError(f"translated subtitles do not contain cue {cue_id}")

    requested_duration_ms = round(duration_seconds * 1000)
    if selected_cue is not None:
        preview_duration_ms = max(
            requested_duration_ms,
            selected_cue.end_ms - selected_cue.start_ms + 4000,
        )
        preview_duration_ms = min(preview_duration_ms, media_info.duration_ms)
        start_ms = max(selected_cue.start_ms - 2000, 0)
        end_ms = min(start_ms + preview_duration_ms, media_info.duration_ms)
        start_ms = max(end_ms - preview_duration_ms, 0)
    else:
        start_ms = round((start_seconds or 0) * 1000)
        if start_ms >= media_info.duration_ms:
            raise ValueError("--start must be before the end of the media")
        end_ms = min(start_ms + requested_duration_ms, media_info.duration_ms)

    preview_cues = [
        replace(
            cue,
            start_ms=max(cue.start_ms, start_ms) - start_ms,
            end_ms=min(cue.end_ms, end_ms) - start_ms,
        )
        for cue in translated
        if cue.start_ms < end_ms and cue.end_ms > start_ms and cue.target_text
    ]
    if not preview_cues:
        raise ValueError("preview range contains no translated subtitle cues")

    output_path = (
        Path(output).expanduser()
        if output is not None
        else get_job_directory(job) / "artifacts" / "preview.mp4"
    )
    if not output_path.is_absolute():
        raise ValueError("preview output path must be absolute")
    artifacts_directory = get_job_directory(job) / "artifacts"
    ass_path = artifacts_directory / "preview.ass"
    output_resolved = output_path.resolve()
    protected_paths = [
        media_path,
        get_job_file(job),
        ass_path,
        *(
            Path(artifact.path).expanduser()
            for artifact in job.artifacts
            if artifact.kind != "preview_video"
        ),
    ]
    for protected_path in protected_paths:
        protected_resolved = protected_path.resolve()
        same_file = output_resolved == protected_resolved
        if not same_file and output_path.exists() and protected_path.exists():
            try:
                same_file = output_path.samefile(protected_path)
            except OSError:
                same_file = False
        if same_file:
            raise ValueError(
                "preview output must not overwrite the source media, job file, "
                "or an existing artifact"
            )
    _invalidate_preview_artifacts(job)
    if event_sink:
        event_sink(
            create_event(
                EventType.PREVIEW_STARTED,
                **job_event_fields(job.id, get_job_file(job)),
                start_ms=start_ms,
                duration_ms=end_ms - start_ms,
            )
        )
    save_bilingual_ass(
        preview_cues,
        ass_path,
        video_width=media_info.width,
        video_height=media_info.height,
        options=AssStyleOptions(target_font=target_font, source_font=source_font),
    )
    _add_artifact(job, "preview_ass", ass_path, event_sink)
    preview_path = burn_subtitles(
        media_path,
        ass_path,
        output_path,
        hardware_encoding=hardware_encoding,
        video_bitrate=recommended_video_bitrate(media_info.height),
        duration_ms=end_ms - start_ms,
        start_ms=start_ms,
        clip_duration_ms=end_ms - start_ms,
        progress_event_type=EventType.PREVIEW_PROGRESS,
        event_sink=event_sink,
    )
    preview_info = probe_media(preview_path)
    issues = []
    if not preview_info.has_video:
        issues.append("preview has no video stream")
    if media_info.has_audio and not preview_info.has_audio:
        issues.append("preview has no audio stream")
    elif preview_info.has_audio and preview_info.audio_codec != "aac":
        issues.append("preview audio codec is not AAC")
    if (preview_info.width, preview_info.height) != (media_info.width, media_info.height):
        issues.append("preview dimensions differ from source video")
    if abs(preview_info.duration_ms - (end_ms - start_ms)) > 500:
        issues.append("preview duration differs by more than 500ms")
    if issues:
        raise RuntimeError(f"preview video failed media QA: {', '.join(issues)}")
    _add_artifact(job, "preview_video", preview_path, event_sink)
    if event_sink:
        event_sink(
            create_event(
                EventType.PREVIEW_COMPLETED,
                **job_event_fields(job.id, get_job_file(job)),
                path=str(preview_path),
                start_ms=start_ms,
                duration_ms=end_ms - start_ms,
            )
        )
    return preview_path


def burn_translated_job(
    job_file: str | Path,
    *,
    media: str | Path | None = None,
    output: str | Path | None = None,
    hardware_encoding: bool = True,
    cookies_from_browser: str | None = None,
    proxy: str | None = None,
    target_font: str | None = None,
    source_font: str | None = None,
    event_sink: EventSink | None = None,
) -> Path:
    job = load_job(job_file)
    if job.config.get("input_kind") == "subtitle":
        raise ValueError("burn-in requires media input")
    current_stage = StageName.RENDER
    try:
        if media:
            media_path = Path(media).expanduser()
        else:
            parsed = urlparse(job.input)
            if parsed.scheme in {"http", "https"} and parsed.hostname:
                media_path = valid_artifact_path(job, "source_video")
                if media_path is None:
                    media_path = download_remote_media(
                        job.input,
                        get_job_directory(job) / "input" / "video",
                        cookies_from_browser=cookies_from_browser,
                        proxy=proxy,
                        event_sink=event_sink,
                    )
                    _add_artifact(job, "source_video", media_path, event_sink)
            else:
                media_path = Path(job.input).expanduser()
        if not media_path.is_file():
            raise ValueError(f"video file does not exist: {media_path}")
        media_path = media_path.resolve()
        media_info = probe_media(media_path)
        if not media_info.has_video or not media_info.width or not media_info.height:
            raise ValueError("burn-in requires a video stream with known dimensions")

        translated = _load_cues(_artifact_path(job, "translated_cues"))
        _set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
        ass_path = save_bilingual_ass(
            translated,
            get_job_directory(job) / "artifacts" / "bilingual.ass",
            video_width=media_info.width,
            video_height=media_info.height,
            options=AssStyleOptions(target_font=target_font, source_font=source_font),
        )
        _add_artifact(job, "bilingual_ass", ass_path, event_sink)
        _set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)

        current_stage = StageName.BURN
        _set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
        output_path = (
            Path(output).expanduser()
            if output
            else get_job_directory(job) / "artifacts" / "burned.mp4"
        )
        burned_path = burn_subtitles(
            media_path,
            ass_path,
            output_path,
            hardware_encoding=hardware_encoding,
            video_bitrate=recommended_video_bitrate(media_info.height),
            duration_ms=media_info.duration_ms,
            event_sink=event_sink,
        )
        _add_artifact(job, "burned_video", burned_path, event_sink)
        _set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)

        current_stage = StageName.MEDIA_QA
        _set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
        burned_info = probe_media(burned_path)
        duration_delta_ms = abs(burned_info.duration_ms - media_info.duration_ms)
        media_report = {
            "status": "pass",
            "video_codec": burned_info.video_codec,
            "audio_codec": burned_info.audio_codec,
            "duration_delta_ms": duration_delta_ms,
            "issues": [],
        }
        if not burned_info.has_video:
            media_report["issues"].append("output has no video stream")
        if media_info.has_audio and not burned_info.has_audio:
            media_report["issues"].append("output has no audio stream")
        elif burned_info.has_audio and burned_info.audio_codec != "aac":
            media_report["issues"].append("output audio codec is not AAC")
        if (burned_info.width, burned_info.height) != (media_info.width, media_info.height):
            media_report["issues"].append("output dimensions differ from source video")
        if duration_delta_ms > 500:
            media_report["issues"].append("output duration differs by more than 500ms")
        if media_report["issues"]:
            media_report["status"] = "failed"
        report_path = get_job_directory(job) / "artifacts" / "media-qa.json"
        _write_json(report_path, media_report)
        _add_artifact(job, "media_qa", report_path, event_sink)
        if media_report["status"] == "failed":
            raise RuntimeError("burned video failed media QA")
        _set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)
        return burned_path
    except Exception as error:
        _set_stage(
            job,
            current_stage,
            StageStatus.FAILED,
            event_sink,
            error=str(error),
            error_category=_error_category(error),
        )
        raise

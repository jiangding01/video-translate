"""翻译主链：批次构建、兼容 API 调用、断点进度、Agent 回填、导入译文与统一收尾。

`_finalize_translation` 是三条译文回填路径（局部重译、Agent/API 响应、导入译文
字幕）共用的收尾：写译文 cue → 失效旧下游产物 → 译文 QA → 渲染。
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from xvt.config import DEFAULT_MAX_RETRIES
from xvt.events import EventSink, EventType, create_event, job_event_fields
from xvt.job_store import get_job_directory, get_job_file, load_job
from xvt.models import Cue, Job, StageName, StageStatus
from xvt.pipeline.common import (
    _add_artifact,
    _artifact_path,
    _checksum,
    _error_category,
    _invalidate_preview_artifacts,
    _load_cues,
    _set_stage,
    _write_json,
)
from xvt.pipeline.review import _complete_qa_stage
from xvt.process import raise_if_cancelled
from xvt.qa import check_subtitles
from xvt.recovery import valid_artifact_path
from xvt.render import save_bilingual_ass
from xvt.subtitles import load_subtitles, save_subtitles
from xvt.translate import (
    SEMANTIC_GROUPING_VERSION,
    TranslationBatch,
    TranslationPolicy,
    apply_translations,
    build_translation_batches,
    load_translation_policy,
    map_subtitle_translations,
    translation_policy_from_dict,
)
from xvt.translation_provider import (
    TRANSLATION_PROMPT_VERSION,
    CompatibleProviderConfig,
    ProviderResponseError,
    TranslationProviderError,
    resolve_provider,
    translate_batch,
)

_RESPONSE_CORRECTION = (
    "The previous response was invalid. Return every requested id "
    "exactly once with non-empty translated text."
)


@dataclass(frozen=True, slots=True)
class TranslationRunResult:
    mode: str
    paths: tuple[Path, ...]
    fallback_reason: str | None = None


def _translate_batch_with_retries(
    batch: TranslationBatch,
    provider_config: CompatibleProviderConfig,
    *,
    max_retries: int,
    on_retry: Callable[[int, float, str], None] | None = None,
) -> dict[int, str]:
    transport_retries = 0
    response_retries = 0
    request_attempt = 1
    correction: str | None = None
    while True:
        raise_if_cancelled()
        try:
            return translate_batch(batch, provider_config, correction=correction)
        except TranslationProviderError as error:
            if not error.retryable or transport_retries >= max_retries:
                raise
            delay = 30 if transport_retries >= 5 else 2**transport_retries
            if (
                error.retry_after is not None
                and math.isfinite(error.retry_after)
                and error.retry_after >= 0
            ):
                delay = min(error.retry_after, 30)
            transport_retries += 1
            request_attempt += 1
            correction = None
            if on_retry:
                on_retry(request_attempt, delay, "provider temporarily unavailable")
            time.sleep(delay)
            raise_if_cancelled()
        except ProviderResponseError:
            if response_retries >= 1:
                raise
            delay = 0.5
            response_retries += 1
            request_attempt += 1
            correction = _RESPONSE_CORRECTION
            if on_retry:
                on_retry(request_attempt, delay, "invalid provider response")
            time.sleep(delay)
            raise_if_cancelled()


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
    # QA/渲染失败由 _qa_and_render_translation 记账，勿回写 TRANSLATE=failed。
    return _qa_and_render_translation(job, translated, event_sink)


def complete_translated_outputs(
    job_file: str | Path,
    *,
    event_sink: EventSink | None = None,
) -> tuple[Path, ...]:
    """从已有 ``translated_cues`` 补跑译文 QA 与渲染，不调用翻译 API、不重写 cue。

    用于 ``needs_review`` 确认后的 continue：cues 已落盘但 SRT/ASS 尚未写出的断点。
    """
    job = load_job(job_file)
    translated = _load_cues(_artifact_path(job, "translated_cues"))
    return _qa_and_render_translation(job, translated, event_sink)


def _qa_and_render_translation(
    job: Job,
    translated: list[Cue],
    event_sink: EventSink | None,
) -> tuple[Path, ...]:
    """译文 QA → 渲染；失败时持久化当前 stage。"""
    current_stage = StageName.SUBTITLE_QA
    try:
        artifacts_directory = get_job_directory(job) / "artifacts"
        _set_stage(
            job,
            current_stage,
            StageStatus.RUNNING,
            event_sink,
            extras={"detail": "translated"},
        )
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
        _set_stage(
            job,
            current_stage,
            StageStatus.RUNNING,
            event_sink,
            extras={"detail": "translated"},
        )
        paths = _render_translated_outputs(job, translated, media_data, event_sink)
        _set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)
        return paths
    except Exception as error:
        if job.stages[current_stage].status is not StageStatus.FAILED:
            _set_stage(
                job,
                current_stage,
                StageStatus.FAILED,
                event_sink,
                error=str(error),
                error_category=_error_category(error),
            )
        raise


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
    await_input: bool = True,
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
                "prompt_version": TRANSLATION_PROMPT_VERSION,
                "semantic_grouping_version": SEMANTIC_GROUPING_VERSION,
                "policy": resolved_policy.to_dict(),
                "batches": [batch.to_dict() for batch in batches],
            },
        )
        _add_artifact(job, "translation_batches", output_path, event_sink)
        job.config["target_language"] = target_language
        job.config["translation_policy"] = resolved_policy.to_dict()
        if await_input:
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
    max_retries: int = DEFAULT_MAX_RETRIES,
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
    # API 路径准备批次时不得进入 awaiting_input，避免人类可读进度误报「等待」。
    batches_path = prepare_agent_translation(
        job_file,
        source_language=source_language,
        target_language=target_language,
        batch_size=batch_size,
        policy=resolved_policy,
        await_input=(mode == "agent"),
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
        "semantic_grouping_version": SEMANTIC_GROUPING_VERSION,
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

            def emit_retry(attempt: int, delay: float, reason: str) -> None:
                if event_sink:
                    event_sink(
                        create_event(
                            EventType.TRANSLATION_BATCH_RETRYING,
                            **job_event_fields(job.id, get_job_file(job)),
                            batch=batch.id,
                            batch_total=batch_total,
                            attempt=attempt,
                            delay_seconds=delay,
                            reason=reason,
                        )
                    )

            translated_batch = _translate_batch_with_retries(
                batch,
                provider_config,
                max_retries=max_retries,
                on_retry=emit_retry,
            )
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
        if subtitle_path.resolve() != imported_path.resolve():
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

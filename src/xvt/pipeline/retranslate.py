"""指定 cue 的局部重译：只回填选中 ID，保留其余译文与全部时间轴。

每个目标 cue 携带相邻源 cue 作为只读上下文；回填后经 `_finalize_translation`
完整重跑译文 QA，并使旧渲染、烧录与预览产物失效。
"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

from xvt.events import EventSink
from xvt.job_store import get_job_directory, load_job
from xvt.models import StageName, StageStatus
from xvt.pipeline.common import (
    _add_artifact,
    _artifact_path,
    _error_category,
    _invalidate_rendered_artifacts,
    _load_cues,
    _set_stage,
    _write_json,
)
from xvt.pipeline.translation import (
    TranslationRunResult,
    _finalize_translation,
    _load_translation_response,
)
from xvt.process import raise_if_cancelled
from xvt.translate import (
    build_cue_retranslation_batches,
    translation_policy_from_dict,
)
from xvt.translation_provider import (
    ProviderResponseError,
    TranslationProviderError,
    resolve_provider,
    translate_batch,
)


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

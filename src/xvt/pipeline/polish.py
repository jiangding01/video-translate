"""可恢复的译文润色：草稿快照、API checkpoint、Agent 回填与统一收尾。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path

from xvt.artifacts import ArtifactKind, downstream_of
from xvt.events import EventSink, EventType, create_event, job_event_fields
from xvt.job_store import get_job_directory, get_job_file, load_job, save_job
from xvt.models import Cue, Job, StageName, StageStatus
from xvt.pipeline.common import (
    _add_artifact,
    _checksum,
    _error_category,
    _invalidate_rendered_artifacts,
    _load_cues,
    _set_stage,
    _write_json,
)
from xvt.pipeline.translation import (
    TranslationRunResult,
    _finalize_translation,
    _translate_batch_with_retries,
)
from xvt.process import raise_if_cancelled
from xvt.recovery import valid_artifact_path
from xvt.translate import (
    SEMANTIC_GROUPING_VERSION,
    PolishBatch,
    apply_polish_translations,
    build_polish_batches,
    translation_policy_from_dict,
)
from xvt.translation_provider import (
    TRANSLATION_POLISH_PROMPT_VERSION,
    polish_batch,
    resolve_provider,
)
from xvt.workflow_config import (
    WorkflowConfig,
    clear_burn_fingerprint,
    require_workflow_config,
    set_workflow_config,
)


def _source_language(job: Job, cues: list[Cue]) -> str:
    return next(
        (cue.source_language for cue in cues if cue.source_language),
        job.config.get("source_language", "auto"),
    )


def _reset_for_polish(job: Job) -> None:
    invalidated = downstream_of(ArtifactKind.TRANSLATION_DRAFT_CUES)
    job.artifacts = [artifact for artifact in job.artifacts if artifact.kind not in invalidated]
    clear_burn_fingerprint(job)
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


def _polished_config(
    config: WorkflowConfig,
    *,
    provider: str | None,
    base_url: str | None,
    model: str | None,
    api_key_env: str | None,
    batch_size: int | None,
    max_retries: int | None,
    timeout_seconds: float | None,
) -> WorkflowConfig:
    resolved = replace(
        config,
        translation_quality="polished",
        provider=provider if provider is not None else config.provider,
        base_url=base_url if base_url is not None else config.base_url,
        model=model if model is not None else config.model,
        api_key_env=api_key_env if api_key_env is not None else config.api_key_env,
        batch_size=batch_size if batch_size is not None else config.batch_size,
        max_retries=max_retries if max_retries is not None else config.max_retries,
        timeout_seconds=(
            timeout_seconds if timeout_seconds is not None else config.timeout_seconds
        ),
    )
    return WorkflowConfig.from_dict(resolved.to_dict())


def _prepare_draft(
    job: Job,
    *,
    provider: str | None,
    base_url: str | None,
    model: str | None,
    api_key_env: str | None,
    batch_size: int | None,
    max_retries: int | None,
    timeout_seconds: float | None,
    event_sink: EventSink | None,
) -> Path:
    config = require_workflow_config(job)
    polished_config = _polished_config(
        config,
        provider=provider,
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        batch_size=batch_size,
        max_retries=max_retries,
        timeout_seconds=timeout_seconds,
    )
    existing = valid_artifact_path(job, "translation_draft_cues")
    if config.translation_quality == "polished":
        if existing is None:
            raise ValueError("polished workflow has no valid translation draft checkpoint")
        identity_changed = (
            config.provider,
            config.base_url,
            config.model,
            config.batch_size,
            config.translation_policy,
        ) != (
            polished_config.provider,
            polished_config.base_url,
            polished_config.model,
            polished_config.batch_size,
            polished_config.translation_policy,
        )
        if polished_config != config:
            set_workflow_config(job, polished_config)
            if identity_changed:
                _reset_for_polish(job)
            save_job(job)
        return existing

    translated_path = valid_artifact_path(job, "translated_cues")
    if translated_path is None:
        raise ValueError("translation polishing requires valid translated_cues")
    draft_path = get_job_directory(job) / "artifacts" / "translation-draft-cues.json"
    draft_path.write_bytes(translated_path.read_bytes())
    _add_artifact(job, "translation_draft_cues", draft_path, event_sink)
    set_workflow_config(job, polished_config)
    _reset_for_polish(job)
    save_job(job)
    return draft_path


def _build_batches(job: Job, draft_path: Path) -> list[PolishBatch]:
    config = require_workflow_config(job)
    if config.target_language is None:
        raise ValueError("translation polishing requires a target language")
    source_path = valid_artifact_path(job, "source_cues")
    if source_path is None:
        raise ValueError("translation polishing requires checksum-valid source_cues")
    source = _load_cues(source_path)
    draft = _load_cues(draft_path)
    policy = config.translation_policy or translation_policy_from_dict(
        job.config.get("translation_policy")
    )
    return build_polish_batches(
        source,
        draft,
        source_language=_source_language(job, source),
        target_language=config.target_language,
        batch_size=config.batch_size,
        policy=policy,
    )


def _fingerprint(
    job: Job,
    *,
    mode: str,
    base_url: str | None,
    model: str | None,
) -> str:
    config = require_workflow_config(job)
    source_path = valid_artifact_path(job, "source_cues")
    draft_path = valid_artifact_path(job, "translation_draft_cues")
    if source_path is None or draft_path is None:
        raise ValueError("translation polish fingerprint requires valid source and draft cues")
    policy = config.translation_policy or translation_policy_from_dict(
        job.config.get("translation_policy")
    )
    source_language = _source_language(job, _load_cues(source_path))
    payload = {
        "source_cues_checksum": _checksum(source_path),
        "draft_cues_checksum": _checksum(draft_path),
        "source_language": source_language,
        "target_language": config.target_language,
        "provider": mode,
        "base_url": base_url,
        "model": model,
        "batch_size": config.batch_size,
        "prompt_version": TRANSLATION_POLISH_PROMPT_VERSION,
        "semantic_grouping_version": SEMANTIC_GROUPING_VERSION,
        "policy": policy.to_dict(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _write_batches(
    job: Job,
    batches: list[PolishBatch],
    fingerprint: str,
    event_sink: EventSink | None,
) -> Path:
    path = get_job_directory(job) / "artifacts" / "translation-polish-batches.json"
    _write_json(
        path,
        {
            "job_id": job.id,
            "fingerprint": fingerprint,
            "prompt_version": TRANSLATION_POLISH_PROMPT_VERSION,
            "semantic_grouping_version": SEMANTIC_GROUPING_VERSION,
            "batches": [batch.to_dict() for batch in batches],
        },
    )
    _add_artifact(job, "translation_polish_batches", path, event_sink)
    return path


def _load_progress(
    job: Job,
    batches: list[PolishBatch],
    fingerprint: str,
    event_sink: EventSink | None,
) -> tuple[set[int], dict[int, str]]:
    path = valid_artifact_path(job, "translation_polish_progress")
    if path is None:
        return set(), {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        completed_data = data["completed_batches"]
        items_data = data["items"]
        if not isinstance(completed_data, list) or not isinstance(items_data, list):
            raise ValueError
        if any(not isinstance(value, int) or isinstance(value, bool) for value in completed_data):
            raise ValueError
        completed = set(completed_data)
        if len(completed) != len(completed_data):
            raise ValueError
        translations: dict[int, str] = {}
        for item in items_data:
            if not isinstance(item, dict):
                raise ValueError
            cue_id = item.get("id")
            text = item.get("text")
            if (
                not isinstance(cue_id, int)
                or isinstance(cue_id, bool)
                or cue_id in translations
                or not isinstance(text, str)
                or not text.strip()
            ):
                raise ValueError
            translations[cue_id] = text.strip()
        batch_ids = {batch.id for batch in batches}
        expected_item_ids = {
            item.id for batch in batches if batch.id in completed for item in batch.items
        }
        if (
            data.get("fingerprint") != fingerprint
            or not completed <= batch_ids
            or set(translations) != expected_item_ids
        ):
            raise ValueError
        return completed, translations
    except (KeyError, TypeError, ValueError):
        if event_sink:
            event_sink(
                create_event(
                    EventType.TRANSLATION_PROGRESS_RESET,
                    **job_event_fields(job.id, get_job_file(job)),
                    phase="polish",
                    reason="polish checkpoint is invalid or its fingerprint changed",
                )
            )
        return set(), {}


def translation_polish_satisfied(job: Job) -> bool:
    draft_path = valid_artifact_path(job, "translation_draft_cues")
    batches_path = valid_artifact_path(job, "translation_polish_batches")
    progress_path = valid_artifact_path(job, "translation_polish_progress")
    polished_path = valid_artifact_path(job, "translation_polished_cues")
    translated_path = valid_artifact_path(job, "translated_cues")
    if not all((draft_path, batches_path, progress_path, polished_path, translated_path)):
        return False
    try:
        batches_data = json.loads(batches_path.read_text(encoding="utf-8"))
        progress_data = json.loads(progress_path.read_text(encoding="utf-8"))
        if not isinstance(batches_data, dict) or not isinstance(progress_data, dict):
            return False
        fingerprint = batches_data.get("fingerprint")
        if (
            not isinstance(fingerprint, str)
            or not fingerprint
            or fingerprint != progress_data.get("fingerprint")
            or batches_data.get("prompt_version") != TRANSLATION_POLISH_PROMPT_VERSION
            or batches_data.get("semantic_grouping_version") != SEMANTIC_GROUPING_VERSION
        ):
            return False
        batches = _build_batches(job, draft_path)
        expected_batches = json.loads(
            json.dumps([batch.to_dict() for batch in batches], ensure_ascii=False)
        )
        completed = progress_data.get("completed_batches")
        items = progress_data.get("items")
        if (
            not isinstance(completed, list)
            or any(not isinstance(value, int) or isinstance(value, bool) for value in completed)
            or completed != [batch.id for batch in batches]
            or batches_data.get("batches") != expected_batches
            or not isinstance(items, list)
        ):
            return False
        translations: dict[int, str] = {}
        for item in items:
            if not isinstance(item, dict):
                return False
            cue_id = item.get("id")
            text = item.get("text")
            if (
                not isinstance(cue_id, int)
                or isinstance(cue_id, bool)
                or cue_id in translations
                or not isinstance(text, str)
                or not text.strip()
            ):
                return False
            translations[cue_id] = text.strip()
        expected = apply_polish_translations(_load_cues(draft_path), translations)
        polished = _load_cues(polished_path)
        translated = _load_cues(translated_path)
        return polished == expected and translated == expected
    except (KeyError, OSError, TypeError, ValueError):
        return False


def _write_progress(
    job: Job,
    path: Path,
    fingerprint: str,
    completed: set[int],
    translations: dict[int, str],
    event_sink: EventSink | None,
) -> None:
    _write_json(
        path,
        {
            "fingerprint": fingerprint,
            "completed_batches": sorted(completed),
            "items": [
                {"id": cue_id, "text": text} for cue_id, text in sorted(translations.items())
            ],
        },
    )
    _add_artifact(job, "translation_polish_progress", path, event_sink)


def _finalize_polish(
    job: Job,
    draft: list[Cue],
    translations: dict[int, str],
    event_sink: EventSink | None,
) -> tuple[Path, ...]:
    polished = apply_polish_translations(draft, translations)
    path = get_job_directory(job) / "artifacts" / "translation-polished-cues.json"
    _write_json(path, [asdict(cue) for cue in polished])
    _add_artifact(job, "translation_polished_cues", path, event_sink)
    return _finalize_translation(
        job,
        polished,
        event_sink,
        invalidate=_invalidate_rendered_artifacts,
        polished_result=True,
    )


def run_translation_polish(
    job_file: str | Path,
    *,
    provider: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    api_key_env: str | None = None,
    batch_size: int | None = None,
    max_retries: int | None = None,
    timeout_seconds: float | None = None,
    event_sink: EventSink | None = None,
) -> TranslationRunResult:
    job = load_job(job_file)
    _polished_config(
        require_workflow_config(job),
        provider=provider,
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        batch_size=batch_size,
        max_retries=max_retries,
        timeout_seconds=timeout_seconds,
    )
    draft_path = _prepare_draft(
        job,
        provider=provider,
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        batch_size=batch_size,
        max_retries=max_retries,
        timeout_seconds=timeout_seconds,
        event_sink=event_sink,
    )
    job = load_job(job_file)
    if translation_polish_satisfied(job):
        paths = tuple(
            path
            for kind in ("translated_srt", "bilingual_srt", "bilingual_ass")
            if (path := valid_artifact_path(job, kind)) is not None
        )
        return TranslationRunResult("reused", paths)

    config = require_workflow_config(job)
    if config.max_retries < 0 or config.timeout_seconds <= 0:
        raise ValueError("invalid persisted translation provider retry configuration")
    batches = _build_batches(job, draft_path)
    mode, provider_config, fallback_reason = resolve_provider(
        config.provider,
        base_url=config.base_url,
        model=config.model,
        api_key_env=config.api_key_env,
        timeout_seconds=config.timeout_seconds,
    )
    fingerprint = _fingerprint(
        job,
        mode=mode,
        base_url=provider_config.base_url if provider_config else config.base_url,
        model=provider_config.model if provider_config else config.model,
    )
    batches_path = _write_batches(job, batches, fingerprint, event_sink)
    _set_stage(
        job,
        StageName.TRANSLATE,
        StageStatus.RUNNING,
        event_sink,
        extras={"phase": "polish"},
    )
    if mode == "agent":
        _set_stage(
            job,
            StageName.TRANSLATE,
            StageStatus.AWAITING_INPUT,
            event_sink,
            extras={"phase": "polish"},
        )
        if fallback_reason and event_sink:
            event_sink(
                create_event(
                    EventType.TRANSLATION_FALLBACK,
                    **job_event_fields(job.id, get_job_file(job)),
                    phase="polish",
                    mode="agent",
                    reason=fallback_reason,
                    path=str(batches_path),
                )
            )
        return TranslationRunResult("agent", (batches_path,), fallback_reason)
    if provider_config is None:
        raise RuntimeError("translation polish provider configuration was not resolved")

    if event_sink:
        event_sink(
            create_event(
                EventType.TRANSLATION_PROVIDER_SELECTED,
                **job_event_fields(job.id, get_job_file(job)),
                phase="polish",
                provider=mode,
                model=provider_config.model,
            )
        )
    completed, translations = _load_progress(job, batches, fingerprint, event_sink)
    progress_path = get_job_directory(job) / "artifacts" / "translation-polish-progress.json"
    try:
        for batch in batches:
            raise_if_cancelled()
            if batch.id in completed:
                continue
            if event_sink:
                event_sink(
                    create_event(
                        EventType.TRANSLATION_BATCH_STARTED,
                        **job_event_fields(job.id, get_job_file(job)),
                        phase="polish",
                        batch=batch.id,
                        batch_total=len(batches),
                    )
                )

            def emit_retry(attempt: int, delay: float, reason: str) -> None:
                if event_sink:
                    event_sink(
                        create_event(
                            EventType.TRANSLATION_BATCH_RETRYING,
                            **job_event_fields(job.id, get_job_file(job)),
                            phase="polish",
                            batch=batch.id,
                            batch_total=len(batches),
                            attempt=attempt,
                            delay_seconds=delay,
                            reason=reason,
                        )
                    )

            translations.update(
                _translate_batch_with_retries(
                    batch,
                    provider_config,
                    max_retries=config.max_retries,
                    on_retry=emit_retry,
                    request=polish_batch,
                )
            )
            completed.add(batch.id)
            _write_progress(job, progress_path, fingerprint, completed, translations, event_sink)
            if event_sink:
                event_sink(
                    create_event(
                        EventType.TRANSLATION_BATCH_COMPLETED,
                        **job_event_fields(job.id, get_job_file(job)),
                        phase="polish",
                        batch=batch.id,
                        batch_total=len(batches),
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
            extras={"phase": "polish"},
        )
        raise
    rendered = _finalize_polish(job, _load_cues(draft_path), translations, event_sink)
    return TranslationRunResult(mode, (progress_path, *rendered))


def apply_agent_translation_polish(
    job_file: str | Path,
    response_file: str | Path,
    *,
    event_sink: EventSink | None = None,
) -> tuple[Path, ...]:
    job = load_job(job_file)
    config = require_workflow_config(job)
    if config.translation_quality != "polished":
        raise ValueError("job does not request polished translation quality")
    draft_path = valid_artifact_path(job, "translation_draft_cues")
    batches_path = valid_artifact_path(job, "translation_polish_batches")
    if draft_path is None or batches_path is None:
        raise ValueError("job has no valid Agent polish checkpoint")
    batches = _build_batches(job, draft_path)
    manifest = json.loads(batches_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("polish batch manifest must contain a JSON object")
    fingerprint = manifest.get("fingerprint")
    if not isinstance(fingerprint, str):
        raise ValueError("polish batch manifest has no valid fingerprint")
    expected_fingerprint = _fingerprint(
        job,
        mode="agent",
        base_url=config.base_url,
        model=config.model,
    )
    if fingerprint != expected_fingerprint:
        raise ValueError("polish batch manifest fingerprint no longer matches the draft")
    expected_ids = {item.id for batch in batches for item in batch.items}
    response = json.loads(Path(response_file).read_text(encoding="utf-8"))
    if not isinstance(response, dict) or response.get("fingerprint") != fingerprint:
        raise ValueError("polish response fingerprint does not match the current draft")
    items = response.get("items") if isinstance(response, dict) else None
    if not isinstance(items, list):
        raise ValueError("polish response must contain an items array")
    translations: dict[int, str] = {}
    for item in items:
        cue_id = item.get("id") if isinstance(item, dict) else None
        text = item.get("text") if isinstance(item, dict) else None
        if not isinstance(cue_id, int) or isinstance(cue_id, bool) or cue_id in translations:
            raise ValueError("polish response contains an invalid or duplicate cue id")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"polished translation for cue {cue_id} is empty")
        translations[cue_id] = text.strip()
    if set(translations) != expected_ids:
        raise ValueError("polish response must cover every requested cue id exactly once")

    _set_stage(
        job,
        StageName.TRANSLATE,
        StageStatus.RUNNING,
        event_sink,
        extras={"phase": "polish"},
    )
    progress_path = get_job_directory(job) / "artifacts" / "translation-polish-progress.json"
    _write_progress(
        job,
        progress_path,
        fingerprint,
        {batch.id for batch in batches},
        translations,
        event_sink,
    )
    return _finalize_polish(job, _load_cues(draft_path), translations, event_sink)

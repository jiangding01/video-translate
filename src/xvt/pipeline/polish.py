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
    add_artifact,
    checksum,
    emit_agent_fallback,
    load_cues,
    load_user_response_json,
    parse_cue_text_items,
    set_stage,
    write_json,
)
from xvt.pipeline.translation import (
    TranslationRunResult,
    agent_translate_and_polish_started,
    run_provider_batches,
)
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
    get_workflow_config,
    require_workflow_config,
    set_workflow_config,
)

# Agent 的 `translate_and_polish` 一次成型：初译与润色在同一次回填里完成，Job 里
# 从来不会出现 `translation-draft-cues.json`。这类 Job 缺 draft 不是「checkpoint 失效、
# 需要恢复草稿」，而是「补跑 Q5a 润色这条路对它结构上不适用」——两种情形必须给出不同
# 的报错，否则用户照着「恢复草稿」的提示折腾一个根本没有草稿的 Job。
AGENT_ONE_SHOT_POLISH_RERUN_MESSAGE = (
    "this job uses the Agent translate_and_polish manifest, which produces the polished "
    "translation inside the translate step and never writes translation-draft-cues.json; "
    "polish-translation does not apply to it — run xvt status JOB to see the remaining steps"
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
            # 复用 planner / recovery 已有的那条「Agent 一次成型」判定，不另造谓词。
            if agent_translate_and_polish_started(job):
                raise ValueError(AGENT_ONE_SHOT_POLISH_RERUN_MESSAGE)
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
    add_artifact(job, "translation_draft_cues", draft_path, event_sink)
    set_workflow_config(job, polished_config)
    _reset_for_polish(job)
    save_job(job)
    return draft_path


def _build_batches(
    job: Job, draft_path: Path, config: WorkflowConfig | None = None
) -> list[PolishBatch]:
    config = config or require_workflow_config(job)
    if config.target_language is None:
        raise ValueError("translation polishing requires a target language")
    source_path = valid_artifact_path(job, "source_cues")
    if source_path is None:
        raise ValueError("translation polishing requires checksum-valid source_cues")
    source = load_cues(source_path)
    draft = load_cues(draft_path)
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
    config: WorkflowConfig | None = None,
    mode: str,
    base_url: str | None,
    model: str | None,
) -> str:
    config = config or require_workflow_config(job)
    source_path = valid_artifact_path(job, "source_cues")
    draft_path = valid_artifact_path(job, "translation_draft_cues")
    if source_path is None or draft_path is None:
        raise ValueError("translation polish fingerprint requires valid source and draft cues")
    policy = config.translation_policy or translation_policy_from_dict(
        job.config.get("translation_policy")
    )
    source_language = _source_language(job, load_cues(source_path))
    payload = {
        "source_cues_checksum": checksum(source_path),
        "draft_cues_checksum": checksum(draft_path),
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
    write_json(
        path,
        {
            "job_id": job.id,
            "fingerprint": fingerprint,
            "prompt_version": TRANSLATION_POLISH_PROMPT_VERSION,
            "semantic_grouping_version": SEMANTIC_GROUPING_VERSION,
            "batches": [batch.to_dict() for batch in batches],
        },
    )
    add_artifact(job, "translation_polish_batches", path, event_sink)
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
        if not isinstance(completed_data, list):
            raise ValueError
        if any(not isinstance(value, int) or isinstance(value, bool) for value in completed_data):
            raise ValueError
        completed = set(completed_data)
        if len(completed) != len(completed_data):
            raise ValueError
        translations = parse_cue_text_items(items_data)
        if translations is None:
            raise ValueError
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


def translation_polish_satisfied(
    job: Job,
    config: WorkflowConfig | None = None,
    expected_fingerprint: str | None = None,
) -> bool:
    draft_path = valid_artifact_path(job, "translation_draft_cues")
    batches_path = valid_artifact_path(job, "translation_polish_batches")
    progress_path = valid_artifact_path(job, "translation_polish_progress")
    polished_path = valid_artifact_path(job, "translation_polished_cues")
    if not all((draft_path, batches_path, progress_path, polished_path)):
        return False
    try:
        config = config or require_workflow_config(job)
        if config.target_language is None:
            return False
        batches_data = json.loads(batches_path.read_text(encoding="utf-8"))
        progress_data = json.loads(progress_path.read_text(encoding="utf-8"))
        if not isinstance(batches_data, dict) or not isinstance(progress_data, dict):
            return False
        fingerprint = batches_data.get("fingerprint")
        if (
            not isinstance(fingerprint, str)
            or not fingerprint
            or fingerprint != progress_data.get("fingerprint")
            or (expected_fingerprint is not None and fingerprint != expected_fingerprint)
            or batches_data.get("prompt_version") != TRANSLATION_POLISH_PROMPT_VERSION
            or batches_data.get("semantic_grouping_version") != SEMANTIC_GROUPING_VERSION
        ):
            return False
        batches = _build_batches(job, draft_path, config)
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
        ):
            return False
        translations = parse_cue_text_items(items)
        if translations is None:
            return False
        expected = apply_polish_translations(
            load_cues(draft_path),
            translations,
            target_language=config.target_language,
        )
        polished = load_cues(polished_path)
        return polished == expected
    except (KeyError, OSError, TypeError, ValueError):
        return False


def agent_translation_polish_manifest_valid(
    job: Job,
    config: WorkflowConfig | None = None,
) -> bool:
    config = config or get_workflow_config(job)
    draft_path = valid_artifact_path(job, "translation_draft_cues")
    batches_path = valid_artifact_path(job, "translation_polish_batches")
    if config is None or draft_path is None or batches_path is None:
        return False
    try:
        manifest = json.loads(batches_path.read_text(encoding="utf-8"))
        expected_fingerprint = _fingerprint(
            job,
            config=config,
            mode="agent",
            base_url=config.base_url,
            model=config.model,
        )
        expected_batches = json.loads(
            json.dumps(
                [batch.to_dict() for batch in _build_batches(job, draft_path, config)],
                ensure_ascii=False,
            )
        )
        return (
            isinstance(manifest, dict)
            and manifest.get("job_id") == job.id
            and manifest.get("fingerprint") == expected_fingerprint
            and manifest.get("prompt_version") == TRANSLATION_POLISH_PROMPT_VERSION
            and manifest.get("semantic_grouping_version") == SEMANTIC_GROUPING_VERSION
            and manifest.get("batches") == expected_batches
        )
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
    write_json(
        path,
        {
            "fingerprint": fingerprint,
            "completed_batches": sorted(completed),
            "items": [
                {"id": cue_id, "text": text} for cue_id, text in sorted(translations.items())
            ],
        },
    )
    add_artifact(job, "translation_polish_progress", path, event_sink)


def _finalize_polish(
    job: Job,
    draft: list[Cue],
    translations: dict[int, str],
    *,
    target_language: str,
    event_sink: EventSink | None,
) -> tuple[Path, ...]:
    polished = apply_polish_translations(
        draft,
        translations,
        target_language=target_language,
    )
    invalidated = downstream_of(ArtifactKind.TRANSLATION_POLISHED_CUES)
    job.artifacts = [artifact for artifact in job.artifacts if artifact.kind not in invalidated]
    clear_burn_fingerprint(job)
    for stage in (StageName.SUBTITLE_QA, StageName.RENDER, StageName.BURN, StageName.MEDIA_QA):
        record = job.stages[stage]
        record.status = StageStatus.PENDING
        record.started_at = None
        record.completed_at = None
        record.error = None
    path = get_job_directory(job) / "artifacts" / "translation-polished-cues.json"
    write_json(path, [asdict(cue) for cue in polished])
    add_artifact(job, "translation_polished_cues", path, event_sink)
    set_stage(
        job,
        StageName.TRANSLATE,
        StageStatus.SUCCEEDED,
        event_sink,
        extras={"phase": "polish"},
    )
    return (path,)


def _run_translation_polish(
    job_file: str | Path,
    config: WorkflowConfig,
    draft_path: Path,
    event_sink: EventSink | None,
) -> TranslationRunResult:
    job = load_job(job_file)
    if config.max_retries < 0 or config.timeout_seconds <= 0:
        raise ValueError("invalid persisted translation provider retry configuration")
    if config.target_language is None:
        raise ValueError("translation polish requires a persisted target language")
    batches = _build_batches(job, draft_path, config)
    mode, provider_config, fallback_reason = resolve_provider(
        config.provider,
        base_url=config.base_url,
        model=config.model,
        api_key_env=config.api_key_env,
        timeout_seconds=config.timeout_seconds,
    )
    fingerprint = _fingerprint(
        job,
        config=config,
        mode=mode,
        base_url=provider_config.base_url if provider_config else config.base_url,
        model=provider_config.model if provider_config else config.model,
    )
    if translation_polish_satisfied(job, config, fingerprint):
        paths = tuple(
            path
            for kind in ("translated_srt", "bilingual_srt", "bilingual_ass")
            if (path := valid_artifact_path(job, kind)) is not None
        )
        return TranslationRunResult("reused", paths)
    batches_path = _write_batches(job, batches, fingerprint, event_sink)
    set_stage(
        job,
        StageName.TRANSLATE,
        StageStatus.RUNNING,
        event_sink,
        extras={"phase": "polish"},
    )
    if mode == "agent":
        set_stage(
            job,
            StageName.TRANSLATE,
            StageStatus.AWAITING_INPUT,
            event_sink,
            extras={"phase": "polish"},
        )
        if fallback_reason and event_sink:
            # 润色侧的降级事件带 phase="polish"；AWAITING_INPUT 已在上面单独设过，
            # helper 只发事件。文案也刻意不走 _fallback_reason_with_persisted_provider，
            # 与协议快照 polish_agent_rerun 场景冻住的那段不对称一致。
            emit_agent_fallback(
                job,
                manifest_path=batches_path,
                reason=fallback_reason,
                event_sink=event_sink,
                phase="polish",
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

    def record_batch(batch: PolishBatch, polished_batch: dict[int, str]) -> None:
        translations.update(polished_batch)
        completed.add(batch.id)
        _write_progress(job, progress_path, fingerprint, completed, translations, event_sink)

    # 润色相对初译只有四处差异，全部由 run_provider_batches 的参数承接：
    # request=polish_batch（显式传，让 `xvt.pipeline.polish.polish_batch` 这个桩位继续有效）、
    # 批次事件带 phase="polish"、失败收口的 extras 也带 phase="polish"（stage_extras），
    # 以及 checkpoint 跳过只看批次 ID —— 覆盖范围由 _load_progress 读 checkpoint 时已校验，
    # 这里无须再比对 cue 集合。
    run_provider_batches(
        job,
        batches,
        provider_config=provider_config,
        max_retries=config.max_retries,
        event_sink=event_sink,
        request=polish_batch,
        phase="polish",
        stage_extras={"phase": "polish"},
        skip=lambda batch: batch.id in completed,
        on_result=record_batch,
    )
    rendered = _finalize_polish(
        job,
        load_cues(draft_path),
        translations,
        target_language=config.target_language,
        event_sink=event_sink,
    )
    return TranslationRunResult(mode, (progress_path, *rendered))


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
    return _run_translation_polish(
        job_file,
        require_workflow_config(load_job(job_file)),
        draft_path,
        event_sink,
    )


def run_atomic_translation_polish(
    job_file: str | Path,
    *,
    target_language: str,
    provider: str,
    base_url: str | None,
    model: str | None,
    api_key_env: str,
    batch_size: int,
    max_retries: int,
    timeout_seconds: float,
    event_sink: EventSink | None = None,
) -> TranslationRunResult:
    job = load_job(job_file)
    if get_workflow_config(job) is not None:
        raise ValueError("atomic polish execution requires a job without a workflow goal")
    if job.config.get("translation_quality") != "polished":
        raise ValueError("atomic job does not request polished translation quality")
    if job.config.get("target_language") != target_language:
        raise ValueError("target language must match the persisted atomic job goal")
    draft_path = valid_artifact_path(job, "translation_draft_cues")
    if draft_path is None:
        raise ValueError("atomic polished translation has no valid draft checkpoint")
    config = WorkflowConfig(
        target_language=target_language,
        provider=provider,
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        batch_size=batch_size,
        max_retries=max_retries,
        timeout_seconds=timeout_seconds,
        translation_policy=translation_policy_from_dict(job.config.get("translation_policy")),
        translation_quality="polished",
    )
    return _run_translation_polish(job_file, config, draft_path, event_sink)


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
    if config.target_language is None:
        raise ValueError("translation polish requires a persisted target language")
    draft_path = valid_artifact_path(job, "translation_draft_cues")
    batches_path = valid_artifact_path(job, "translation_polish_batches")
    if draft_path is None or batches_path is None:
        raise ValueError("job has no valid Agent polish checkpoint")
    if not agent_translation_polish_manifest_valid(job, config):
        raise ValueError("polish batch manifest no longer matches the draft")
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
    response = load_user_response_json(response_file, what="polish response")
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

    set_stage(
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
    return _finalize_polish(
        job,
        load_cues(draft_path),
        translations,
        target_language=config.target_language,
        event_sink=event_sink,
    )

"""可恢复的译文语义校对、一次定向修复和人工确认前置状态。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from xvt.artifacts import ArtifactKind, downstream_of
from xvt.candidate_qa import (
    TRANSLATION_CANDIDATE_QA_VERSION,
    translation_candidate_qa_issues,
)
from xvt.events import EventSink
from xvt.job_store import get_job_directory, get_job_file, load_job, save_job
from xvt.models import Job, StageName, StageStatus
from xvt.pipeline.common import (
    add_artifact,
    checksum,
    error_category,
    invalidate_rendered_artifacts,
    load_cues,
    load_user_response_json,
    set_stage,
    translation_qa_context,
    write_json,
)
from xvt.pipeline.review import translation_review_acknowledged
from xvt.pipeline.revision import ensure_semantic_review_inputs
from xvt.pipeline.semantic_state import (
    agent_results,
    build_review_batches,
    canonical_issue_key,
    config_for_job,
    current_semantic_candidate,
    human_revision_scope,
    load_repair_checkpoint,
    report_status,
    resolved_policy,
    semantic_review_target,
    serialized_batches,
    serialized_issues,
    source_language,
    validate_unique_issues,
)
from xvt.pipeline.translation import (
    complete_translated_outputs,
    finalize_translation,
    run_provider_batches,
)
from xvt.qa import check_subtitles
from xvt.recovery import valid_artifact_path
from xvt.translate import (
    SEMANTIC_GROUPING_VERSION,
    SEMANTIC_REPAIR_CATEGORIES,
    SemanticIssue,
    SemanticRepairBatch,
    SemanticReviewBatch,
    apply_semantic_repairs,
    build_semantic_repair_batches,
    build_semantic_review_batches,
    parse_semantic_repair_response,
    parse_semantic_review_response,
)
from xvt.translation_provider import (
    SEMANTIC_REPAIR_PROMPT_VERSION,
    SEMANTIC_REVIEW_PROMPT_VERSION,
    resolve_provider,
    semantic_repair_batch,
    semantic_review_batch,
)
from xvt.workflow_config import WorkflowConfig, clear_burn_fingerprint

SEMANTIC_AGENT_TASK_VERSION = 1
_SEMANTIC_REVIEW_RESPONSE_CORRECTION = (
    'Return valid JSON only in the form {"issues":[]}; every issue must follow the requested '
    "category, cue_ids, severity, message, and suggested_items contract."
)


@dataclass(frozen=True, slots=True)
class SemanticRunResult:
    status: str
    mode: str
    paths: tuple[Path, ...]
    fallback_reason: str | None = None


def _ensure_candidate_qa(
    job: Job,
    candidate_path: Path,
    event_sink: EventSink | None,
) -> tuple[Path, tuple[dict[str, Any], ...]]:
    # 字体安装与实际烧录媒体可能改变，不跨命令复用布局测量。
    report = check_subtitles(
        load_cues(candidate_path),
        **translation_qa_context(job),
        require_translation=True,
    )
    path = get_job_directory(job) / "artifacts" / "translation-candidate-qa.json"
    payload = {
        "task": "translation_candidate_qa",
        "version": TRANSLATION_CANDIDATE_QA_VERSION,
        "candidate_cues_checksum": checksum(candidate_path),
        **asdict(report),
    }
    existing = valid_artifact_path(job, "translation_candidate_qa")
    if existing is not None:
        # JSON 将 tuple 转为 list，比较同一序列化口径，避免无变化时改写 Job。
        if json.loads(existing.read_text(encoding="utf-8")) == json.loads(json.dumps(payload)):
            issues = translation_candidate_qa_issues(job, candidate_path)
            if issues is not None:
                return existing, issues
    write_json(
        path,
        payload,
    )
    add_artifact(job, "translation_candidate_qa", path, event_sink)
    issues = translation_candidate_qa_issues(job, candidate_path)
    if issues is None:
        raise ValueError("translation candidate QA artifact is invalid after writing")
    return path, issues


def _reviewed_report_filename(candidate_path: Path) -> str:
    return f"translation-semantic-qa-reviewed-{checksum(candidate_path)[:12]}.json"


def _fingerprint(
    job: Job,
    candidate_path: Path,
    config: WorkflowConfig,
    *,
    phase: str,
    mode: str,
    base_url: str | None,
    model: str | None,
    report_checksum: str | None = None,
    review_scope: dict[str, Any] | None = None,
) -> str:
    source_path = valid_artifact_path(job, "source_cues")
    if source_path is None:
        raise ValueError("semantic review fingerprint requires valid source cues")
    payload = {
        "source_cues_checksum": checksum(source_path),
        "candidate_cues_checksum": checksum(candidate_path),
        "target_language": config.target_language,
        "provider": mode,
        "base_url": base_url,
        "model": model,
        "batch_size": config.batch_size,
        "policy": resolved_policy(job, config).to_dict(),
        "phase": phase,
        "prompt_version": (
            SEMANTIC_REPAIR_PROMPT_VERSION if phase == "repair" else SEMANTIC_REVIEW_PROMPT_VERSION
        ),
        "semantic_grouping_version": SEMANTIC_GROUPING_VERSION,
        "report_checksum": report_checksum,
        "review_scope": review_scope,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _merge_issues(
    primary: tuple[SemanticIssue, ...],
    preserved: tuple[SemanticIssue, ...],
) -> tuple[SemanticIssue, ...]:
    merged = list(primary)
    seen = {canonical_issue_key(issue) for issue in primary}
    for issue in preserved:
        if canonical_issue_key(issue) not in seen:
            merged.append(issue)
            seen.add(canonical_issue_key(issue))
    return validate_unique_issues(merged)


def _invalidate_semantic_downstream(job: Job, root: ArtifactKind) -> None:
    invalidated = downstream_of(root)
    job.artifacts = [artifact for artifact in job.artifacts if artifact.kind not in invalidated]
    job.config.pop("translation_review_acknowledgment", None)
    clear_burn_fingerprint(job)
    for stage in (StageName.SUBTITLE_QA, StageName.RENDER, StageName.BURN, StageName.MEDIA_QA):
        record = job.stages[stage]
        record.status = StageStatus.PENDING
        record.started_at = None
        record.completed_at = None
        record.error = None
    save_job(job)


def _load_review_checkpoint(
    job: Job,
    kind: str,
    batches: list[SemanticReviewBatch],
    fingerprint: str,
) -> tuple[bool, set[int], dict[int, tuple[SemanticIssue, ...]]]:
    path = valid_artifact_path(job, kind)
    if path is None:
        return False, set(), {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("fingerprint") != fingerprint or data.get("batches") != serialized_batches(
            batches
        ):
            raise ValueError
        results = data.get("results")
        if not isinstance(results, list):
            raise ValueError
        batches_by_id = {batch.id: batch for batch in batches}
        parsed: dict[int, tuple[SemanticIssue, ...]] = {}
        for result in results:
            batch_id = result.get("batch") if isinstance(result, dict) else None
            issues = result.get("issues") if isinstance(result, dict) else None
            if batch_id not in batches_by_id or batch_id in parsed or not isinstance(issues, list):
                raise ValueError
            parsed[batch_id] = parse_semantic_review_response(batches_by_id[batch_id], issues)
        validate_unique_issues([issue for value in parsed.values() for issue in value])
        return True, set(parsed), parsed
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False, set(), {}


def _write_review_checkpoint(
    job: Job,
    *,
    kind: str,
    path: Path,
    task: str,
    batches: list[SemanticReviewBatch],
    fingerprint: str,
    results: dict[int, tuple[SemanticIssue, ...]],
    mode: str,
    event_sink: EventSink | None,
    preserved_issues: tuple[SemanticIssue, ...] = (),
    review_scope: dict[str, Any] | None = None,
) -> Path:
    complete = len(results) == len(batches)
    issues = _merge_issues(
        validate_unique_issues([issue for value in results.values() for issue in value]),
        preserved_issues,
    )
    write_json(
        path,
        {
            "task": task,
            "mode": mode,
            "fingerprint": fingerprint,
            "prompt_version": SEMANTIC_REVIEW_PROMPT_VERSION,
            "semantic_grouping_version": SEMANTIC_GROUPING_VERSION,
            "batches": serialized_batches(batches),
            "completed_batches": sorted(results),
            "results": [
                {
                    "batch": batch_id,
                    "issues": [issue.to_dict() for issue in results[batch_id]],
                }
                for batch_id in sorted(results)
            ],
            "issues": [issue.to_dict() for issue in issues],
            "status": (
                "pass" if complete and not issues else "needs_review" if complete else "running"
            ),
            "review_scope": review_scope,
        },
    )
    add_artifact(job, kind, path, event_sink)
    return path


def _run_review_phase(
    job: Job,
    config: WorkflowConfig,
    candidate_path: Path,
    *,
    kind: str,
    filename: str,
    task: str,
    phase: str,
    mode: str,
    provider_config,
    event_sink: EventSink | None,
    preserved_issues: tuple[SemanticIssue, ...] = (),
    review_cue_ids: frozenset[int] | None = None,
    review_scope: dict[str, Any] | None = None,
) -> tuple[Path, tuple[SemanticIssue, ...]]:
    batches = build_review_batches(
        job,
        candidate_path,
        config,
        review_cue_ids=review_cue_ids,
    )
    fingerprint = _fingerprint(
        job,
        candidate_path,
        config,
        phase=phase,
        mode=mode,
        base_url=provider_config.base_url,
        model=provider_config.model,
        review_scope=review_scope,
    )
    reusable, completed, results = _load_review_checkpoint(job, kind, batches, fingerprint)
    if not reusable and any(artifact.kind == kind for artifact in job.artifacts):
        _invalidate_semantic_downstream(job, ArtifactKind(kind))
    path = get_job_directory(job) / "artifacts" / filename

    def record_batch(batch: SemanticReviewBatch, issues: tuple[SemanticIssue, ...]) -> None:
        results[batch.id] = issues
        _write_review_checkpoint(
            job,
            kind=kind,
            path=path,
            task=task,
            batches=batches,
            fingerprint=fingerprint,
            results=results,
            mode=mode,
            event_sink=event_sink,
            preserved_issues=preserved_issues,
            review_scope=review_scope,
        )

    # 语义校对相对初译 / 润色 / 局部重译只多两处差异，都由 run_provider_batches 的参数承接：
    # 批次事件多带本轮 1 起序号（with_batch_index，共享循环内部 enumerate 注入），以及失败
    # 不在循环里收口（fail_stage=False）—— stage 失败由 run_translation_semantic_review 的外层
    # except 统一带 phase="semantic_review" 收口，这里再收一次会多发一个 stage_failed 事件。
    # request 显式传，让 `xvt.pipeline.semantic.semantic_review_batch` 这个桩位继续有效。
    run_provider_batches(
        job,
        batches,
        provider_config=provider_config,
        max_retries=config.max_retries,
        event_sink=event_sink,
        request=semantic_review_batch,
        response_correction=_SEMANTIC_REVIEW_RESPONSE_CORRECTION,
        phase=phase,
        with_batch_index=True,
        fail_stage=False,
        skip=lambda batch: batch.id in completed,
        on_result=record_batch,
    )
    # 这次写盘在循环之外：全部批次都跳过（checkpoint 已完整）时也要把报告重写一遍并
    # 拿到登记后的 path。
    path = _write_review_checkpoint(
        job,
        kind=kind,
        path=path,
        task=task,
        batches=batches,
        fingerprint=fingerprint,
        results=results,
        mode=mode,
        event_sink=event_sink,
        preserved_issues=preserved_issues,
        review_scope=review_scope,
    )
    return path, _merge_issues(
        validate_unique_issues([issue for value in results.values() for issue in value]),
        preserved_issues,
    )


def _write_repair_checkpoint(
    job: Job,
    path: Path,
    batches: list[SemanticRepairBatch],
    fingerprint: str,
    completed: set[int],
    repaired: dict[int, str],
    mode: str,
    event_sink: EventSink | None,
) -> Path:
    write_json(
        path,
        {
            "task": "semantic_repair",
            "mode": mode,
            "fingerprint": fingerprint,
            "prompt_version": SEMANTIC_REPAIR_PROMPT_VERSION,
            "semantic_grouping_version": SEMANTIC_GROUPING_VERSION,
            "batches": serialized_batches(batches),
            "completed_batches": sorted(completed),
            "results": [
                {
                    "batch": batch.id,
                    "items": [{"id": item.id, "text": repaired[item.id]} for item in batch.items],
                }
                for batch in batches
                if batch.id in completed
            ],
            "status": "completed" if len(completed) == len(batches) else "running",
        },
    )
    add_artifact(job, "translation_semantic_repair", path, event_sink)
    return path


def _run_repair_phase(
    job: Job,
    config: WorkflowConfig,
    candidate_path: Path,
    report_path: Path,
    issues: tuple[SemanticIssue, ...],
    *,
    mode: str,
    provider_config,
    event_sink: EventSink | None,
) -> tuple[Path, Path]:
    source_path = valid_artifact_path(job, "source_cues")
    if source_path is None or config.target_language is None:
        raise ValueError("semantic repair requires valid source cues and target language")
    source = load_cues(source_path)
    candidate = load_cues(candidate_path)
    batches = build_semantic_repair_batches(
        source,
        candidate,
        issues,
        source_language=source_language(job, source),
        target_language=config.target_language,
        policy=resolved_policy(job, config),
    )
    fingerprint = _fingerprint(
        job,
        candidate_path,
        config,
        phase="repair",
        mode=mode,
        base_url=provider_config.base_url,
        model=provider_config.model,
        report_checksum=checksum(report_path),
    )
    reusable, completed, repaired = load_repair_checkpoint(job, batches, fingerprint)
    if not reusable and any(
        artifact.kind == "translation_semantic_repair" for artifact in job.artifacts
    ):
        _invalidate_semantic_downstream(job, ArtifactKind.TRANSLATION_SEMANTIC_REPAIR)
    progress_path = get_job_directory(job) / "artifacts" / "translation-semantic-repair.json"

    def record_batch(batch: SemanticRepairBatch, values: dict[int, str]) -> None:
        if repaired.keys() & values.keys():
            raise ValueError("semantic repair batches overlap")
        repaired.update(values)
        completed.add(batch.id)
        _write_repair_checkpoint(
            job,
            progress_path,
            batches,
            fingerprint,
            completed,
            repaired,
            mode,
            event_sink,
        )

    # 与初审 / 复审同样的两处差异（batch_index 与不在循环里收口 stage）；修复不传
    # response_correction，沿用 translation 的默认纠错提示词，与重构前一致。
    run_provider_batches(
        job,
        batches,
        provider_config=provider_config,
        max_retries=config.max_retries,
        event_sink=event_sink,
        request=semantic_repair_batch,
        phase="repair",
        with_batch_index=True,
        fail_stage=False,
        skip=lambda batch: batch.id in completed,
        on_result=record_batch,
    )
    progress_path = _write_repair_checkpoint(
        job,
        progress_path,
        batches,
        fingerprint,
        completed,
        repaired,
        mode,
        event_sink,
    )
    expected_ids = {item.id for batch in batches for item in batch.items}
    repaired_cues = apply_semantic_repairs(
        candidate,
        repaired,
        expected_ids,
        target_language=config.target_language,
    )
    repaired_path = get_job_directory(job) / "artifacts" / "translation-repaired-cues.json"
    write_json(repaired_path, [asdict(cue) for cue in repaired_cues])
    add_artifact(job, "translation_repaired_cues", repaired_path, event_sink)
    return progress_path, repaired_path


def _finalize_candidate(
    job: Job,
    candidate_path: Path,
    event_sink: EventSink | None,
) -> tuple[Path, ...]:
    candidate = load_cues(candidate_path)
    translated_path = valid_artifact_path(job, "translated_cues")
    if translated_path is not None and load_cues(translated_path) == candidate:
        # 复用短路必须与 workflow._translation_satisfied 同口径：译文 QA 报告也是
        # 完成条件，漏检会让 polished 链路在 QA 失效时空转短路、continue 永不重建。
        if all(
            valid_artifact_path(job, kind) is not None
            for kind in ("translated_srt", "bilingual_srt", "translated_subtitle_qa")
        ):
            record = job.stages[StageName.TRANSLATE]
            if record.status is not StageStatus.SUCCEEDED or record.error is not None:
                set_stage(job, StageName.TRANSLATE, StageStatus.SUCCEEDED, event_sink)
            return tuple(
                path
                for kind in ("translated_srt", "bilingual_srt", "bilingual_ass")
                if (path := valid_artifact_path(job, kind)) is not None
            )
        rendered = complete_translated_outputs(get_job_file(job), event_sink=event_sink)
        refreshed = load_job(get_job_file(job))
        record = refreshed.stages[StageName.TRANSLATE]
        if record.status is not StageStatus.SUCCEEDED or record.error is not None:
            set_stage(refreshed, StageName.TRANSLATE, StageStatus.SUCCEEDED, event_sink)
        return rendered
    return finalize_translation(
        job,
        candidate,
        event_sink,
        invalidate=invalidate_rendered_artifacts,
        polished_result=True,
    )


def semantic_review_approved(job: Job) -> bool:
    target = semantic_review_target(job)
    if target is None:
        return False
    report, candidate = target
    qa_issues = translation_candidate_qa_issues(job, candidate)
    if qa_issues is None or qa_issues:
        return False
    try:
        status, issues = report_status(report)
        if status == "pass":
            return not issues
        if status == "needs_review" and issues:
            return translation_review_acknowledged(job, report)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return False


def semantic_review_satisfied(job: Job) -> bool:
    if not semantic_review_approved(job):
        return False
    translated = valid_artifact_path(job, "translated_cues")
    if translated is None:
        return False
    target = semantic_review_target(job)
    if target is None:
        return False
    _report, expected = target
    try:
        return load_cues(expected) == load_cues(translated)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def semantic_review_waiting(job: Job) -> bool:
    target = semantic_review_target(job)
    if target is None:
        return False
    report, candidate = target
    try:
        status, issues = report_status(report)
        return bool(translation_candidate_qa_issues(job, candidate)) or (
            status == "needs_review" and bool(issues) and not semantic_review_approved(job)
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def run_translation_semantic_review(
    job_file: str | Path,
    *,
    config: WorkflowConfig | None = None,
    event_sink: EventSink | None = None,
) -> SemanticRunResult:
    job = load_job(job_file)
    resolved = config_for_job(job, config)
    if resolved.translation_quality != "polished":
        raise ValueError("semantic review requires polished translation quality")
    candidate_path = current_semantic_candidate(job)
    reviewed_candidate = valid_artifact_path(job, "translation_reviewed_cues") == candidate_path
    candidate_qa_path, blocking_qa_issues = _ensure_candidate_qa(job, candidate_path, event_sink)
    completed_target = semantic_review_target(job)
    if completed_target is not None and semantic_review_satisfied(job):
        _report_path, completed_candidate_path = completed_target
        rendered = _finalize_candidate(job, completed_candidate_path, event_sink)
        review_paths = tuple(
            path
            for kind in (
                "translation_semantic_qa",
                "translation_semantic_repair",
                "translation_repaired_cues",
                "translation_semantic_qa_final",
                "translation_review_revision",
                "translation_reviewed_cues",
                "translation_semantic_qa_reviewed",
            )
            if (path := valid_artifact_path(job, kind)) is not None
        )
        return SemanticRunResult("completed", "reused", (*review_paths, *rendered))
    if completed_target is not None and blocking_qa_issues:
        set_stage(
            job,
            StageName.TRANSLATE,
            StageStatus.AWAITING_INPUT,
            event_sink,
            extras={"phase": "semantic_review"},
        )
        inputs = ensure_semantic_review_inputs(job_file, event_sink=event_sink)
        return SemanticRunResult("awaiting_review", "reused", (candidate_qa_path, *inputs))
    mode, provider_config, fallback_reason = resolve_provider(
        resolved.provider,
        base_url=resolved.base_url,
        model=resolved.model,
        api_key_env=resolved.api_key_env,
        timeout_seconds=resolved.timeout_seconds,
    )
    if mode == "agent":
        manifest = prepare_agent_semantic_review(
            job_file,
            config=resolved,
            fallback_reason=fallback_reason,
            event_sink=event_sink,
        )
        return SemanticRunResult("awaiting_agent", mode, (manifest,), fallback_reason)
    if provider_config is None:
        raise RuntimeError("semantic review provider configuration was not resolved")
    set_stage(
        job,
        StageName.TRANSLATE,
        StageStatus.RUNNING,
        event_sink,
        extras={"phase": "semantic_review"},
    )
    paths: list[Path] = [candidate_qa_path]
    try:
        if reviewed_candidate:
            source_path = valid_artifact_path(job, "source_cues")
            if source_path is None:
                raise ValueError("semantic review requires valid source cues")
            review_cue_ids, preserved_issues, review_scope = human_revision_scope(
                job,
                candidate_path,
                load_cues(source_path),
                resolved,
            )
            report_path, issues = _run_review_phase(
                job,
                resolved,
                candidate_path,
                kind="translation_semantic_qa_reviewed",
                filename=_reviewed_report_filename(candidate_path),
                task="semantic_review_human_revision",
                phase="semantic_review",
                mode=mode,
                provider_config=provider_config,
                event_sink=event_sink,
                preserved_issues=preserved_issues,
                review_cue_ids=review_cue_ids,
                review_scope=review_scope,
            )
            paths.append(report_path)
            if issues or blocking_qa_issues:
                set_stage(
                    job,
                    StageName.TRANSLATE,
                    StageStatus.AWAITING_INPUT,
                    event_sink,
                    extras={"phase": "semantic_review"},
                )
                paths.extend(ensure_semantic_review_inputs(job_file, event_sink=event_sink))
                return SemanticRunResult("awaiting_review", mode, tuple(paths))
            rendered = _finalize_candidate(job, candidate_path, event_sink)
            return SemanticRunResult("completed", mode, (*paths, *rendered))
        report_path, issues = _run_review_phase(
            job,
            resolved,
            candidate_path,
            kind="translation_semantic_qa",
            filename="translation-semantic-qa.json",
            task="semantic_review",
            phase="semantic_review",
            mode=mode,
            provider_config=provider_config,
            event_sink=event_sink,
        )
        paths.append(report_path)
        repairable = tuple(
            issue for issue in issues if issue.category in SEMANTIC_REPAIR_CATEGORIES
        )
        preserved = tuple(issue for issue in issues if issue.category == "source_suspect")
        if repairable:
            repair_path, repaired_path = _run_repair_phase(
                job,
                resolved,
                candidate_path,
                report_path,
                issues,
                mode=mode,
                provider_config=provider_config,
                event_sink=event_sink,
            )
            paths.extend((repair_path, repaired_path))
            final_path, final_issues = _run_review_phase(
                job,
                resolved,
                repaired_path,
                kind="translation_semantic_qa_final",
                filename="translation-semantic-qa-final.json",
                task="semantic_review_final",
                phase="semantic_review",
                mode=mode,
                provider_config=provider_config,
                event_sink=event_sink,
                preserved_issues=preserved,
            )
            paths.append(final_path)
            candidate_path = repaired_path
            issues = final_issues
            candidate_qa_path, blocking_qa_issues = _ensure_candidate_qa(
                job, candidate_path, event_sink
            )
            paths.append(candidate_qa_path)
        if issues or blocking_qa_issues:
            set_stage(
                job,
                StageName.TRANSLATE,
                StageStatus.AWAITING_INPUT,
                event_sink,
                extras={"phase": "semantic_review"},
            )
            paths.extend(ensure_semantic_review_inputs(job_file, event_sink=event_sink))
            return SemanticRunResult("awaiting_review", mode, tuple(paths))
        rendered = _finalize_candidate(job, candidate_path, event_sink)
        return SemanticRunResult("completed", mode, (*paths, *rendered))
    except Exception as error:
        set_stage(
            job,
            StageName.TRANSLATE,
            StageStatus.FAILED,
            event_sink,
            error=str(error),
            error_category=error_category(error),
            extras={"phase": "semantic_review"},
        )
        raise


def _agent_manifest_fingerprint(payload: dict[str, Any]) -> str:
    canonical = {key: value for key, value in payload.items() if key != "fingerprint"}
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()


def prepare_agent_semantic_review(
    job_file: str | Path,
    *,
    config: WorkflowConfig | None = None,
    fallback_reason: str | None = None,
    event_sink: EventSink | None = None,
) -> Path:
    job = load_job(job_file)
    resolved = config_for_job(job, config)
    candidate_path = current_semantic_candidate(job)
    candidate_qa_path, _blocking_qa_issues = _ensure_candidate_qa(job, candidate_path, event_sink)
    reviewed_candidate = valid_artifact_path(job, "translation_reviewed_cues") == candidate_path
    source_path = valid_artifact_path(job, "source_cues")
    if source_path is None:
        raise ValueError("Agent semantic review requires valid source cues")
    review_cue_ids, preserved_issues, review_scope = human_revision_scope(
        job,
        candidate_path,
        load_cues(source_path),
        resolved,
    )
    batches = build_review_batches(
        job,
        candidate_path,
        resolved,
        review_cue_ids=review_cue_ids,
    )
    report_kind = (
        ArtifactKind.TRANSLATION_SEMANTIC_QA_REVIEWED
        if reviewed_candidate
        else ArtifactKind.TRANSLATION_SEMANTIC_QA
    )
    _invalidate_semantic_downstream(job, report_kind)
    payload: dict[str, Any] = {
        "job_id": job.id,
        "task": "semantic_review_only" if reviewed_candidate else "semantic_review_and_repair",
        "task_version": SEMANTIC_AGENT_TASK_VERSION,
        "source_cues_checksum": checksum(source_path),
        "candidate_cues_checksum": checksum(candidate_path),
        "candidate_qa_checksum": checksum(candidate_qa_path),
        "target_language": resolved.target_language,
        "review_prompt_version": SEMANTIC_REVIEW_PROMPT_VERSION,
        "repair_prompt_version": SEMANTIC_REPAIR_PROMPT_VERSION,
        "semantic_grouping_version": SEMANTIC_GROUPING_VERSION,
        "batches": serialized_batches(batches),
        "status": "awaiting_input",
    }
    if review_scope is not None:
        payload["review_scope"] = review_scope
        payload["preserved_issues"] = serialized_issues(preserved_issues)
    if fallback_reason:
        payload["fallback_reason"] = fallback_reason
    payload["fingerprint"] = _agent_manifest_fingerprint(payload)
    filename = (
        _reviewed_report_filename(candidate_path)
        if reviewed_candidate
        else "translation-semantic-qa.json"
    )
    path = get_job_directory(job) / "artifacts" / filename
    write_json(path, payload)
    add_artifact(job, report_kind.value, path, event_sink)
    set_stage(
        job,
        StageName.TRANSLATE,
        StageStatus.AWAITING_INPUT,
        event_sink,
        extras={"phase": "semantic_review"},
    )
    return path


def agent_semantic_review_waiting(job: Job) -> bool:
    path = valid_artifact_path(job, "translation_semantic_qa_reviewed") or valid_artifact_path(
        job, "translation_semantic_qa"
    )
    if path is None:
        return False
    try:
        _validated_agent_manifest(job, path)
        return True
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _validated_agent_manifest(
    job: Job,
    path: Path,
) -> tuple[
    dict[str, Any],
    WorkflowConfig,
    Path,
    list[SemanticReviewBatch],
    tuple[SemanticIssue, ...],
    dict[str, Any] | None,
]:
    data = json.loads(path.read_text(encoding="utf-8"))
    resolved = config_for_job(job, None)
    candidate_path = current_semantic_candidate(job)
    source_path = valid_artifact_path(job, "source_cues")
    candidate_qa_path = valid_artifact_path(job, "translation_candidate_qa")
    if source_path is None or candidate_qa_path is None:
        raise ValueError("Agent semantic review requires valid source cues")
    review_cue_ids, preserved_issues, review_scope = human_revision_scope(
        job,
        candidate_path,
        load_cues(source_path),
        resolved,
    )
    batches = build_review_batches(
        job,
        candidate_path,
        resolved,
        review_cue_ids=review_cue_ids,
    )
    expected_preserved = serialized_issues(preserved_issues) if review_scope is not None else None
    fingerprint = data.get("fingerprint")
    if not (
        data.get("job_id") == job.id
        and data.get("task")
        == (
            "semantic_review_only"
            if valid_artifact_path(job, "translation_reviewed_cues") == candidate_path
            else "semantic_review_and_repair"
        )
        and data.get("task_version") == SEMANTIC_AGENT_TASK_VERSION
        and data.get("source_cues_checksum") == checksum(source_path)
        and data.get("candidate_cues_checksum") == checksum(candidate_path)
        and data.get("candidate_qa_checksum") == checksum(candidate_qa_path)
        and data.get("target_language") == resolved.target_language
        and data.get("review_prompt_version") == SEMANTIC_REVIEW_PROMPT_VERSION
        and data.get("repair_prompt_version") == SEMANTIC_REPAIR_PROMPT_VERSION
        and data.get("semantic_grouping_version") == SEMANTIC_GROUPING_VERSION
        and data.get("batches") == serialized_batches(batches)
        and data.get("review_scope") == review_scope
        and data.get("preserved_issues") == expected_preserved
        and data.get("status") == "awaiting_input"
        and isinstance(fingerprint, str)
        and fingerprint == _agent_manifest_fingerprint(data)
    ):
        raise ValueError("Agent semantic review manifest is invalid or stale")
    return data, resolved, candidate_path, batches, preserved_issues, review_scope


def apply_agent_semantic_review(
    job_file: str | Path,
    response_file: str | Path,
    *,
    event_sink: EventSink | None = None,
) -> SemanticRunResult:
    job = load_job(job_file)
    manifest_path = valid_artifact_path(
        job, "translation_semantic_qa_reviewed"
    ) or valid_artifact_path(job, "translation_semantic_qa")
    if manifest_path is None:
        raise ValueError("job has no valid Agent semantic review manifest")
    (
        manifest,
        resolved,
        candidate_path,
        review_batches,
        preserved_revision_issues,
        review_scope,
    ) = _validated_agent_manifest(job, manifest_path)
    response = load_user_response_json(response_file, what="semantic review response")
    if not isinstance(response, dict) or response.get("fingerprint") != manifest.get("fingerprint"):
        raise ValueError("Agent semantic response fingerprint does not match the current manifest")
    initial_results = agent_results(
        "initial_results", response.get("initial_results"), review_batches
    )
    initial_issues = validate_unique_issues(
        [issue for value in initial_results.values() for issue in value]
    )
    review_only = manifest.get("task") == "semantic_review_only"
    repairable = tuple(
        issue for issue in initial_issues if issue.category in SEMANTIC_REPAIR_CATEGORIES
    )
    preserved = tuple(issue for issue in initial_issues if issue.category == "source_suspect")
    final_issues = _merge_issues(initial_issues, preserved_revision_issues)
    repair_state = None
    if repairable and not review_only:
        source_path = valid_artifact_path(job, "source_cues")
        if source_path is None or resolved.target_language is None:
            raise ValueError("Agent semantic repair requires valid source cues")
        source = load_cues(source_path)
        candidate = load_cues(candidate_path)
        repair_batches = build_semantic_repair_batches(
            source,
            candidate,
            initial_issues,
            source_language=source_language(job, source),
            target_language=resolved.target_language,
            policy=resolved_policy(job, resolved),
        )
        repair_raw = response.get("repair_results")
        if not isinstance(repair_raw, list):
            raise ValueError("Agent semantic response repair_results must be an array")
        repair_by_id = {batch.id: batch for batch in repair_batches}
        repaired: dict[int, str] = {}
        completed: set[int] = set()
        for result in repair_raw:
            batch_id = result.get("batch") if isinstance(result, dict) else None
            items = result.get("items") if isinstance(result, dict) else None
            if batch_id not in repair_by_id or batch_id in completed or not isinstance(items, list):
                raise ValueError("Agent semantic response contains an invalid repair batch")
            values = parse_semantic_repair_response(repair_by_id[batch_id], items)
            if repaired.keys() & values.keys():
                raise ValueError("Agent semantic repair batches overlap")
            repaired.update(values)
            completed.add(batch_id)
        if completed != set(repair_by_id):
            raise ValueError("Agent semantic response must cover every repair batch")
        repaired_cues = apply_semantic_repairs(
            candidate,
            repaired,
            {item.id for batch in repair_batches for item in batch.items},
            target_language=resolved.target_language,
        )
        final_batches = build_semantic_review_batches(
            source,
            repaired_cues,
            source_language=source_language(job, source),
            target_language=resolved.target_language,
            batch_size=resolved.batch_size,
            policy=resolved_policy(job, resolved),
        )
        final_results = agent_results("final_results", response.get("final_results"), final_batches)
        final_issues = _merge_issues(
            validate_unique_issues([issue for value in final_results.values() for issue in value]),
            preserved,
        )
        repair_state = (
            repair_batches,
            completed,
            repaired,
            repaired_cues,
            final_batches,
            final_results,
        )
    elif response.get("repair_results") not in (None, []) or response.get("final_results") not in (
        None,
        [],
    ):
        raise ValueError("Agent semantic response must not repair when no repairable issue exists")

    report_kind = "translation_semantic_qa_reviewed" if review_only else "translation_semantic_qa"
    _write_review_checkpoint(
        job,
        kind=report_kind,
        path=manifest_path,
        task="semantic_review_human_revision" if review_only else "semantic_review",
        batches=review_batches,
        fingerprint=str(manifest["fingerprint"]),
        results=initial_results,
        mode="agent",
        event_sink=event_sink,
        preserved_issues=preserved_revision_issues,
        review_scope=review_scope,
    )
    paths: list[Path] = [manifest_path]
    if repair_state is not None:
        (
            repair_batches,
            completed,
            repaired,
            repaired_cues,
            final_batches,
            final_results,
        ) = repair_state
        repair_path = get_job_directory(job) / "artifacts" / "translation-semantic-repair.json"
        _write_repair_checkpoint(
            job,
            repair_path,
            repair_batches,
            str(manifest["fingerprint"]),
            completed,
            repaired,
            "agent",
            event_sink,
        )
        repaired_path = get_job_directory(job) / "artifacts" / "translation-repaired-cues.json"
        write_json(repaired_path, [asdict(cue) for cue in repaired_cues])
        add_artifact(job, "translation_repaired_cues", repaired_path, event_sink)
        final_path = get_job_directory(job) / "artifacts" / "translation-semantic-qa-final.json"
        _write_review_checkpoint(
            job,
            kind="translation_semantic_qa_final",
            path=final_path,
            task="semantic_review_final",
            batches=final_batches,
            fingerprint=str(manifest["fingerprint"]),
            results=final_results,
            mode="agent",
            event_sink=event_sink,
            preserved_issues=preserved,
        )
        candidate_path = repaired_path
        paths.extend((repair_path, repaired_path, final_path))
    candidate_qa_path, blocking_qa_issues = _ensure_candidate_qa(job, candidate_path, event_sink)
    paths.append(candidate_qa_path)
    if final_issues or blocking_qa_issues:
        set_stage(
            job,
            StageName.TRANSLATE,
            StageStatus.AWAITING_INPUT,
            event_sink,
            extras={"phase": "semantic_review"},
        )
        paths.extend(ensure_semantic_review_inputs(job_file, event_sink=event_sink))
        return SemanticRunResult("awaiting_review", "agent", tuple(paths))
    rendered = _finalize_candidate(job, candidate_path, event_sink)
    return SemanticRunResult("completed", "agent", (*paths, *rendered))


def complete_acknowledged_semantic_review(
    job_file: str | Path,
    *,
    event_sink: EventSink | None = None,
) -> tuple[Path, ...]:
    job = load_job(job_file)
    target = semantic_review_target(job)
    if target is None or not semantic_review_approved(job):
        raise ValueError("semantic review has not been acknowledged for the current report")
    _report, candidate = target
    return _finalize_candidate(job, candidate, event_sink)

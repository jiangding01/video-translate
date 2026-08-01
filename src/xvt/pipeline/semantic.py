"""可恢复的译文语义校对、一次定向修复和人工确认前置状态。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

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
    _finalize_translation,
    _translate_batch_with_retries,
    complete_translated_outputs,
)
from xvt.process import raise_if_cancelled
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
    translation_policy_from_dict,
)
from xvt.translation_provider import (
    SEMANTIC_REPAIR_PROMPT_VERSION,
    SEMANTIC_REVIEW_PROMPT_VERSION,
    resolve_provider,
    semantic_repair_batch,
    semantic_review_batch,
)
from xvt.workflow_config import WorkflowConfig, clear_burn_fingerprint, get_workflow_config

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


def _config_for_job(job: Job, config: WorkflowConfig | None) -> WorkflowConfig:
    if config is not None:
        return config
    workflow = get_workflow_config(job)
    if workflow is not None:
        return workflow
    target_language = job.config.get("target_language")
    if not isinstance(target_language, str) or not target_language:
        raise ValueError("semantic review requires a persisted target language")
    return WorkflowConfig(
        target_language=target_language,
        provider=str(job.config.get("provider", "auto")),
        base_url=job.config.get("base_url"),
        model=job.config.get("model"),
        api_key_env=str(job.config.get("api_key_env", "XVT_TRANSLATION_API_KEY")),
        batch_size=int(job.config.get("batch_size", 40)),
        max_retries=int(job.config.get("max_retries", 4)),
        timeout_seconds=float(job.config.get("timeout_seconds", 60)),
        translation_policy=translation_policy_from_dict(job.config.get("translation_policy")),
        translation_quality="polished",
    )


def _candidate_path(job: Job) -> Path:
    reviewed = valid_artifact_path(job, "translation_reviewed_cues")
    if reviewed is not None:
        return reviewed
    path = valid_artifact_path(job, "translation_polished_cues")
    if path is None:
        raise ValueError("semantic review requires checksum-valid polished translation cues")
    return path


def _reviewed_report_filename(candidate_path: Path) -> str:
    return f"translation-semantic-qa-reviewed-{_checksum(candidate_path)[:12]}.json"


def _source_language(job: Job, source: list[Cue]) -> str:
    return next(
        (cue.source_language for cue in source if cue.source_language),
        str(job.config.get("source_language", "auto")),
    )


def _review_batches(
    job: Job, candidate_path: Path, config: WorkflowConfig
) -> list[SemanticReviewBatch]:
    source_path = valid_artifact_path(job, "source_cues")
    if source_path is None or config.target_language is None:
        raise ValueError("semantic review requires valid source cues and a target language")
    source = _load_cues(source_path)
    return build_semantic_review_batches(
        source,
        _load_cues(candidate_path),
        source_language=_source_language(job, source),
        target_language=config.target_language,
        batch_size=config.batch_size,
        policy=config.translation_policy
        or translation_policy_from_dict(job.config.get("translation_policy")),
    )


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
) -> str:
    source_path = valid_artifact_path(job, "source_cues")
    if source_path is None:
        raise ValueError("semantic review fingerprint requires valid source cues")
    payload = {
        "source_cues_checksum": _checksum(source_path),
        "candidate_cues_checksum": _checksum(candidate_path),
        "target_language": config.target_language,
        "provider": mode,
        "base_url": base_url,
        "model": model,
        "batch_size": config.batch_size,
        "policy": (
            config.translation_policy
            or translation_policy_from_dict(job.config.get("translation_policy"))
        ).to_dict(),
        "phase": phase,
        "prompt_version": (
            SEMANTIC_REPAIR_PROMPT_VERSION if phase == "repair" else SEMANTIC_REVIEW_PROMPT_VERSION
        ),
        "semantic_grouping_version": SEMANTIC_GROUPING_VERSION,
        "report_checksum": report_checksum,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _emit_batch_event(
    event_type: EventType,
    job: Job,
    phase: str,
    batch: int,
    total: int,
    event_sink: EventSink | None,
    **data: Any,
) -> None:
    if event_sink:
        event_sink(
            create_event(
                event_type,
                **job_event_fields(job.id, get_job_file(job)),
                phase=phase,
                batch=batch,
                batch_total=total,
                **data,
            )
        )


def _canonical_issue_key(issue: SemanticIssue) -> tuple[str, tuple[int, ...]]:
    return issue.category, issue.cue_ids


def _validate_unique_issues(issues: list[SemanticIssue]) -> tuple[SemanticIssue, ...]:
    seen: set[tuple[str, tuple[int, ...]]] = set()
    for issue in issues:
        key = _canonical_issue_key(issue)
        if key in seen:
            raise ValueError("semantic review contains duplicate issues across batches")
        seen.add(key)
    return tuple(issues)


def _merge_issues(
    primary: tuple[SemanticIssue, ...],
    preserved: tuple[SemanticIssue, ...],
) -> tuple[SemanticIssue, ...]:
    merged = list(primary)
    seen = {_canonical_issue_key(issue) for issue in primary}
    for issue in preserved:
        if _canonical_issue_key(issue) not in seen:
            merged.append(issue)
            seen.add(_canonical_issue_key(issue))
    return _validate_unique_issues(merged)


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


def _serialized_batches(
    batches: list[SemanticReviewBatch] | list[SemanticRepairBatch],
) -> list[dict[str, Any]]:
    return json.loads(json.dumps([batch.to_dict() for batch in batches], ensure_ascii=False))


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
        if data.get("fingerprint") != fingerprint or data.get("batches") != _serialized_batches(
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
        _validate_unique_issues([issue for value in parsed.values() for issue in value])
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
) -> Path:
    complete = len(results) == len(batches)
    issues = _merge_issues(
        _validate_unique_issues([issue for value in results.values() for issue in value]),
        preserved_issues if complete else (),
    )
    _write_json(
        path,
        {
            "task": task,
            "mode": mode,
            "fingerprint": fingerprint,
            "prompt_version": SEMANTIC_REVIEW_PROMPT_VERSION,
            "semantic_grouping_version": SEMANTIC_GROUPING_VERSION,
            "batches": _serialized_batches(batches),
            "completed_batches": sorted(results),
            "results": [
                {
                    "batch": batch_id,
                    "issues": [issue.to_dict() for issue in results[batch_id]],
                }
                for batch_id in sorted(results)
            ],
            "issues": [issue.to_dict() for issue in issues] if complete else [],
            "status": (
                "pass" if complete and not issues else "needs_review" if complete else "running"
            ),
        },
    )
    _add_artifact(job, kind, path, event_sink)
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
) -> tuple[Path, tuple[SemanticIssue, ...]]:
    batches = _review_batches(job, candidate_path, config)
    fingerprint = _fingerprint(
        job,
        candidate_path,
        config,
        phase=phase,
        mode=mode,
        base_url=provider_config.base_url,
        model=provider_config.model,
    )
    reusable, completed, results = _load_review_checkpoint(job, kind, batches, fingerprint)
    if not reusable and any(artifact.kind == kind for artifact in job.artifacts):
        _invalidate_semantic_downstream(job, ArtifactKind(kind))
    path = get_job_directory(job) / "artifacts" / filename
    for batch in batches:
        raise_if_cancelled()
        if batch.id in completed:
            continue
        _emit_batch_event(
            EventType.TRANSLATION_BATCH_STARTED,
            job,
            phase,
            batch.id,
            len(batches),
            event_sink,
        )

        def emit_retry(attempt: int, delay: float, reason: str) -> None:
            _emit_batch_event(
                EventType.TRANSLATION_BATCH_RETRYING,
                job,
                phase,
                batch.id,
                len(batches),
                event_sink,
                attempt=attempt,
                delay_seconds=delay,
                reason=reason,
            )

        results[batch.id] = _translate_batch_with_retries(
            batch,
            provider_config,
            max_retries=config.max_retries,
            on_retry=emit_retry,
            request=semantic_review_batch,
            response_correction=_SEMANTIC_REVIEW_RESPONSE_CORRECTION,
        )
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
        )
        _emit_batch_event(
            EventType.TRANSLATION_BATCH_COMPLETED,
            job,
            phase,
            batch.id,
            len(batches),
            event_sink,
        )
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
    )
    return path, _merge_issues(
        _validate_unique_issues([issue for value in results.values() for issue in value]),
        preserved_issues,
    )


def _load_repair_checkpoint(
    job: Job,
    batches: list[SemanticRepairBatch],
    fingerprint: str,
) -> tuple[bool, set[int], dict[int, str]]:
    path = valid_artifact_path(job, "translation_semantic_repair")
    if path is None:
        return False, set(), {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("fingerprint") != fingerprint or data.get("batches") != _serialized_batches(
            batches
        ):
            raise ValueError
        results = data.get("results")
        if not isinstance(results, list):
            raise ValueError
        batches_by_id = {batch.id: batch for batch in batches}
        repaired: dict[int, str] = {}
        completed: set[int] = set()
        for result in results:
            batch_id = result.get("batch") if isinstance(result, dict) else None
            items = result.get("items") if isinstance(result, dict) else None
            if (
                batch_id not in batches_by_id
                or batch_id in completed
                or not isinstance(items, list)
            ):
                raise ValueError
            values = parse_semantic_repair_response(batches_by_id[batch_id], items)
            if repaired.keys() & values.keys():
                raise ValueError
            repaired.update(values)
            completed.add(batch_id)
        return True, completed, repaired
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False, set(), {}


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
    _write_json(
        path,
        {
            "task": "semantic_repair",
            "mode": mode,
            "fingerprint": fingerprint,
            "prompt_version": SEMANTIC_REPAIR_PROMPT_VERSION,
            "semantic_grouping_version": SEMANTIC_GROUPING_VERSION,
            "batches": _serialized_batches(batches),
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
    _add_artifact(job, "translation_semantic_repair", path, event_sink)
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
    source = _load_cues(source_path)
    candidate = _load_cues(candidate_path)
    batches = build_semantic_repair_batches(
        source,
        candidate,
        issues,
        source_language=_source_language(job, source),
        target_language=config.target_language,
        policy=config.translation_policy
        or translation_policy_from_dict(job.config.get("translation_policy")),
    )
    fingerprint = _fingerprint(
        job,
        candidate_path,
        config,
        phase="repair",
        mode=mode,
        base_url=provider_config.base_url,
        model=provider_config.model,
        report_checksum=_checksum(report_path),
    )
    reusable, completed, repaired = _load_repair_checkpoint(job, batches, fingerprint)
    if not reusable and any(
        artifact.kind == "translation_semantic_repair" for artifact in job.artifacts
    ):
        _invalidate_semantic_downstream(job, ArtifactKind.TRANSLATION_SEMANTIC_REPAIR)
    progress_path = get_job_directory(job) / "artifacts" / "translation-semantic-repair.json"
    for batch in batches:
        raise_if_cancelled()
        if batch.id in completed:
            continue
        _emit_batch_event(
            EventType.TRANSLATION_BATCH_STARTED,
            job,
            "repair",
            batch.id,
            len(batches),
            event_sink,
        )

        def emit_retry(attempt: int, delay: float, reason: str) -> None:
            _emit_batch_event(
                EventType.TRANSLATION_BATCH_RETRYING,
                job,
                "repair",
                batch.id,
                len(batches),
                event_sink,
                attempt=attempt,
                delay_seconds=delay,
                reason=reason,
            )

        values = _translate_batch_with_retries(
            batch,
            provider_config,
            max_retries=config.max_retries,
            on_retry=emit_retry,
            request=semantic_repair_batch,
        )
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
        _emit_batch_event(
            EventType.TRANSLATION_BATCH_COMPLETED,
            job,
            "repair",
            batch.id,
            len(batches),
            event_sink,
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
    repaired_cues = apply_semantic_repairs(candidate, repaired, expected_ids)
    repaired_path = get_job_directory(job) / "artifacts" / "translation-repaired-cues.json"
    _write_json(repaired_path, [asdict(cue) for cue in repaired_cues])
    _add_artifact(job, "translation_repaired_cues", repaired_path, event_sink)
    return progress_path, repaired_path


def _finalize_candidate(
    job: Job,
    candidate_path: Path,
    event_sink: EventSink | None,
) -> tuple[Path, ...]:
    candidate = _load_cues(candidate_path)
    translated_path = valid_artifact_path(job, "translated_cues")
    if translated_path is not None and _load_cues(translated_path) == candidate:
        if all(
            valid_artifact_path(job, kind) is not None
            for kind in ("translated_srt", "bilingual_srt")
        ):
            return tuple(
                path
                for kind in ("translated_srt", "bilingual_srt", "bilingual_ass")
                if (path := valid_artifact_path(job, kind)) is not None
            )
        return complete_translated_outputs(get_job_file(job), event_sink=event_sink)
    return _finalize_translation(
        job,
        candidate,
        event_sink,
        invalidate=_invalidate_rendered_artifacts,
        polished_result=True,
    )


def _report_status(path: Path) -> tuple[str, tuple[dict[str, Any], ...]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    issues = data.get("issues")
    if not isinstance(issues, list):
        raise ValueError("semantic review report has no issues array")
    return str(data.get("status")), tuple(item for item in issues if isinstance(item, dict))


def _report_matches_candidate(
    job: Job,
    report_path: Path,
    candidate_path: Path,
) -> bool:
    try:
        config = _config_for_job(job, None)
        batches = _review_batches(job, candidate_path, config)
        data = json.loads(report_path.read_text(encoding="utf-8"))
        results = data.get("results")
        completed = data.get("completed_batches")
        if (
            data.get("prompt_version") != SEMANTIC_REVIEW_PROMPT_VERSION
            or data.get("semantic_grouping_version") != SEMANTIC_GROUPING_VERSION
            or data.get("batches") != _serialized_batches(batches)
            or completed != [batch.id for batch in batches]
            or not isinstance(results, list)
        ):
            return False
        parsed = _agent_results("report results", results, batches)
        parsed_issues = _validate_unique_issues(
            [issue for value in parsed.values() for issue in value]
        )
        status, issues = _report_status(report_path)
        issue_keys = {
            (item.get("category"), tuple(sorted(item.get("cue_ids", ())))) for item in issues
        }
        parsed_keys = {_canonical_issue_key(issue) for issue in parsed_issues}
        if not parsed_keys <= issue_keys:
            return False
        return status == ("pass" if not issues else "needs_review")
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _repair_chain_satisfied(
    job: Job,
    polished_path: Path,
    initial_report: Path,
    repaired_path: Path,
) -> bool:
    repair_path = valid_artifact_path(job, "translation_semantic_repair")
    if repair_path is None:
        return False
    try:
        config = _config_for_job(job, None)
        source_path = valid_artifact_path(job, "source_cues")
        if source_path is None or config.target_language is None:
            return False
        report = json.loads(initial_report.read_text(encoding="utf-8"))
        raw_issues = report.get("issues")
        if not isinstance(raw_issues, list):
            return False
        review_batches = _review_batches(job, polished_path, config)
        issues = _agent_results(
            "initial report",
            report.get("results"),
            review_batches,
        )
        all_issues = _validate_unique_issues(
            [issue for value in issues.values() for issue in value]
        )
        source = _load_cues(source_path)
        candidate = _load_cues(polished_path)
        repair_batches = build_semantic_repair_batches(
            source,
            candidate,
            all_issues,
            source_language=_source_language(job, source),
            target_language=config.target_language,
            policy=config.translation_policy,
        )
        repair_data = json.loads(repair_path.read_text(encoding="utf-8"))
        if repair_data.get("batches") != _serialized_batches(repair_batches):
            return False
        fingerprint = repair_data.get("fingerprint")
        if not isinstance(fingerprint, str):
            return False
        reusable, completed, repaired = _load_repair_checkpoint(job, repair_batches, fingerprint)
        expected_ids = {item.id for batch in repair_batches for item in batch.items}
        return bool(
            reusable
            and completed == {batch.id for batch in repair_batches}
            and _load_cues(repaired_path)
            == apply_semantic_repairs(candidate, repaired, expected_ids)
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def semantic_review_target(job: Job) -> tuple[Path, Path] | None:
    reviewed = valid_artifact_path(job, "translation_reviewed_cues")
    reviewed_report = valid_artifact_path(job, "translation_semantic_qa_reviewed")
    if (
        reviewed is not None
        and reviewed_report is not None
        and _report_matches_candidate(job, reviewed_report, reviewed)
    ):
        return reviewed_report, reviewed
    polished = valid_artifact_path(job, "translation_polished_cues")
    initial = valid_artifact_path(job, "translation_semantic_qa")
    if polished is None or initial is None or not _report_matches_candidate(job, initial, polished):
        return None
    final = valid_artifact_path(job, "translation_semantic_qa_final")
    repaired = valid_artifact_path(job, "translation_repaired_cues")
    if (
        final is not None
        and repaired is not None
        and _repair_chain_satisfied(job, polished, initial, repaired)
        and _report_matches_candidate(job, final, repaired)
    ):
        return final, repaired
    try:
        _, issues = _report_status(initial)
        if any(item.get("category") in SEMANTIC_REPAIR_CATEGORIES for item in issues):
            return None
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return initial, polished


def semantic_review_approved(job: Job) -> bool:
    target = semantic_review_target(job)
    if target is None:
        return False
    report, _candidate = target
    try:
        status, issues = _report_status(report)
        if status == "pass":
            return not issues
        if status == "needs_review" and issues:
            from xvt.pipeline.review import translation_review_acknowledged

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
        return _load_cues(expected) == _load_cues(translated)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def semantic_review_waiting(job: Job) -> bool:
    target = semantic_review_target(job)
    if target is None:
        return False
    report, _candidate = target
    try:
        status, issues = _report_status(report)
        return status == "needs_review" and bool(issues) and not semantic_review_approved(job)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def run_translation_semantic_review(
    job_file: str | Path,
    *,
    config: WorkflowConfig | None = None,
    event_sink: EventSink | None = None,
) -> SemanticRunResult:
    job = load_job(job_file)
    resolved = _config_for_job(job, config)
    if resolved.translation_quality != "polished":
        raise ValueError("semantic review requires polished translation quality")
    candidate_path = _candidate_path(job)
    reviewed_candidate = valid_artifact_path(job, "translation_reviewed_cues") == candidate_path
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
    _set_stage(
        job,
        StageName.TRANSLATE,
        StageStatus.RUNNING,
        event_sink,
        extras={"phase": "semantic_review"},
    )
    paths: list[Path] = []
    try:
        if reviewed_candidate:
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
            )
            paths.append(report_path)
            if issues:
                _set_stage(
                    job,
                    StageName.TRANSLATE,
                    StageStatus.AWAITING_INPUT,
                    event_sink,
                    extras={"phase": "semantic_review"},
                )
                from xvt.pipeline.revision import ensure_translation_review_input

                paths.append(ensure_translation_review_input(job_file, event_sink=event_sink))
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
        if issues:
            _set_stage(
                job,
                StageName.TRANSLATE,
                StageStatus.AWAITING_INPUT,
                event_sink,
                extras={"phase": "semantic_review"},
            )
            from xvt.pipeline.revision import ensure_translation_review_input

            paths.append(ensure_translation_review_input(job_file, event_sink=event_sink))
            return SemanticRunResult("awaiting_review", mode, tuple(paths))
        rendered = _finalize_candidate(job, candidate_path, event_sink)
        return SemanticRunResult("completed", mode, (*paths, *rendered))
    except Exception as error:
        _set_stage(
            job,
            StageName.TRANSLATE,
            StageStatus.FAILED,
            event_sink,
            error=str(error),
            error_category=_error_category(error),
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
    resolved = _config_for_job(job, config)
    candidate_path = _candidate_path(job)
    reviewed_candidate = valid_artifact_path(job, "translation_reviewed_cues") == candidate_path
    batches = _review_batches(job, candidate_path, resolved)
    source_path = valid_artifact_path(job, "source_cues")
    if source_path is None:
        raise ValueError("Agent semantic review requires valid source cues")
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
        "source_cues_checksum": _checksum(source_path),
        "candidate_cues_checksum": _checksum(candidate_path),
        "target_language": resolved.target_language,
        "review_prompt_version": SEMANTIC_REVIEW_PROMPT_VERSION,
        "repair_prompt_version": SEMANTIC_REPAIR_PROMPT_VERSION,
        "semantic_grouping_version": SEMANTIC_GROUPING_VERSION,
        "batches": _serialized_batches(batches),
        "status": "awaiting_input",
    }
    if fallback_reason:
        payload["fallback_reason"] = fallback_reason
    payload["fingerprint"] = _agent_manifest_fingerprint(payload)
    filename = (
        _reviewed_report_filename(candidate_path)
        if reviewed_candidate
        else "translation-semantic-qa.json"
    )
    path = get_job_directory(job) / "artifacts" / filename
    _write_json(path, payload)
    _add_artifact(job, report_kind.value, path, event_sink)
    _set_stage(
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
) -> tuple[dict[str, Any], WorkflowConfig, Path, list[SemanticReviewBatch]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    resolved = _config_for_job(job, None)
    candidate_path = _candidate_path(job)
    source_path = valid_artifact_path(job, "source_cues")
    if source_path is None:
        raise ValueError("Agent semantic review requires valid source cues")
    batches = _review_batches(job, candidate_path, resolved)
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
        and data.get("source_cues_checksum") == _checksum(source_path)
        and data.get("candidate_cues_checksum") == _checksum(candidate_path)
        and data.get("target_language") == resolved.target_language
        and data.get("review_prompt_version") == SEMANTIC_REVIEW_PROMPT_VERSION
        and data.get("repair_prompt_version") == SEMANTIC_REPAIR_PROMPT_VERSION
        and data.get("semantic_grouping_version") == SEMANTIC_GROUPING_VERSION
        and data.get("batches") == _serialized_batches(batches)
        and data.get("status") == "awaiting_input"
        and isinstance(fingerprint, str)
        and fingerprint == _agent_manifest_fingerprint(data)
    ):
        raise ValueError("Agent semantic review manifest is invalid or stale")
    return data, resolved, candidate_path, batches


def _agent_results(
    name: str,
    raw: Any,
    batches: list[SemanticReviewBatch],
) -> dict[int, tuple[SemanticIssue, ...]]:
    if not isinstance(raw, list):
        raise ValueError(f"Agent semantic response {name} must be an array")
    by_id = {batch.id: batch for batch in batches}
    results: dict[int, tuple[SemanticIssue, ...]] = {}
    for item in raw:
        batch_id = item.get("batch") if isinstance(item, dict) else None
        issues = item.get("issues") if isinstance(item, dict) else None
        if batch_id not in by_id or batch_id in results or not isinstance(issues, list):
            raise ValueError(f"Agent semantic response {name} contains an invalid batch")
        results[batch_id] = parse_semantic_review_response(by_id[batch_id], issues)
    if set(results) != set(by_id):
        raise ValueError(f"Agent semantic response {name} must cover every batch")
    _validate_unique_issues([issue for value in results.values() for issue in value])
    return results


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
    manifest, resolved, candidate_path, review_batches = _validated_agent_manifest(
        job, manifest_path
    )
    response = json.loads(Path(response_file).read_text(encoding="utf-8"))
    if not isinstance(response, dict) or response.get("fingerprint") != manifest.get("fingerprint"):
        raise ValueError("Agent semantic response fingerprint does not match the current manifest")
    initial_results = _agent_results(
        "initial_results", response.get("initial_results"), review_batches
    )
    initial_issues = _validate_unique_issues(
        [issue for value in initial_results.values() for issue in value]
    )
    review_only = manifest.get("task") == "semantic_review_only"
    repairable = tuple(
        issue for issue in initial_issues if issue.category in SEMANTIC_REPAIR_CATEGORIES
    )
    preserved = tuple(issue for issue in initial_issues if issue.category == "source_suspect")
    final_issues = initial_issues
    repair_state = None
    if repairable and not review_only:
        source_path = valid_artifact_path(job, "source_cues")
        if source_path is None or resolved.target_language is None:
            raise ValueError("Agent semantic repair requires valid source cues")
        source = _load_cues(source_path)
        candidate = _load_cues(candidate_path)
        repair_batches = build_semantic_repair_batches(
            source,
            candidate,
            initial_issues,
            source_language=_source_language(job, source),
            target_language=resolved.target_language,
            policy=resolved.translation_policy,
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
        )
        final_batches = build_semantic_review_batches(
            source,
            repaired_cues,
            source_language=_source_language(job, source),
            target_language=resolved.target_language,
            batch_size=resolved.batch_size,
            policy=resolved.translation_policy,
        )
        final_results = _agent_results(
            "final_results", response.get("final_results"), final_batches
        )
        final_issues = _merge_issues(
            _validate_unique_issues([issue for value in final_results.values() for issue in value]),
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
        _write_json(repaired_path, [asdict(cue) for cue in repaired_cues])
        _add_artifact(job, "translation_repaired_cues", repaired_path, event_sink)
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
    if final_issues:
        _set_stage(
            job,
            StageName.TRANSLATE,
            StageStatus.AWAITING_INPUT,
            event_sink,
            extras={"phase": "semantic_review"},
        )
        from xvt.pipeline.revision import ensure_translation_review_input

        paths.append(ensure_translation_review_input(job_file, event_sink=event_sink))
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

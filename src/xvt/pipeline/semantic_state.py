"""语义复查候选与报告的只读状态查询（零副作用叶子模块）。

这些查询只读取 checksum 有效的 artifact、重算批次与指纹并解析报告，不写盘、不登记
artifact、不改 stage，所以可以安全地下移到 pipeline 包的底层：它在包内只依赖
`common`，不依赖 `review` / `revision` / `semantic`。

下移消掉的是 R1.3 遗留的两个加载期循环（原先靠函数体内惰性 import 规避）：

* `review` ⇄ `semantic`：`review.translation_review_acknowledged` /
  `acknowledge_translation_review` 需要 `semantic_review_target`，而
  `semantic.semantic_review_approved` 需要 `review.translation_review_acknowledged`。
* `revision` ⇄ `semantic`：`revision._target_kinds` 需要 `semantic_review_target`，而
  `semantic` 的三处等待分支需要 `revision.ensure_semantic_review_inputs`。

现在 `review` / `revision` 顶层 import 本模块，`semantic` 顶层 import
`review` / `revision` / 本模块，依赖方向全部向下，包内已无函数级 import 环。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from xvt.config import DEFAULT_BATCH_SIZE, DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT_SECONDS
from xvt.models import Cue, Job
from xvt.pipeline.common import checksum, load_cues, resolved_translation_policy
from xvt.recovery import valid_artifact_path
from xvt.translate import (
    SEMANTIC_GROUP_MAX_CUES,
    SEMANTIC_GROUPING_VERSION,
    SEMANTIC_REPAIR_CATEGORIES,
    SEMANTIC_REVIEW_CATEGORIES,
    SemanticIssue,
    SemanticRepairBatch,
    SemanticReviewBatch,
    SemanticSuggestedItem,
    apply_semantic_repairs,
    build_semantic_groups,
    build_semantic_repair_batches,
    build_semantic_review_batches,
    parse_semantic_repair_response,
    parse_semantic_review_response,
    translation_policy_from_dict,
)
from xvt.translation_provider import SEMANTIC_REVIEW_PROMPT_VERSION
from xvt.workflow_config import WorkflowConfig, get_workflow_config

HUMAN_REVISION_SCOPE_VERSION = 1


def config_for_job(job: Job, config: WorkflowConfig | None) -> WorkflowConfig:
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
        batch_size=int(job.config.get("batch_size", DEFAULT_BATCH_SIZE)),
        max_retries=int(job.config.get("max_retries", DEFAULT_MAX_RETRIES)),
        timeout_seconds=float(job.config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
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


def source_language(job: Job, source: list[Cue]) -> str:
    return next(
        (cue.source_language for cue in source if cue.source_language),
        str(job.config.get("source_language", "auto")),
    )


def build_review_batches(
    job: Job,
    candidate_path: Path,
    config: WorkflowConfig,
    *,
    review_cue_ids: frozenset[int] | None = None,
) -> list[SemanticReviewBatch]:
    source_path = valid_artifact_path(job, "source_cues")
    if source_path is None or config.target_language is None:
        raise ValueError("semantic review requires valid source cues and a target language")
    source = load_cues(source_path)
    return build_semantic_review_batches(
        source,
        load_cues(candidate_path),
        source_language=source_language(job, source),
        target_language=config.target_language,
        batch_size=config.batch_size,
        policy=resolved_translation_policy(job, config),
        review_cue_ids=review_cue_ids,
    )


def _issue_from_dict(raw: Any) -> SemanticIssue:
    if not isinstance(raw, dict):
        raise ValueError("human revision base issue must be an object")
    category = raw.get("category")
    cue_ids = raw.get("cue_ids")
    severity = raw.get("severity")
    message = raw.get("message")
    suggested = raw.get("suggested_items")
    if (
        category not in SEMANTIC_REVIEW_CATEGORIES
        or not isinstance(cue_ids, list)
        or not cue_ids
        or any(not isinstance(value, int) or isinstance(value, bool) for value in cue_ids)
        or len(set(cue_ids)) != len(cue_ids)
        or severity != "review"
        or not isinstance(message, str)
        or not message.strip()
        or not isinstance(suggested, list)
    ):
        raise ValueError("human revision base issue is invalid")
    suggestions: list[SemanticSuggestedItem] = []
    for item in suggested:
        cue_id = item.get("id") if isinstance(item, dict) else None
        text = item.get("text") if isinstance(item, dict) else None
        if (
            not isinstance(cue_id, int)
            or isinstance(cue_id, bool)
            or cue_id not in cue_ids
            or not isinstance(text, str)
            or not text.strip()
        ):
            raise ValueError("human revision base issue suggestion is invalid")
        suggestions.append(SemanticSuggestedItem(cue_id, text.strip()))
    return SemanticIssue(
        category=category,
        cue_ids=tuple(sorted(cue_ids)),
        severity="review",
        message=message.strip(),
        suggested_items=tuple(suggestions),
    )


def human_revision_scope(
    job: Job,
    candidate_path: Path,
    source: list[Cue],
    config: WorkflowConfig,
) -> tuple[frozenset[int] | None, tuple[SemanticIssue, ...], dict[str, Any] | None]:
    if valid_artifact_path(job, "translation_reviewed_cues") != candidate_path:
        return None, (), None
    revision_path = valid_artifact_path(job, "translation_review_revision")
    if revision_path is None:
        return None, (), None
    try:
        revision = json.loads(revision_path.read_text(encoding="utf-8"))
        changed_raw = revision.get("changed_ids")
        base_raw = revision.get("base_issues")
        if (
            revision.get("reviewed_candidate_checksum") != checksum(candidate_path)
            or revision.get("semantic_grouping_version") != SEMANTIC_GROUPING_VERSION
            or not isinstance(changed_raw, list)
            or not changed_raw
            or any(not isinstance(value, int) or isinstance(value, bool) for value in changed_raw)
            or len(set(changed_raw)) != len(changed_raw)
            or not isinstance(base_raw, list)
        ):
            raise ValueError
        base_issues = validate_unique_issues([_issue_from_dict(item) for item in base_raw])
        groups = build_semantic_groups(
            source,
            max_cues=min(SEMANTIC_GROUP_MAX_CUES, config.batch_size),
        )
        known_ids = {cue_id for group in groups for cue_id in group}
        changed_ids = set(changed_raw)
        if not changed_ids <= known_ids:
            raise ValueError
        selected_groups = {index for index, group in enumerate(groups) if set(group) & changed_ids}
        expanded = True
        while expanded:
            selected_ids = {cue_id for index in selected_groups for cue_id in groups[index]}
            before = len(selected_groups)
            for issue in base_issues:
                if set(issue.cue_ids) & selected_ids:
                    selected_groups.update(
                        index
                        for index, group in enumerate(groups)
                        if set(group) & set(issue.cue_ids)
                    )
            expanded = len(selected_groups) != before
        selected_ids = frozenset(cue_id for index in selected_groups for cue_id in groups[index])
        preserved = tuple(issue for issue in base_issues if not set(issue.cue_ids) & selected_ids)
        scope = {
            "version": HUMAN_REVISION_SCOPE_VERSION,
            "changed_ids": sorted(changed_ids),
            "reviewed_ids": sorted(selected_ids),
            "parent_report_checksum": revision.get("source_report_checksum"),
            "preserved_issues": serialized_issues(preserved),
        }
        return selected_ids, preserved, scope
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None, (), None


def canonical_issue_key(issue: SemanticIssue) -> tuple[str, tuple[int, ...]]:
    return issue.category, issue.cue_ids


def validate_unique_issues(issues: list[SemanticIssue]) -> tuple[SemanticIssue, ...]:
    seen: set[tuple[str, tuple[int, ...]]] = set()
    for issue in issues:
        key = canonical_issue_key(issue)
        if key in seen:
            raise ValueError("semantic review contains duplicate issues across batches")
        seen.add(key)
    return tuple(issues)


def agent_results(
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
    validate_unique_issues([issue for value in results.values() for issue in value])
    return results


def serialized_batches(
    batches: list[SemanticReviewBatch] | list[SemanticRepairBatch],
) -> list[dict[str, Any]]:
    return json.loads(json.dumps([batch.to_dict() for batch in batches], ensure_ascii=False))


def serialized_issues(issues: tuple[SemanticIssue, ...]) -> list[dict[str, Any]]:
    return json.loads(json.dumps([issue.to_dict() for issue in issues], ensure_ascii=False))


def load_repair_checkpoint(
    job: Job,
    batches: list[SemanticRepairBatch],
    fingerprint: str,
) -> tuple[bool, set[int], dict[int, str]]:
    path = valid_artifact_path(job, "translation_semantic_repair")
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


def report_status(path: Path) -> tuple[str, tuple[dict[str, Any], ...]]:
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
        config = config_for_job(job, None)
        source_path = valid_artifact_path(job, "source_cues")
        if source_path is None:
            return False
        review_cue_ids, _preserved, review_scope = human_revision_scope(
            job,
            candidate_path,
            load_cues(source_path),
            config,
        )
        batches = build_review_batches(
            job,
            candidate_path,
            config,
            review_cue_ids=review_cue_ids,
        )
        data = json.loads(report_path.read_text(encoding="utf-8"))
        results = data.get("results")
        completed = data.get("completed_batches")
        if (
            data.get("prompt_version") != SEMANTIC_REVIEW_PROMPT_VERSION
            or data.get("semantic_grouping_version") != SEMANTIC_GROUPING_VERSION
            or data.get("batches") != serialized_batches(batches)
            or data.get("review_scope") != review_scope
            or completed != [batch.id for batch in batches]
            or not isinstance(results, list)
        ):
            return False
        parsed = agent_results("report results", results, batches)
        parsed_issues = validate_unique_issues(
            [issue for value in parsed.values() for issue in value]
        )
        status, issues = report_status(report_path)
        issue_keys = {
            (item.get("category"), tuple(sorted(item.get("cue_ids", ())))) for item in issues
        }
        parsed_keys = {canonical_issue_key(issue) for issue in parsed_issues}
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
        config = config_for_job(job, None)
        source_path = valid_artifact_path(job, "source_cues")
        if source_path is None or config.target_language is None:
            return False
        report = json.loads(initial_report.read_text(encoding="utf-8"))
        raw_issues = report.get("issues")
        if not isinstance(raw_issues, list):
            return False
        review_batches = build_review_batches(job, polished_path, config)
        issues = agent_results(
            "initial report",
            report.get("results"),
            review_batches,
        )
        all_issues = validate_unique_issues([issue for value in issues.values() for issue in value])
        source = load_cues(source_path)
        candidate = load_cues(polished_path)
        repair_batches = build_semantic_repair_batches(
            source,
            candidate,
            all_issues,
            source_language=source_language(job, source),
            target_language=config.target_language,
            policy=resolved_translation_policy(job, config),
        )
        repair_data = json.loads(repair_path.read_text(encoding="utf-8"))
        if repair_data.get("batches") != serialized_batches(repair_batches):
            return False
        fingerprint = repair_data.get("fingerprint")
        if not isinstance(fingerprint, str):
            return False
        reusable, completed, repaired = load_repair_checkpoint(job, repair_batches, fingerprint)
        expected_ids = {item.id for batch in repair_batches for item in batch.items}
        return bool(
            reusable
            and completed == {batch.id for batch in repair_batches}
            and load_cues(repaired_path)
            == apply_semantic_repairs(
                candidate,
                repaired,
                expected_ids,
                target_language=config.target_language,
            )
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
        _, issues = report_status(initial)
        if any(item.get("category") in SEMANTIC_REPAIR_CATEGORIES for item in issues):
            return None
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return initial, polished


def current_semantic_candidate(job: Job) -> Path:
    target = semantic_review_target(job)
    return target[1] if target is not None else _candidate_path(job)

"""人工修订语义校对后的译文，不改变字幕时间轴。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from xvt.artifacts import ArtifactKind, downstream_of
from xvt.candidate_qa import overflow_limits, translation_candidate_qa_issues
from xvt.errors import UserInputError
from xvt.events import EventSink, EventType, create_event, job_event_fields
from xvt.job_store import get_job_directory, get_job_file, load_job, save_job, utc_now
from xvt.models import Artifact, Cue, Job, StageName, StageStatus
from xvt.pipeline.common import (
    SUBTITLE_REVIEW_REQUIRED_MESSAGE,
    artifact_checksum_for,
    checksum,
    load_cues,
    load_user_response_json,
    translation_qa_context,
    write_json,
)
from xvt.pipeline.semantic_state import semantic_review_target
from xvt.qa import SubtitleQaReport, check_subtitles
from xvt.recovery import valid_artifact_path
from xvt.segment import TARGET_WRAP_MAX_LINES
from xvt.subtitles import load_review_srt, save_review_srt, save_subtitles
from xvt.translation_provider import SEMANTIC_REVIEW_PROMPT_VERSION
from xvt.workflow_config import (
    WorkflowConfig,
    clear_burn_fingerprint,
    get_workflow_config,
    resolve_target_language,
)

# 修订模板写入 / 校验共用的 task 值，也是 CLI `xvt apply` task 路由表的真实来源。
SOURCE_REVIEW_TASK = "source_review_fixes"
TRANSLATION_REVIEW_TASK = "translation_review_fixes"

# 人工修订模板的文件名主干。SRT 载体没有 `revision_fingerprint` 字段，fingerprint 全靠
# 文件名承载，所以这些命名同时是「写模板」和「校验回填」的唯一来源；CLI 统一入口识别
# 修订 SRT 用的形状正则也从这些常量派生，不再各自硬编码。
SOURCE_REVIEW_STEM_PREFIX = "source-review-"
TRANSLATION_REVIEW_STEM_PREFIX = "translation-review-"
REVISION_FINGERPRINT_LENGTH = 12


def source_review_stem(fingerprint: str) -> str:
    """源文修订模板（JSON 与 SRT 共用）的文件名主干。"""

    return f"{SOURCE_REVIEW_STEM_PREFIX}{fingerprint[:REVISION_FINGERPRINT_LENGTH]}"


def translation_review_stem(fingerprint: str) -> str:
    """译文修订模板（JSON 与 SRT 共用）的文件名主干。"""

    return f"{TRANSLATION_REVIEW_STEM_PREFIX}{fingerprint[:REVISION_FINGERPRINT_LENGTH]}"


def _source_revision_fingerprint(job: Job, cue_ids: list[int]) -> str:
    source_checksum = artifact_checksum_for(job, "source_cues")
    if source_checksum is None:
        raise UserInputError("source review requires valid source cues")
    payload = {
        "job_id": job.id,
        "source_cues_checksum": source_checksum,
        "cue_ids": sorted(cue_ids),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def source_review_supported(job: Job) -> bool:
    workflow = get_workflow_config(job)
    return not (
        (workflow is not None and workflow.translation_subtitles)
        or valid_artifact_path(job, "input_translation_subtitles") is not None
    )


def _require_source_review_supported(job: Job) -> None:
    if not source_review_supported(job):
        raise UserInputError(
            "cannot revise source cues while reusing imported translation subtitles; "
            "provide a matching translated subtitle file in a new job"
        )


def source_review_input_path(job: Job, cue_ids: list[int]) -> Path | None:
    try:
        fingerprint = _source_revision_fingerprint(job, cue_ids)
    except UserInputError:
        return None
    path = get_job_directory(job) / "review" / f"{source_review_stem(fingerprint)}.json"
    return path if path.is_file() else None


def prepare_source_review(
    job_file: str | Path,
    cue_ids: list[int],
    *,
    event_sink: EventSink | None = None,
) -> Path:
    job = load_job(job_file)
    _require_source_review_supported(job)
    source_path = valid_artifact_path(job, "source_cues")
    if source_path is None:
        raise UserInputError("source review requires valid source cues")
    if not cue_ids or any(isinstance(cue_id, bool) or cue_id <= 0 for cue_id in cue_ids):
        raise UserInputError("source review requires positive cue ids")
    if len(set(cue_ids)) != len(cue_ids):
        raise UserInputError("source review cue ids must not contain duplicates")

    cues = load_cues(source_path)
    by_id = {cue.id: cue for cue in cues}
    if len(by_id) != len(cues):
        raise UserInputError("source cues contain duplicate cue ids")
    unknown = sorted(set(cue_ids) - by_id.keys())
    if unknown:
        raise UserInputError(f"source review contains unknown cue ids: {unknown}")

    fingerprint = _source_revision_fingerprint(job, cue_ids)
    path = get_job_directory(job) / "review" / f"{source_review_stem(fingerprint)}.json"
    if path.is_file():
        srt_path = path.with_suffix(".srt")
        if not srt_path.is_file() or srt_path.is_symlink():
            save_review_srt(
                [by_id[cue_id] for cue_id in sorted(cue_ids)],
                srt_path,
                mode="source",
            )
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(
        path,
        {
            "task": SOURCE_REVIEW_TASK,
            "revision_fingerprint": fingerprint,
            "scope": "source",
            "items": [
                {"id": cue_id, "text": by_id[cue_id].source_text} for cue_id in sorted(cue_ids)
            ],
        },
    )
    save_review_srt(
        [by_id[cue_id] for cue_id in sorted(cue_ids)],
        path.with_suffix(".srt"),
        mode="source",
    )
    if event_sink:
        event_sink(
            create_event(
                EventType.SOURCE_REVIEW_INPUT_READY,
                **job_event_fields(job.id, get_job_file(job)),
                path=str(path),
                scope="source",
                item_count=len(cue_ids),
            )
        )
    return path


def _source_srt_review_changes(
    response_file: Path,
    job: Job,
    source_cues: list[Cue],
) -> tuple[str, dict[int, str]]:
    try:
        entries = load_review_srt(response_file)
    except (OSError, ValueError) as error:
        raise UserInputError(f"cannot read source review response: {error}") from error
    if not entries:
        raise UserInputError("source review response items must be a non-empty array")

    ids_by_timing: dict[tuple[int, int], list[int]] = {}
    for cue in source_cues:
        ids_by_timing.setdefault((cue.start_ms, cue.end_ms), []).append(cue.id)
    changes: dict[int, str] = {}
    for marker_id, start_ms, end_ms, text in entries:
        matching_ids = ids_by_timing.get((start_ms, end_ms), [])
        if not matching_ids:
            raise UserInputError(
                "source review SRT changed cue timing or added a cue; "
                "revision may only change source text"
            )
        if marker_id is not None:
            if marker_id not in matching_ids:
                raise UserInputError(
                    f"source review SRT cue id marker #{marker_id} does not match its timing"
                )
            cue_id = marker_id
        elif len(matching_ids) == 1:
            cue_id = matching_ids[0]
        else:
            raise UserInputError(
                "cannot map the review SRT back to a cue id because source cues share timing"
            )
        if cue_id in changes or not text.strip():
            raise UserInputError("source review response contains an invalid or duplicate item")
        changes[cue_id] = text.strip()

    expected_fingerprint = _source_revision_fingerprint(job, list(changes))
    expected_stem = source_review_stem(expected_fingerprint)
    if response_file.stem != expected_stem:
        raise UserInputError(
            "source review response is stale for the current source cues "
            f"(expected a file named {expected_stem}.srt)"
        )
    return expected_fingerprint, changes


def _load_previous_source_selection(job: Job) -> dict[str, Any] | None:
    path = valid_artifact_path(job, "source_selection")
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    fields = (
        "strategy",
        "candidate_kind",
        "selected_kind",
        "reason",
        "reference_allowed",
        "alignment_confidence",
        "source_cues_checksum",
    )
    return {field: data[field] for field in fields if field in data}


def _source_qa_context(job: Job) -> dict[str, int | None]:
    media_path = valid_artifact_path(job, "media_info")
    if media_path is None:
        return {}
    try:
        data = json.loads(media_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "media_duration_ms": data.get("duration_ms"),
        "video_width": data.get("width"),
        "video_height": data.get("height"),
    }


@dataclass
class _SourceRevision:
    job: Job
    workflow: WorkflowConfig | None
    fingerprint: str
    reviewed: list[Cue]
    changed_ids: set[int]
    qa: SubtitleQaReport


def _prepare_source_revision(
    job_file: str | Path,
    response_file: str | Path,
) -> _SourceRevision:
    """只读地走完源文修订的全部校验与计算，含修订后的源字幕 QA。

    切点取在 `apply_source_review_fixes` 第一次 `write_json` 之前：这里只读 Job 与
    响应文件、算出修订后的 cue 与 QA 报告，不写盘、不改 stage、不登记 artifact。
    `preflight_source_review_fixes` 与正式 apply 共用这一份，避免预检与实际行为分叉。
    """

    job = load_job(job_file)
    workflow = get_workflow_config(job)
    _require_source_review_supported(job)
    source_path = valid_artifact_path(job, "source_cues")
    if source_path is None:
        raise UserInputError("source review requires valid source cues")
    source_cues = load_cues(source_path)
    by_id = {cue.id: cue for cue in source_cues}
    if len(by_id) != len(source_cues):
        raise UserInputError("source cues contain duplicate cue ids")
    response_path = Path(response_file)
    if response_path.suffix.lower() == ".srt":
        expected_fingerprint, changes = _source_srt_review_changes(
            response_path,
            job,
            source_cues,
        )
    else:
        response = load_user_response_json(response_path, what="source review response")
        if not isinstance(response, dict):
            raise UserInputError("source review response must be a JSON object")
        if response.get("task") != SOURCE_REVIEW_TASK or response.get("scope") != "source":
            raise UserInputError("source review response task or scope is invalid")
        items = response.get("items")
        if not isinstance(items, list) or not items:
            raise UserInputError("source review response items must be a non-empty array")

        changes = {}
        for item in items:
            if not isinstance(item, dict) or set(item) != {"id", "text"}:
                raise UserInputError("source review items may contain only id and text")
            cue_id = item.get("id")
            text = item.get("text")
            if (
                not isinstance(cue_id, int)
                or isinstance(cue_id, bool)
                or cue_id in changes
                or not isinstance(text, str)
                or not text.strip()
            ):
                raise UserInputError("source review response contains an invalid or duplicate item")
            changes[cue_id] = text.strip()

        expected_fingerprint = _source_revision_fingerprint(job, list(changes))
        if response.get("revision_fingerprint") != expected_fingerprint:
            raise UserInputError("source review response is stale for the current source cues")
    unknown = sorted(changes.keys() - by_id.keys())
    if unknown:
        raise UserInputError(f"source review contains unknown cue ids: {unknown}")
    changed_ids = {cue_id for cue_id, text in changes.items() if by_id[cue_id].source_text != text}
    if not changed_ids:
        raise UserInputError("source review response does not change any cue text")
    reviewed = [
        replace(cue, source_text=changes.get(cue.id, cue.source_text)) for cue in source_cues
    ]
    qa = check_subtitles(reviewed, **_source_qa_context(job))
    return _SourceRevision(job, workflow, expected_fingerprint, reviewed, changed_ids, qa)


def _source_revision_preflight_result(revision: _SourceRevision) -> dict[str, Any]:
    """预检结论。

    **`valid` 的语义与译文侧不同，不要混用。** 译文侧 `valid: false` 表示 apply 会被
    拒绝、一个字节都不写；源文侧 apply 在 QA 失败时**照旧写盘并失效全部下游**，只是
    不产出 `source_srt`、给 `SUBTITLE_QA` 记 error，最后才抛 `RuntimeError`
    （`apply_source_review_fixes` 末尾，事件都已发出）。所以这里的 `valid: false` 读作
    「这份修订存得进去，但存完 Job 就是坏的、译文与下游已经白白失效」——**预检要帮
    用户避免的正是这件事**，因为那一步不可逆、重新翻译要再付一次钱。

    `blockers` 只收 error 级：`needs_review` 不会让 apply 抛错，归在 `qa_status` 里由
    调用方自行判断，不与真正的失败混为一谈。
    """

    blockers = [asdict(issue) for issue in revision.qa.issues if issue.severity == "error"]
    return {
        "task": "source_review_preflight",
        "valid": revision.qa.status != "failed",
        "changed_ids": sorted(revision.changed_ids),
        "qa_status": revision.qa.status,
        "blockers": blockers,
        "qa": asdict(revision.qa),
    }


def preflight_source_review_fixes(
    job_file: str | Path,
    response_file: str | Path,
) -> dict[str, Any]:
    """只读校验源文修订与修订后的源字幕 QA；不写 Job、不改 stage、不调用模型。"""

    return _source_revision_preflight_result(_prepare_source_revision(job_file, response_file))


def apply_source_review_fixes(
    job_file: str | Path,
    response_file: str | Path,
    *,
    event_sink: EventSink | None = None,
) -> tuple[Path, ...]:
    # 校验与计算全部走 `_prepare_source_revision`，与 `preflight_source_review_fixes`
    # 共用同一份——预检与实际写入各算一遍迟早会分叉，那会让 `--check` 报的结论与
    # apply 的真实结果对不上，比没有预检更糟。
    #
    # 刻意**不**在这里因 QA 失败提前返回：源文修订的既有契约是「照旧写盘、失效下游、
    # 最后抛 RuntimeError」（本函数末尾，`tests/test_pipeline_revision.py` 钉住了它），
    # 改成提前拒绝会动到不该动的行为。预检的价值在于让用户事先知道这一步会把 Job
    # 带到哪里，而不是改变这一步本身。
    revision = _prepare_source_revision(job_file, response_file)
    job = revision.job
    workflow = revision.workflow
    expected_fingerprint = revision.fingerprint
    reviewed = revision.reviewed
    changed_ids = revision.changed_ids

    artifacts_dir = get_job_directory(job) / "artifacts"
    suffix = expected_fingerprint[:12]
    revision_path = artifacts_dir / f"source-review-revision-{suffix}.json"
    reviewed_path = artifacts_dir / f"source-reviewed-cues-{suffix}.json"
    qa_path = artifacts_dir / f"source-review-qa-{suffix}.json"
    source_srt_path = artifacts_dir / f"source-reviewed-{suffix}.srt"
    selection_path = artifacts_dir / f"source-selection-reviewed-{suffix}.json"
    write_json(reviewed_path, [asdict(cue) for cue in reviewed])
    reviewed_checksum = checksum(reviewed_path)
    write_json(
        revision_path,
        {
            "task": SOURCE_REVIEW_TASK,
            "revision_fingerprint": expected_fingerprint,
            "scope": "source",
            "previous_source_cues_checksum": artifact_checksum_for(job, "source_cues"),
            "reviewed_source_cues_checksum": reviewed_checksum,
            "changed_ids": sorted(changed_ids),
            "items": [{"id": cue.id, "text": cue.source_text} for cue in reviewed],
        },
    )
    # 复用预检算出的那份，不重算：两次结果必须是同一个，重算只是给分叉留口子。
    report = revision.qa
    write_json(qa_path, asdict(report))
    previous_selection = _load_previous_source_selection(job)
    write_json(
        selection_path,
        {
            "selected_kind": "human_reviewed",
            "reason": "human_source_revision",
            "source_cues_checksum": reviewed_checksum,
            "previous_selection": previous_selection,
        },
    )

    invalidated_kinds = downstream_of(ArtifactKind.SOURCE_CUES) | {
        ArtifactKind.SOURCE_CUES,
        ArtifactKind.SOURCE_REVIEW_REVISION,
        ArtifactKind.TRANSLATION_RESEGMENTATION,
        ArtifactKind.TRANSLATION_REVIEW_REVISION,
    }
    invalidated = [artifact for artifact in job.artifacts if artifact.kind in invalidated_kinds]
    replacements = [
        Artifact("source_review_revision", str(revision_path), checksum(revision_path)),
        Artifact("source_cues", str(reviewed_path), reviewed_checksum),
        Artifact("source_selection", str(selection_path), checksum(selection_path)),
        Artifact("subtitle_qa", str(qa_path), checksum(qa_path)),
    ]
    qa_message = None
    if report.status not in {"failed", "needs_review"}:
        save_subtitles(reviewed, source_srt_path)
        replacements.append(Artifact("source_srt", str(source_srt_path), checksum(source_srt_path)))
    elif report.status == "needs_review":
        qa_message = SUBTITLE_REVIEW_REQUIRED_MESSAGE
    else:
        qa_message = "subtitle QA failed; inspect the QA artifact and repair subtitles"

    replacement_kinds = {artifact.kind for artifact in replacements}
    job.artifacts = [
        artifact
        for artifact in job.artifacts
        if artifact.kind not in invalidated_kinds | replacement_kinds
    ]
    job.artifacts.extend(replacements)
    job.config.pop("translation_review_acknowledgment", None)
    job.config.pop("subtitle_review_acknowledgment", None)
    clear_burn_fingerprint(job)
    now = utc_now()
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
    translate_record = job.stages[StageName.TRANSLATE]
    if resolve_target_language(job) is None:
        translate_record.status = StageStatus.SKIPPED
        translate_record.completed_at = now
    burn_requested = bool(workflow is not None and workflow.burn)
    if not burn_requested:
        for stage in (StageName.BURN, StageName.MEDIA_QA):
            record = job.stages[stage]
            record.status = StageStatus.SKIPPED
            record.completed_at = now
    qa_record = job.stages[StageName.SUBTITLE_QA]
    qa_record.status = (
        StageStatus.SUCCEEDED
        if qa_message is None
        else StageStatus.AWAITING_INPUT
        if report.status == "needs_review"
        else StageStatus.FAILED
    )
    qa_record.completed_at = None if report.status == "needs_review" else now
    qa_record.error = qa_message
    if qa_message is None:
        render_record = job.stages[StageName.RENDER]
        render_record.status = StageStatus.SUCCEEDED
        render_record.completed_at = now
    save_job(job)

    if event_sink:
        for artifact in invalidated:
            event_sink(
                create_event(
                    EventType.ARTIFACT_INVALIDATED,
                    **job_event_fields(job.id, get_job_file(job)),
                    kind=artifact.kind,
                    path=artifact.path,
                )
            )
        for artifact in replacements:
            event_sink(
                create_event(
                    EventType.ARTIFACT_CREATED,
                    **job_event_fields(job.id, get_job_file(job)),
                    kind=artifact.kind,
                    path=artifact.path,
                )
            )
        event_sink(
            create_event(
                EventType.SOURCE_REVIEW_REVISED,
                **job_event_fields(job.id, get_job_file(job)),
                scope="source",
                item_count=len(changed_ids),
                checksum=reviewed_checksum,
                qa_status=report.status,
            )
        )
    if report.status == "failed":
        raise RuntimeError(qa_message)
    return tuple(Path(artifact.path) for artifact in replacements)


def _target_kinds(job: Job) -> tuple[str, str, Path, Path] | None:
    target = semantic_review_target(job)
    if target is None:
        return None
    report, candidate = target
    for report_kind, candidate_kind in (
        ("translation_semantic_qa_reviewed", "translation_reviewed_cues"),
        ("translation_semantic_qa_final", "translation_repaired_cues"),
        ("translation_semantic_qa", "translation_polished_cues"),
    ):
        if (
            valid_artifact_path(job, report_kind) == report
            and valid_artifact_path(job, candidate_kind) == candidate
        ):
            return report_kind, candidate_kind, report, candidate
    return None


def _issue_set(report: dict[str, Any]) -> list[dict[str, Any]]:
    issues = report.get("issues")
    if report.get("status") == "pass" and issues == []:
        return []
    if report.get("status") != "needs_review" or not isinstance(issues, list) or not issues:
        raise UserInputError("semantic review report does not require translation revision")
    normalized: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            raise UserInputError("semantic review report contains an invalid issue")
        category = issue.get("category")
        cue_ids = issue.get("cue_ids")
        severity = issue.get("severity")
        suggested_items = issue.get("suggested_items", [])
        if (
            not isinstance(category, str)
            or severity != "review"
            or not isinstance(cue_ids, list)
            or not cue_ids
            or any(not isinstance(cue_id, int) or isinstance(cue_id, bool) for cue_id in cue_ids)
            or not isinstance(suggested_items, list)
        ):
            raise UserInputError("semantic review report contains an invalid issue")
        normalized_suggestions: list[dict[str, Any]] = []
        for item in suggested_items:
            cue_id = item.get("id") if isinstance(item, dict) else None
            text = item.get("text") if isinstance(item, dict) else None
            if not isinstance(cue_id, int) or isinstance(cue_id, bool) or not isinstance(text, str):
                raise UserInputError("semantic review report contains an invalid suggestion")
            normalized_suggestions.append({"id": cue_id, "text": text})
        normalized.append(
            {
                "category": category,
                "cue_ids": sorted(cue_ids),
                "severity": severity,
                "message": str(issue.get("message", "")),
                "suggested_items": sorted(
                    normalized_suggestions, key=lambda item: (item["id"], item["text"])
                ),
            }
        )
    return sorted(normalized, key=lambda item: (item["category"], item["cue_ids"]))


def _candidate_qa_issue_set(job: Job, candidate_path: Path) -> list[dict[str, Any]]:
    issues = translation_candidate_qa_issues(job, candidate_path)
    return sorted(
        [dict(issue) for issue in issues or ()],
        key=lambda item: (item.get("cue_id", 0), item.get("code", "")),
    )


def _describe_unchanged_qa_cues(qa_issues: list[dict[str, Any]]) -> str:
    """把仍待处理的 QA 阻断项按 cue 汇总成人类可读描述，供零改动报错提示。"""

    grouped: dict[int, list[str]] = {}
    for issue in qa_issues:
        cue_id = issue.get("cue_id")
        if not isinstance(cue_id, int) or isinstance(cue_id, bool):
            continue
        message = issue.get("message")
        grouped.setdefault(cue_id, []).append(message if isinstance(message, str) else "")
    if not grouped:
        return ""
    details = ", ".join(
        f"{cue_id} ({'; '.join(messages)})" for cue_id, messages in sorted(grouped.items())
    )
    return f"{len(grouped)} QA-blocked cue(s) still fail candidate QA: {details}"


def _semantic_review_issues(job: Job) -> list[dict[str, Any]]:
    """语义校对报告里规范化后的 issue 列表；报告缺失或不可解析时返回空。

    `source_suspect_cue_ids` 与 `semantic_review_issue_summaries` 共用这一段解析——
    两者都要读同一份报告，各写一遍迟早会在 except 口径或 `_issue_set` 的调用方式上
    分叉。这里的 except 刻意保持既有口径（含 `UserInputError`：报告不要求修订时
    `_issue_set` 会抛它，对调用方而言等同于「没有可报的 issue」），不要放宽。
    """

    target = _target_kinds(job)
    if target is None:
        return []
    _report_kind, _candidate_kind, report_path, _candidate_path = target
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        return _issue_set(report)
    except (OSError, TypeError, ValueError, json.JSONDecodeError, UserInputError):
        return []


def source_suspect_cue_ids(job: Job) -> tuple[int, ...]:
    return tuple(
        sorted(
            {
                cue_id
                for issue in _semantic_review_issues(job)
                if issue["category"] == "source_suspect"
                for cue_id in issue["cue_ids"]
            }
        )
    )


def semantic_review_issue_summaries(job: Job) -> tuple[tuple[tuple[int, ...], str], ...]:
    """每条语义校对 issue 的 `(cue_ids, message)`，顺序与报告一致（`_issue_set` 已排序）。

    只读，不写盘、不改 stage。给 CLI 的人读摘要用：报告里的 message 定位到 cue、带
    推理过程，而终态此前只打一句「语义校对发现源文可能有误」加一串路径，用户必须自己
    打开 JSON 才知道是哪几句、错在哪。

    message 原样透出，**不翻译也不改写**：模型产出的 issue 语言不固定（实测同一份报告
    里两条英文一条中文），那是 prompt 侧的问题（发现清单 E6），在这里动手只会让摘要与
    报告对不上。`_issue_set` 已把缺失的 message 规范成空串，调用方据此只打 cue 区间。
    """

    return tuple(
        (tuple(issue["cue_ids"]), str(issue.get("message") or ""))
        for issue in _semantic_review_issues(job)
    )


def ensure_source_suspect_review_input(
    job_file: str | Path,
    *,
    event_sink: EventSink | None = None,
) -> Path | None:
    job = load_job(job_file)
    cue_ids = source_suspect_cue_ids(job)
    if not cue_ids or not source_review_supported(job):
        return None
    existing = source_review_input_path(job, list(cue_ids))
    if existing is not None:
        return existing
    return prepare_source_review(job_file, list(cue_ids), event_sink=event_sink)


def _revision_fingerprint(
    job: Job,
    report_kind: str,
    candidate_kind: str,
    report: dict[str, Any],
) -> str:
    candidate_path = valid_artifact_path(job, candidate_kind)
    if candidate_path is None:
        raise UserInputError("semantic review candidate is no longer valid")
    payload = {
        "job_id": job.id,
        "source_cues_checksum": artifact_checksum_for(job, "source_cues"),
        "candidate_kind": candidate_kind,
        "candidate_cues_checksum": artifact_checksum_for(job, candidate_kind),
        "report_kind": report_kind,
        "report_checksum": artifact_checksum_for(job, report_kind),
        "prompt_version": report.get("prompt_version", SEMANTIC_REVIEW_PROMPT_VERSION),
        "issues": _issue_set(report),
        "candidate_qa_checksum": artifact_checksum_for(job, "translation_candidate_qa"),
        "candidate_qa_issues": _candidate_qa_issue_set(job, candidate_path),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _review_item_ids(template_path: Path) -> set[int]:
    """读出既有 JSON 模板覆盖的 cue ID 集合，供补生成 SRT 时对齐同一范围。"""

    try:
        template = json.loads(template_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    items = template.get("items") if isinstance(template, dict) else None
    if not isinstance(items, list):
        return set()
    return {
        item["id"]
        for item in items
        if isinstance(item, dict)
        and isinstance(item.get("id"), int)
        and not isinstance(item.get("id"), bool)
    }


def translation_review_input_path(job: Job) -> Path | None:
    target = _target_kinds(job)
    if target is None:
        return None
    report_kind, candidate_kind, report_path, _candidate_path = target
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if not _issue_set(report) and not _candidate_qa_issue_set(job, _candidate_path):
            return None
        fingerprint = _revision_fingerprint(job, report_kind, candidate_kind, report)
        review_dir = get_job_directory(job) / "review"
        path = review_dir / f"{translation_review_stem(fingerprint)}.json"
        return path if path.is_file() else None
    except (OSError, TypeError, ValueError, json.JSONDecodeError, UserInputError):
        return None
    return None


def _translation_review_items(
    cues: list[Cue],
    affected: set[int],
    qa_issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """给译文修订模板的每条 cue 附上只读标注，帮人看清「哪条必改、要缩到多少」。

    ``source_text``/``must_fix``/``qa`` 只是标注，回填端一律忽略；可编辑的 ``text``
    放在最后，紧挨着这些标注。顺序保持 cue id 升序，与同名 SRT 通道逐条对得上。

    宽度标注里的 ``limit`` 是**每行**上限（``segment.wrap_target_lines`` 之后 QA 量的
    就是最宽那行），所以同时给出 ``max_lines``：只报上限会让人以为要把整句缩进一行。
    换行只作用于译文，源文永不换行，因此行数只标在 ``target`` 的宽度标注上。
    """

    grouped: dict[int, list[dict[str, Any]]] = {}
    for issue in qa_issues:
        cue_id = issue.get("cue_id")
        if not isinstance(cue_id, int) or isinstance(cue_id, bool):
            continue
        message = str(issue.get("message", ""))
        annotation: dict[str, Any] = {"code": str(issue.get("code", "")), "message": message}
        limits = overflow_limits(message)
        if limits is not None:
            annotation["display_width"], annotation["limit"] = limits
            annotation["width_unit"] = "ASS pixels" if "ASS pixels" in message else "display units"
            if message.startswith("target line width "):
                annotation["max_lines"] = TARGET_WRAP_MAX_LINES
        grouped.setdefault(cue_id, []).append(annotation)
    return [
        {
            "id": cue.id,
            "source_text": cue.source_text,
            "must_fix": cue.id in grouped,
            "qa": grouped.get(cue.id, []),
            "text": cue.target_text or "",
        }
        for cue in cues
        if cue.id in affected
    ]


def ensure_translation_review_input(
    job_file: str | Path,
    *,
    event_sink: EventSink | None = None,
) -> Path:
    job = load_job(job_file)
    existing = translation_review_input_path(job)
    if existing is not None:
        # 旧 Job 可能只有 JSON 模板；SRT 是后加的人工通道，缺失时按同一 fingerprint 补齐。
        srt_path = existing.with_suffix(".srt")
        if not srt_path.is_file() or srt_path.is_symlink():
            target = _target_kinds(job)
            if target is not None:
                _, _, _, candidate_path = target
                affected = _review_item_ids(existing)
                if affected:
                    save_review_srt(
                        [cue for cue in load_cues(candidate_path) if cue.id in affected],
                        srt_path,
                    )
        return existing
    target = _target_kinds(job)
    if target is None:
        raise UserInputError("no current semantic review report is available for revision")
    report_kind, candidate_kind, report_path, candidate_path = target
    report = json.loads(report_path.read_text(encoding="utf-8"))
    issues = _issue_set(report)
    qa_issues = _candidate_qa_issue_set(job, candidate_path)
    if not issues and not qa_issues:
        raise UserInputError("current translation candidate does not require revision")
    fingerprint = _revision_fingerprint(job, report_kind, candidate_kind, report)
    cues = load_cues(candidate_path)
    affected = {cue_id for issue in issues for cue_id in issue["cue_ids"]} | {
        issue["cue_id"] for issue in qa_issues
    }
    path = get_job_directory(job) / "review" / f"{translation_review_stem(fingerprint)}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(
        path,
        {
            "task": TRANSLATION_REVIEW_TASK,
            "revision_fingerprint": fingerprint,
            "scope": "translation",
            "report": str(report_path),
            "issues": issues,
            "qa_report": str(valid_artifact_path(job, "translation_candidate_qa")),
            "qa_issues": qa_issues,
            "items": _translation_review_items(cues, affected, qa_issues),
        },
    )
    # SRT 与 JSON 同名同 fingerprint，供人工用任意字幕工具编辑；两条通道等价。
    save_review_srt([cue for cue in cues if cue.id in affected], path.with_suffix(".srt"))
    if event_sink:
        event_sink(
            create_event(
                EventType.TRANSLATION_REVIEW_INPUT_READY,
                **job_event_fields(job.id, get_job_file(job)),
                path=str(path),
                scope="translation",
                item_count=len(affected),
            )
        )
    return path


def ensure_semantic_review_inputs(
    job_file: str | Path,
    *,
    event_sink: EventSink | None = None,
) -> tuple[Path, ...]:
    translation_input = ensure_translation_review_input(job_file, event_sink=event_sink)
    source_input = ensure_source_suspect_review_input(job_file, event_sink=event_sink)
    if source_input is None:
        return (translation_input,)
    return source_input, translation_input


def _json_review_changes(
    response_file: Path,
    expected_fingerprint: str,
    expected_ids: set[int],
) -> dict[int, str]:
    """解析 JSON 修订模板，返回 ``{cue id: 新译文}``。供 Agent 与脚本消费。"""

    response = load_user_response_json(response_file, what="translation review response")
    if not isinstance(response, dict):
        raise UserInputError("translation review response must be a JSON object")
    if response.get("task") != TRANSLATION_REVIEW_TASK or response.get("scope") != "translation":
        raise UserInputError("translation review response task or scope is invalid")
    if response.get("revision_fingerprint") != expected_fingerprint:
        raise UserInputError("translation review response is stale for the current report")
    items = response.get("items")
    if not isinstance(items, list) or not items:
        raise UserInputError("translation review response items must be a non-empty array")
    changes: dict[int, str] = {}
    for item in items:
        cue_id = item.get("id") if isinstance(item, dict) else None
        text = item.get("text") if isinstance(item, dict) else None
        if (
            not isinstance(cue_id, int)
            or isinstance(cue_id, bool)
            or cue_id in changes
            or not isinstance(text, str)
            or not text.strip()
        ):
            raise UserInputError(
                "translation review response contains an invalid or duplicate item"
            )
        changes[cue_id] = text.strip()
    if set(changes) != expected_ids:
        raise UserInputError(
            "translation review response must preserve the complete template cue id set"
        )
    return changes


def _srt_review_changes(
    response_file: Path,
    candidate: list[Cue],
    affected: set[int],
    expected_fingerprint: str,
) -> dict[int, str]:
    """解析人工编辑过的修订 SRT，返回 ``{cue id: 新译文}``。

    SRT 不带 fingerprint 字段，改由文件名承载：要求文件名主干与模板生成时的
    ``translation_review_stem`` 完全一致（改名即视为过期），等价于 JSON 通道的
    ``revision_fingerprint`` 字段校验。

    cue ID 按时间轴精确匹配还原——时间轴是不可破坏的单一来源，因此改动时间轴、增删
    cue 都会在此被拒；``#<id>`` 注释行若存在，必须与时间轴匹配结果一致。
    """

    expected_stem = translation_review_stem(expected_fingerprint)
    if response_file.stem != expected_stem:
        raise UserInputError(
            "translation review response is stale for the current report "
            f"(expected a file named {expected_stem}.srt)"
        )
    try:
        entries = load_review_srt(response_file)
    except (OSError, ValueError) as error:
        raise UserInputError(f"cannot read translation review response: {error}") from error
    if not entries:
        raise UserInputError("translation review response items must be a non-empty array")

    by_timing = {(cue.start_ms, cue.end_ms): cue.id for cue in candidate if cue.id in affected}
    if len(by_timing) != len(affected):
        raise UserInputError(
            "cannot map the review SRT back to cue ids because candidate cues share timing"
        )
    changes: dict[int, str] = {}
    for marker_id, start_ms, end_ms, text in entries:
        cue_id = by_timing.get((start_ms, end_ms))
        if cue_id is None:
            raise UserInputError(
                "translation review SRT changed cue timing or added a cue; "
                "revision may only change translated text"
            )
        if marker_id is not None and marker_id != cue_id:
            raise UserInputError(
                f"translation review SRT cue id marker #{marker_id} does not match its timing"
            )
        if cue_id in changes:
            raise UserInputError(
                "translation review response contains an invalid or duplicate item"
            )
        if not text.strip():
            raise UserInputError(
                "translation review response contains an invalid or duplicate item"
            )
        changes[cue_id] = text.strip()
    if set(changes) != set(by_timing.values()):
        raise UserInputError(
            "translation review SRT removed a cue; revision may only change translated text"
        )
    return changes


@dataclass
class _TranslationRevision:
    job: Job
    report_kind: str
    candidate_kind: str
    report_path: Path
    report: dict[str, Any]
    issues: list[dict[str, Any]]
    qa_issues: list[dict[str, Any]]
    fingerprint: str
    reviewed: list[Cue]
    changed_ids: set[int]
    qa: SubtitleQaReport


def _prepare_translation_revision(
    job_file: str | Path,
    response_file: str | Path,
) -> _TranslationRevision:
    job = load_job(job_file)
    target = _target_kinds(job)
    if target is None:
        raise UserInputError("no current semantic review report is available for revision")
    report_kind, candidate_kind, report_path, candidate_path = target
    report = json.loads(report_path.read_text(encoding="utf-8"))
    issues = _issue_set(report)
    qa_issues = _candidate_qa_issue_set(job, candidate_path)
    affected = {cue_id for issue in issues for cue_id in issue["cue_ids"]} | {
        issue["cue_id"] for issue in qa_issues
    }
    expected_fingerprint = _revision_fingerprint(job, report_kind, candidate_kind, report)
    candidate = load_cues(candidate_path)
    # SRT 与 JSON 是同一修订通道的两种载体，各自解析后汇聚成同一个 changes 映射，
    # 后续 fingerprint 绑定、只改译文、review-only 复查的语义完全共用。
    if Path(response_file).suffix.lower() == ".srt":
        changes = _srt_review_changes(
            Path(response_file),
            candidate,
            affected,
            expected_fingerprint,
        )
    else:
        changes = _json_review_changes(Path(response_file), expected_fingerprint, affected)
    known_ids = {cue.id for cue in candidate}
    if not changes.keys() <= known_ids:
        raise UserInputError("translation review response contains an unknown cue id")
    current_text = {cue.id: cue.target_text or "" for cue in candidate}
    changed_ids = {cue_id for cue_id, text in changes.items() if current_text[cue_id] != text}
    if not changed_ids:
        message = "translation review response does not change any cue text"
        blocked = _describe_unchanged_qa_cues(qa_issues)
        if blocked:
            message = f"{message}; {blocked}"
        raise UserInputError(message)
    reviewed = [replace(cue, target_text=changes.get(cue.id, cue.target_text)) for cue in candidate]
    qa = check_subtitles(reviewed, require_translation=True, **translation_qa_context(job))
    return _TranslationRevision(
        job,
        report_kind,
        candidate_kind,
        report_path,
        report,
        issues,
        qa_issues,
        expected_fingerprint,
        reviewed,
        changed_ids,
        qa,
    )


def _revision_preflight_result(revision: _TranslationRevision) -> dict[str, Any]:
    blockers = [
        asdict(issue)
        for issue in revision.qa.issues
        if issue.severity == "error"
        or (issue.severity == "review" and issue.message.startswith("target line width "))
    ]
    return {
        "task": "translation_review_preflight",
        "valid": not blockers,
        "changed_ids": sorted(revision.changed_ids),
        "blockers": blockers,
        "qa": asdict(revision.qa),
    }


def preflight_translation_review_fixes(
    job_file: str | Path,
    response_file: str | Path,
) -> dict[str, Any]:
    """只读校验 JSON/SRT 身份与修订后的确定性 QA；不写 Job、不调用模型。"""
    return _revision_preflight_result(_prepare_translation_revision(job_file, response_file))


def apply_translation_review_fixes(
    job_file: str | Path,
    response_file: str | Path,
    *,
    event_sink: EventSink | None = None,
) -> tuple[Path, Path]:
    revision = _prepare_translation_revision(job_file, response_file)
    preflight = _revision_preflight_result(revision)
    if not preflight["valid"]:
        details = "; ".join(
            f"cue {issue['cue_id']}: {issue['code']}: {issue['message']}"
            for issue in preflight["blockers"]
        )
        raise UserInputError(f"translation review preflight failed; no changes saved; {details}")
    job = revision.job
    report_kind, candidate_kind = revision.report_kind, revision.candidate_kind
    report_path, report = revision.report_path, revision.report
    issues, qa_issues = revision.issues, revision.qa_issues
    expected_fingerprint = revision.fingerprint
    reviewed, changed_ids = revision.reviewed, revision.changed_ids
    affected = {cue_id for issue in issues for cue_id in issue["cue_ids"]} | {
        issue["cue_id"] for issue in qa_issues
    }

    artifacts_dir = get_job_directory(job) / "artifacts"
    revision_suffix = expected_fingerprint[:12]
    revision_path = artifacts_dir / f"translation-review-revision-{revision_suffix}.json"
    reviewed_path = artifacts_dir / f"translation-reviewed-cues-{revision_suffix}.json"
    write_json(reviewed_path, [asdict(cue) for cue in reviewed])
    reviewed_checksum = checksum(reviewed_path)
    parent_revision_path = valid_artifact_path(job, "translation_review_revision")
    parent_revision = (
        {
            "path": str(parent_revision_path),
            "checksum": artifact_checksum_for(job, "translation_review_revision"),
        }
        if parent_revision_path is not None
        else None
    )
    write_json(
        revision_path,
        {
            "task": TRANSLATION_REVIEW_TASK,
            "revision_fingerprint": expected_fingerprint,
            "scope": "translation",
            "source_report_kind": report_kind,
            "source_report_path": str(report_path),
            "source_report_checksum": artifact_checksum_for(job, report_kind),
            "source_candidate_kind": candidate_kind,
            "source_candidate_checksum": artifact_checksum_for(job, candidate_kind),
            "reviewed_candidate_checksum": reviewed_checksum,
            "semantic_grouping_version": report.get("semantic_grouping_version"),
            "template_ids": sorted(affected),
            "changed_ids": sorted(changed_ids),
            "base_issues": issues,
            "base_qa_issues": qa_issues,
            "parent_revision": parent_revision,
            "items": [{"id": cue.id, "text": cue.target_text or ""} for cue in reviewed],
        },
    )

    invalidated = {
        ArtifactKind.TRANSLATION_CANDIDATE_QA,
        ArtifactKind.TRANSLATION_SEMANTIC_QA,
        ArtifactKind.TRANSLATION_SEMANTIC_REPAIR,
        ArtifactKind.TRANSLATION_REPAIRED_CUES,
        ArtifactKind.TRANSLATION_SEMANTIC_QA_FINAL,
        ArtifactKind.TRANSLATION_REVIEW_REVISION,
        ArtifactKind.TRANSLATION_REVIEWED_CUES,
        ArtifactKind.TRANSLATION_SEMANTIC_QA_REVIEWED,
    }
    for root in tuple(invalidated):
        invalidated.update(downstream_of(root))
    job.artifacts = [artifact for artifact in job.artifacts if artifact.kind not in invalidated]
    job.artifacts.extend(
        (
            Artifact("translation_review_revision", str(revision_path), checksum(revision_path)),
            Artifact("translation_reviewed_cues", str(reviewed_path), checksum(reviewed_path)),
        )
    )
    job.config.pop("translation_review_acknowledgment", None)
    job.config.pop("subtitle_review_acknowledgment", None)
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
    save_job(job)
    if event_sink:
        for kind, path in (
            ("translation_review_revision", revision_path),
            ("translation_reviewed_cues", reviewed_path),
        ):
            event_sink(
                create_event(
                    EventType.ARTIFACT_CREATED,
                    **job_event_fields(job.id, get_job_file(job)),
                    kind=kind,
                    path=str(path),
                )
            )
        event_sink(
            create_event(
                EventType.TRANSLATION_REVIEW_REVISED,
                **job_event_fields(job.id, get_job_file(job)),
                scope="translation",
                item_count=len(changed_ids),
                checksum=artifact_checksum_for(job, "translation_reviewed_cues"),
            )
        )
    return revision_path, reviewed_path

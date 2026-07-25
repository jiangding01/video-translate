"""字幕 QA `needs_review` 报告的人工确认通道。

确认记录持久化在 job.config，绑定 source cue checksum 与具体 review 问题集合；
字幕变化或出现新的 review 问题后自动失效，error 级问题永不放行。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from xvt.errors import UserInputError
from xvt.events import EventSink, EventType, create_event, job_event_fields
from xvt.job_store import get_job_file, load_job, save_job, utc_now
from xvt.models import Job, StageName, StageStatus
from xvt.pipeline.common import _artifact_checksum_for, _set_stage
from xvt.recovery import valid_artifact_path

# 同一「显示宽度超限」风险在源字幕 QA 与双语 QA 中使用不同 code：
# 确认匹配按规范化后的问题族比较，用户对某个 cue 的宽度风险只需确认一次。
_REVIEW_CODE_FAMILIES = {
    "line_too_wide": "display_width",
    "bilingual_overflow_risk": "display_width",
}


def _canonical_review_issue(code: str, cue_id: int) -> tuple[str, int]:
    return (_REVIEW_CODE_FAMILIES.get(code, code), cue_id)


def _acknowledged_review_issues(job: Job) -> set[tuple[str, int]] | None:
    """返回仍然有效的已确认 review 问题集合；确认不存在或已失效时返回 None。

    有效性绑定 source_cues 的 checksum：字幕内容变化后旧确认自动失效。
    """
    record = job.config.get("subtitle_review_acknowledgment")
    if not isinstance(record, dict):
        return None
    if record.get("source_cues_checksum") != _artifact_checksum_for(job, "source_cues"):
        return None
    issues = record.get("acknowledged_issues")
    if not isinstance(issues, list):
        return None
    acknowledged: set[tuple[str, int]] = set()
    for item in issues:
        if not (isinstance(item, list) and len(item) == 2):
            return None
        code, cue_id = item
        if not isinstance(code, str) or not isinstance(cue_id, int):
            return None
        acknowledged.add(_canonical_review_issue(code, cue_id))
    return acknowledged


def _complete_qa_stage(
    job: Job,
    report: Any,
    event_sink: EventSink | None,
) -> None:
    if report.status == "needs_review":
        # 人工确认通道：仅当当前报告的全部 review 级问题都是用户已确认集合的
        # 子集时放行；出现任何新的 review 问题仍然阻断，error 级永不放行。
        acknowledged = _acknowledged_review_issues(job)
        review_issues = {
            _canonical_review_issue(issue.code, issue.cue_id)
            for issue in report.issues
            if issue.severity == "review"
        }
        if acknowledged is not None and review_issues and review_issues <= acknowledged:
            if event_sink:
                event_sink(
                    create_event(
                        EventType.SUBTITLE_REVIEW_ACKNOWLEDGED,
                        **job_event_fields(job.id, get_job_file(job)),
                        review_count=len(review_issues),
                        source="stored_acknowledgment",
                    )
                )
            _set_stage(job, StageName.SUBTITLE_QA, StageStatus.SUCCEEDED, event_sink)
            return
    if report.status in {"failed", "needs_review"}:
        if report.status == "needs_review":
            message = (
                "subtitle QA needs_review; inspect the QA artifact, then run "
                "xvt continue <job> --acknowledge-subtitle-review"
            )
        else:
            message = "subtitle QA failed; inspect the QA artifact and repair subtitles"
        _set_stage(
            job,
            StageName.SUBTITLE_QA,
            StageStatus.FAILED,
            event_sink,
            error=message,
        )
        raise RuntimeError(message)
    _set_stage(job, StageName.SUBTITLE_QA, StageStatus.SUCCEEDED, event_sink)


def acknowledge_subtitle_review(
    job_file: str | Path,
    *,
    event_sink: EventSink | None = None,
) -> Path:
    """人工确认当前 `needs_review` 的字幕 QA 报告，允许工作流继续。

    - 只确认 review 级问题；报告含 error 级问题或整体 `failed` 时拒绝。
    - 确认记录持久化到 job.config，绑定 source_cues checksum 与具体问题集合；
      字幕内容变化或出现新的 review 问题后自动失效，需重新查看报告确认。
    - 原始 QA 报告保持 `needs_review` 不被改写；放行事实以结构化事件与
      config 记录留痕，而不是把 QA 伪装成自然通过。
    """
    job = load_job(job_file)
    report_kind = None
    report_path = None
    for kind in ("translated_subtitle_qa", "subtitle_qa"):
        candidate = valid_artifact_path(job, kind)
        if candidate is not None:
            report_kind = kind
            report_path = candidate
            break
    if report_path is None:
        raise UserInputError("no subtitle QA report found to acknowledge")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    issues = report.get("issues", [])
    if report.get("status") == "failed" or any(
        issue.get("severity") == "error" for issue in issues if isinstance(issue, dict)
    ):
        raise UserInputError(
            "subtitle QA contains error-level problems that cannot be acknowledged; "
            "fix the subtitles instead"
        )
    if report.get("status") != "needs_review":
        raise UserInputError("subtitle QA report does not require review acknowledgment")

    review_issues = sorted(
        [issue["code"], issue["cue_id"]]
        for issue in issues
        if isinstance(issue, dict) and issue.get("severity") == "review"
    )
    job.config["subtitle_review_acknowledgment"] = {
        "report_kind": report_kind,
        "report_checksum": _artifact_checksum_for(job, report_kind or "subtitle_qa"),
        "source_cues_checksum": _artifact_checksum_for(job, "source_cues"),
        "acknowledged_issues": review_issues,
        "acknowledged_at": utc_now(),
    }
    qa_record = job.stages[StageName.SUBTITLE_QA]
    if qa_record.status is StageStatus.FAILED:
        qa_record.status = StageStatus.PENDING
        qa_record.started_at = None
        qa_record.completed_at = None
        qa_record.error = None
    save_job(job)
    if event_sink:
        event_sink(
            create_event(
                EventType.SUBTITLE_REVIEW_ACKNOWLEDGED,
                **job_event_fields(job.id, get_job_file(job)),
                review_count=len(review_issues),
                source="user_flag",
            )
        )
    return report_path


def _semantic_issue_keys(report: dict[str, Any]) -> tuple[tuple[str, tuple[int, ...]], ...]:
    issues = report.get("issues")
    if not isinstance(issues, list) or not issues:
        raise UserInputError("semantic review report has no review issues")
    keys: list[tuple[str, tuple[int, ...]]] = []
    for issue in issues:
        category = issue.get("category") if isinstance(issue, dict) else None
        cue_ids = issue.get("cue_ids") if isinstance(issue, dict) else None
        severity = issue.get("severity") if isinstance(issue, dict) else None
        if (
            not isinstance(category, str)
            or not isinstance(cue_ids, list)
            or not cue_ids
            or any(not isinstance(value, int) or isinstance(value, bool) for value in cue_ids)
            or severity != "review"
        ):
            raise UserInputError("semantic review report contains an invalid issue")
        keys.append((category, tuple(sorted(cue_ids))))
    if len(set(keys)) != len(keys):
        raise UserInputError("semantic review report contains duplicate issues")
    return tuple(sorted(keys))


def translation_review_acknowledged(job: Job, report_path: Path) -> bool:
    record = job.config.get("translation_review_acknowledgment")
    if not isinstance(record, dict):
        return False
    from xvt.pipeline.semantic import semantic_review_target

    target = semantic_review_target(job)
    if target is None or target[0] != report_path:
        return False
    repaired_path = valid_artifact_path(job, "translation_repaired_cues")
    candidate_kind = (
        "translation_repaired_cues"
        if repaired_path is not None and target[1] == repaired_path
        else "translation_polished_cues"
    )
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        issue_keys = _semantic_issue_keys(report)
    except (OSError, json.JSONDecodeError, UserInputError):
        return False
    return bool(
        record.get("source_cues_checksum") == _artifact_checksum_for(job, "source_cues")
        and record.get("candidate_cues_checksum") == _artifact_checksum_for(job, candidate_kind)
        and record.get("report_checksum")
        == _artifact_checksum_for(
            job,
            "translation_semantic_qa_final"
            if candidate_kind == "translation_repaired_cues"
            else "translation_semantic_qa",
        )
        and record.get("prompt_version") == report.get("prompt_version")
        and record.get("acknowledged_issues")
        == [[category, list(cue_ids)] for category, cue_ids in issue_keys]
    )


def acknowledge_translation_review(
    job_file: str | Path,
    *,
    event_sink: EventSink | None = None,
) -> Path:
    job = load_job(job_file)
    from xvt.pipeline.semantic import semantic_review_target

    target = semantic_review_target(job)
    if target is None:
        raise UserInputError("no confirmable semantic review report found")
    report_path, candidate_path = target
    repaired_path = valid_artifact_path(job, "translation_repaired_cues")
    report_kind = (
        "translation_semantic_qa_final"
        if repaired_path is not None and candidate_path == repaired_path
        else "translation_semantic_qa"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("status") != "needs_review":
        raise UserInputError("semantic review report does not require acknowledgment")
    issue_keys = _semantic_issue_keys(report)
    candidate_kind = (
        "translation_repaired_cues"
        if report_kind == "translation_semantic_qa_final"
        else "translation_polished_cues"
    )
    job.config["translation_review_acknowledgment"] = {
        "report_kind": report_kind,
        "report_checksum": _artifact_checksum_for(job, report_kind),
        "source_cues_checksum": _artifact_checksum_for(job, "source_cues"),
        "candidate_cues_checksum": _artifact_checksum_for(job, candidate_kind),
        "prompt_version": report.get("prompt_version"),
        "acknowledged_issues": [[category, list(cue_ids)] for category, cue_ids in issue_keys],
        "acknowledged_at": utc_now(),
    }
    record = job.stages[StageName.TRANSLATE]
    if record.status in {StageStatus.AWAITING_INPUT, StageStatus.FAILED}:
        record.status = StageStatus.PENDING
        record.started_at = None
        record.completed_at = None
        record.error = None
    save_job(job)
    if event_sink:
        event_sink(
            create_event(
                EventType.TRANSLATION_REVIEW_ACKNOWLEDGED,
                **job_event_fields(job.id, get_job_file(job)),
                review_count=len(issue_keys),
                source="user_flag",
            )
        )
    return report_path

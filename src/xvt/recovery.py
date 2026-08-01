from __future__ import annotations

import hashlib
import json
import shlex
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from xvt.job_store import get_job_file, load_job
from xvt.models import Artifact, Job, StageName, StageStatus
from xvt.workflow_config import WorkflowConfig, get_workflow_config, resolve_target_language


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    category: str
    reason: str
    job_file: str
    current_stage: str | None
    completed_stages: tuple[str, ...]
    reusable_artifacts: tuple[str, ...]
    primary_command: str | None = None
    prerequisite_commands: tuple[str, ...] = ()
    alternative_commands: tuple[str, ...] = ()
    input_paths: tuple[str, ...] = ()
    report_paths: tuple[str, ...] = ()
    # 与 workflow.plan_advance_steps 对齐的剩余推进步骤（端到端 Job 有值；原子 Job 为空）。
    advance_steps: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status_command"] = f"xvt status {shlex.quote(self.job_file)}"
        return data


@dataclass(frozen=True, slots=True)
class SubtitleQaFailureGuidance:
    """字幕 QA 阻断时的用户面引导（status / continue 共用）。"""

    reason: str
    message: str
    report_paths: tuple[str, ...]
    primary_command: str | None
    is_needs_review: bool


def file_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_artifact(artifact: Artifact) -> dict[str, Any]:
    path = Path(artifact.path)
    result: dict[str, Any] = {
        "kind": artifact.kind,
        "path": str(path),
        "expected_checksum": artifact.checksum,
    }
    if not path.is_file():
        return {**result, "status": "fail", "message": "artifact file is missing"}
    actual_checksum = file_checksum(path)
    result["actual_checksum"] = actual_checksum
    if artifact.checksum is None:
        return {**result, "status": "warn", "message": "artifact has no checksum"}
    if actual_checksum != artifact.checksum:
        return {**result, "status": "fail", "message": "artifact checksum mismatch"}
    return {**result, "status": "pass", "message": "artifact is valid"}


def valid_artifact_path(job: Job, kind: str) -> Path | None:
    artifact = next((item for item in job.artifacts if item.kind == kind), None)
    if artifact is None:
        return None
    result = verify_artifact(artifact)
    return Path(artifact.path) if result["status"] == "pass" else None


def _job_command(command: str, job_file: Path, *arguments: str) -> str:
    parts = ["xvt", command, shlex.quote(str(job_file)), *arguments]
    return " ".join(parts)


def _load_qa_report(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _qa_report_has_errors(report: dict[str, Any]) -> bool:
    if report.get("status") == "failed":
        return True
    issues = report.get("issues")
    if not isinstance(issues, list):
        return False
    return any(isinstance(issue, dict) and issue.get("severity") == "error" for issue in issues)


def _source_can_be_rebuilt(job: Job, valid_artifacts: dict[str, Path]) -> bool:
    if job.config.get("input_kind") == "subtitle":
        return False
    if (
        "media_info" in valid_artifacts
        and {
            "word_transcript",
            "normalized_audio",
        }
        & valid_artifacts.keys()
    ):
        return True
    parsed = urlparse(job.input)
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        return True
    return Path(job.input).expanduser().is_file()


def _source_selection_is_asr(job: Job, valid_artifacts: dict[str, Path]) -> bool:
    selection_path = valid_artifacts.get("source_selection")
    source_cues_path = valid_artifacts.get("source_cues")
    if selection_path is None or source_cues_path is None:
        return False
    try:
        selection = json.loads(selection_path.read_text(encoding="utf-8"))
        source_checksum = file_checksum(source_cues_path)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(selection, dict)
        and selection.get("selected_kind") == "asr"
        and selection.get("source_cues_checksum") == source_checksum
    )


def subtitle_qa_failure_guidance(
    job: Job,
    job_file: str | Path | None = None,
) -> SubtitleQaFailureGuidance | None:
    """当 ``subtitle_qa`` stage 为 failed 时返回恢复引导；否则返回 None。

    ``needs_review``（且无 error 级问题）指向 ``continue --acknowledge-subtitle-review``；
    error 级失败仍要求修复字幕，不提供 acknowledge 命令。
    """
    if job.stages[StageName.SUBTITLE_QA].status is not StageStatus.FAILED:
        return None

    resolved = (
        Path(job_file).expanduser().resolve()
        if job_file is not None
        else get_job_file(job).resolve()
    )
    report_paths: list[str] = []
    blocking: dict[str, Any] | None = None
    for kind in ("translated_subtitle_qa", "subtitle_qa"):
        path = valid_artifact_path(job, kind)
        if path is None:
            continue
        report_paths.append(str(path))
        if blocking is None:
            blocking = _load_qa_report(path)

    if blocking is not None and not _qa_report_has_errors(blocking):
        if blocking.get("status") == "needs_review":
            command = _job_command(
                "continue",
                resolved,
                "--acknowledge-subtitle-review",
            )
            reason = (
                "字幕 QA 需要人工确认（needs_review）。"
                "请查看报告后运行确认命令继续；确认不会改写原始 QA 报告。"
            )
            return SubtitleQaFailureGuidance(
                reason=reason,
                message=f"{reason} 下一步：{command}",
                report_paths=tuple(report_paths),
                primary_command=command,
                is_needs_review=True,
            )

    reason = "字幕 QA 未通过，需要检查报告并修复字幕后再继续。"
    return SubtitleQaFailureGuidance(
        reason=reason,
        message=reason,
        report_paths=tuple(report_paths),
        primary_command=None,
        is_needs_review=False,
    )


def build_recovery_plan(job_or_file: Job | str | Path) -> RecoveryPlan:
    job = job_or_file if isinstance(job_or_file, Job) else load_job(job_or_file)
    job_file = get_job_file(job).resolve()
    valid_artifacts = {
        artifact.kind: path
        for artifact in job.artifacts
        if (path := valid_artifact_path(job, artifact.kind)) is not None
    }
    completed = tuple(
        name
        for name, record in job.stages.items()
        if record.status in {StageStatus.SUCCEEDED, StageStatus.SKIPPED}
    )
    active = next(
        (
            name
            for name, record in job.stages.items()
            if record.status in {StageStatus.RUNNING, StageStatus.AWAITING_INPUT}
        ),
        None,
    )
    failed = [name for name, record in job.stages.items() if record.status is StageStatus.FAILED]
    current_stage = active or (failed[-1] if failed else None)

    def plan(category: str, reason: str, **data: Any) -> RecoveryPlan:
        return RecoveryPlan(
            category=category,
            reason=reason,
            job_file=str(job_file),
            current_stage=current_stage,
            completed_stages=completed,
            reusable_artifacts=tuple(valid_artifacts),
            **data,
        )

    workflow = get_workflow_config(job)
    target_language = resolve_target_language(job)

    source_cues = valid_artifacts.get("source_cues")
    subtitle_resumable = (
        job.config.get("input_kind") == "subtitle" and "input_subtitles" in valid_artifacts
    )
    resumable = "media_info" in valid_artifacts and bool(
        {"word_transcript", "normalized_audio"} & valid_artifacts.keys()
    )
    if source_cues is None:
        if job.config.get("source_rebuild_requested") is True and _source_can_be_rebuilt(
            job, valid_artifacts
        ):
            return plan(
                "manual_fix",
                "ASR 源字幕重建尚未完成，可以重试显式修复命令。",
                primary_command=_job_command(
                    "rebuild-source",
                    job_file,
                    "--strategy",
                    "asr",
                ),
            )
        if subtitle_resumable:
            command = "continue" if workflow is not None else "resume"
            return plan(
                "retryable",
                "存在 checksum 有效的源字幕，可以重新生成字幕产物。",
                primary_command=_job_command(command, job_file),
            )
        if resumable:
            command = "continue" if workflow is not None else "resume"
            return plan(
                "retryable",
                "存在 checksum 有效的转写 checkpoint，可以从断点继续。",
                primary_command=_job_command(command, job_file),
            )
        parsed_input = urlparse(job.input)
        if (
            workflow is not None
            and parsed_input.scheme in {"http", "https"}
            and parsed_input.hostname
        ):
            return plan(
                "retryable",
                "远程任务可以在当前 Job 中重新获取素材并继续。",
                primary_command=_job_command("continue", job_file),
            )
        return plan(
            "unrecoverable",
            "没有 checksum 有效的源字幕或转写 checkpoint，当前 Job 不能自动续跑。",
        )

    qa_failed = job.stages[StageName.SUBTITLE_QA].status is StageStatus.FAILED
    if qa_failed:
        source_report_path = valid_artifacts.get("subtitle_qa")
        translated_report_path = valid_artifacts.get("translated_subtitle_qa")
        source_report = _load_qa_report(source_report_path) if source_report_path else None
        translated_report = (
            _load_qa_report(translated_report_path) if translated_report_path else None
        )
        if source_report is None and "source_srt" not in valid_artifacts:
            command = "continue" if workflow is not None else "resume"
            return plan(
                "retryable",
                "源字幕 QA 或渲染在落盘前中断，可以从现有 source cues 继续。",
                primary_command=_job_command(command, job_file),
            )
        source_has_errors = bool(source_report and _qa_report_has_errors(source_report))
        translated_has_errors = bool(translated_report and _qa_report_has_errors(translated_report))
        if (
            source_has_errors
            and not translated_has_errors
            and job.config.get("source_rebuild_requested") is not True
            and not _source_selection_is_asr(job, valid_artifacts)
            and _source_can_be_rebuilt(job, valid_artifacts)
        ):
            return plan(
                "manual_fix",
                "源字幕 QA 未通过，可以使用 ASR 在当前 Job 中重建时间轴；"
                "旧翻译、渲染和烧录产物将失效。",
                primary_command=_job_command(
                    "rebuild-source",
                    job_file,
                    "--strategy",
                    "asr",
                ),
                report_paths=(str(source_report_path),),
            )
        guidance = subtitle_qa_failure_guidance(job, job_file)
        assert guidance is not None
        return plan(
            "manual_fix",
            guidance.reason,
            primary_command=guidance.primary_command,
            report_paths=guidance.report_paths,
        )

    # 端到端 Job：翻译/烧录剩余步骤与 continue 共用 plan_advance_steps。
    if workflow is not None:
        return _recovery_for_workflow(
            job,
            job_file=job_file,
            workflow=workflow,
            valid_artifacts=valid_artifacts,
            plan=plan,
        )

    # 原子 Job（无 workflow 目标）：保持独立 translate / apply 入口。
    from xvt.pipeline.semantic import (
        agent_semantic_review_waiting,
        semantic_review_approved,
        semantic_review_waiting,
    )
    from xvt.pipeline.translation import (
        agent_translation_polish_progress_ready,
        agent_translation_polish_started,
        agent_translation_polish_waiting,
    )

    if agent_semantic_review_waiting(job):
        return plan(
            "awaiting_input",
            "任务正在等待 Agent 语义校对与一次修复结果回填。",
            prerequisite_commands=(_job_command("apply-semantic-review", job_file, "RESPONSE"),),
            report_paths=(str(valid_artifacts["translation_semantic_qa"]),),
        )

    if semantic_review_waiting(job):
        from xvt.pipeline.revision import translation_review_input_path

        report_kind = (
            "translation_semantic_qa_reviewed"
            if "translation_semantic_qa_reviewed" in valid_artifacts
            else "translation_semantic_qa_final"
            if "translation_semantic_qa_final" in valid_artifacts
            else "translation_semantic_qa"
        )
        review_input = translation_review_input_path(job)
        fix_command = (
            _job_command("apply-translation-review-fixes", job_file, str(review_input))
            if review_input is not None
            else _job_command("continue", job_file)
        )
        return plan(
            "awaiting_input",
            "语义校对仍有 review 问题；可编辑修订模板后回填，或显式确认当前译文。",
            primary_command=fix_command,
            alternative_commands=(_job_command("acknowledge-translation-review", job_file),),
            input_paths=((str(review_input),) if review_input is not None else ()),
            report_paths=(str(valid_artifacts[report_kind]),),
        )

    if (
        job.config.get("translation_quality") == "polished"
        and "translation_polished_cues" in valid_artifacts
        and not semantic_review_approved(job)
    ):
        target = target_language or "LANGUAGE"
        return plan(
            "retryable",
            "润色候选有效，可以重新执行原子翻译命令继续语义校对。",
            primary_command=_job_command(
                "translate",
                job_file,
                "--target-language",
                target,
                "--translation-quality",
                "polished",
            ),
        )

    agent_waiting = agent_translation_polish_waiting(job)
    agent_progress = agent_translation_polish_progress_ready(job)
    invalid_polished_agent = (
        job.config.get("translation_quality") == "polished"
        and agent_translation_polish_started(job)
        and not agent_waiting
        and not agent_progress
    )
    if not invalid_polished_agent and (
        job.stages[StageName.TRANSLATE].status is StageStatus.AWAITING_INPUT
        or agent_waiting
        or agent_progress
    ):
        target = target_language or "LANGUAGE"
        apply_command = _job_command(
            "apply-translation",
            job_file,
            "RESPONSE",
            "--target-language",
            target,
        )
        return plan(
            "awaiting_input",
            "任务正在等待翻译结果回填。",
            primary_command=None,
            prerequisite_commands=(apply_command,),
        )

    if target_language and "translated_cues" not in valid_artifacts:
        return plan(
            "retryable",
            "源字幕有效，可以重新执行翻译阶段。",
            primary_command=_job_command(
                "translate",
                job_file,
                "--target-language",
                target_language,
            ),
        )

    return plan(
        "complete",
        "当前目标已完成，可以校验已有产物。",
        primary_command=_job_command("verify", job_file),
    )


def _recovery_for_workflow(
    job: Job,
    *,
    job_file: Path,
    workflow: WorkflowConfig,
    valid_artifacts: dict[str, Path],
    plan: Callable[..., RecoveryPlan],
) -> RecoveryPlan:
    """按 ``plan_advance_steps`` 生成端到端 Job 的恢复建议。

    延迟导入 ``xvt.workflow``，避免 ``workflow → recovery → workflow`` 循环导入。
    """

    from xvt.workflow import plan_advance_steps

    steps = plan_advance_steps(job, workflow)
    if not steps:
        return plan(
            "complete",
            "当前目标已完成，可以校验已有产物。",
            primary_command=_job_command("verify", job_file),
            advance_steps=(),
        )

    if steps[0] == "await_agent_translation":
        target = workflow.target_language or "LANGUAGE"
        apply_command = _job_command(
            "apply-translation",
            job_file,
            "RESPONSE",
            "--target-language",
            target,
        )
        return plan(
            "awaiting_input",
            "任务正在等待翻译结果回填。",
            primary_command=_job_command("continue", job_file),
            prerequisite_commands=(apply_command,),
            advance_steps=steps,
        )

    if steps[0] == "await_agent_polish":
        return plan(
            "awaiting_input",
            "任务正在等待译文润色结果回填。",
            primary_command=_job_command("continue", job_file),
            prerequisite_commands=(_job_command("apply-polish-translation", job_file, "RESPONSE"),),
            advance_steps=steps,
        )

    if steps[0] == "await_agent_semantic_review":
        return plan(
            "awaiting_input",
            "任务正在等待 Agent 语义校对与一次修复结果回填。",
            primary_command=_job_command("continue", job_file),
            prerequisite_commands=(_job_command("apply-semantic-review", job_file, "RESPONSE"),),
            report_paths=tuple(
                str(valid_artifacts[kind])
                for kind in ("translation_semantic_qa", "translation_semantic_qa_final")
                if kind in valid_artifacts
            ),
            advance_steps=steps,
        )

    if steps[0] == "await_translation_review":
        from xvt.pipeline.revision import translation_review_input_path

        review_input = translation_review_input_path(job)
        fix_command = (
            _job_command("apply-translation-review-fixes", job_file, str(review_input))
            if review_input is not None
            else _job_command("continue", job_file)
        )
        return plan(
            "awaiting_input",
            "语义校对仍有 review 问题；可编辑修订模板后回填，或显式确认当前译文。",
            primary_command=fix_command,
            alternative_commands=(
                _job_command(
                    "continue",
                    job_file,
                    "--acknowledge-translation-review",
                ),
            ),
            input_paths=((str(review_input),) if review_input is not None else ()),
            report_paths=tuple(
                str(valid_artifacts[kind])
                for kind in (
                    "translation_semantic_qa_reviewed",
                    "translation_semantic_qa_final",
                    "translation_semantic_qa",
                )
                if kind in valid_artifacts
            ),
            advance_steps=steps,
        )

    if steps[0] == "repair_polish_draft":
        return plan(
            "manual_fix",
            "润色草稿缺失或 checksum 已失效，不能从最终译文安全重建；"
            "请重新导入或生成原始译文草稿。",
            primary_command=None,
            advance_steps=steps,
        )

    if "translate" in steps or "import_translation" in steps:
        reason = "源字幕有效，可以继续未完成的翻译阶段。"
    elif "polish" in steps:
        reason = "译文草稿有效，可以继续未完成的润色阶段。"
    elif "semantic_review" in steps:
        reason = "润色候选有效，可以继续未完成的语义校对阶段。"
    elif "render" in steps:
        reason = "译文 cue 已就绪，可以补齐字幕渲染产物后继续。"
    elif "burn" in steps:
        reason = _burn_recovery_reason(job, valid_artifacts)
    else:
        reason = "存在未完成的工作流步骤，可以继续。"

    return plan(
        "retryable",
        reason,
        primary_command=_job_command("continue", job_file),
        advance_steps=steps,
    )


def _burn_recovery_reason(job: Job, valid_artifacts: dict[str, Path]) -> str:
    """烧录未完成时的人类可读原因；媒体 QA 文案与历史 status 输出保持一致。"""

    media_qa_status = job.stages[StageName.MEDIA_QA].status
    if "burned_video" not in valid_artifacts or media_qa_status is not StageStatus.SUCCEEDED:
        return "烧录尚未通过媒体 QA，可以重新烧录并检查输出视频。"
    return "烧录派生指纹已失效，可以重新烧录并检查输出视频。"


def verify_job(job_file: str | Path) -> dict[str, Any]:
    job = load_job(job_file)
    artifacts = [verify_artifact(artifact) for artifact in job.artifacts]
    failed = [artifact for artifact in artifacts if artifact["status"] == "fail"]
    warnings = [artifact for artifact in artifacts if artifact["status"] == "warn"]
    return {
        "status": "fail" if failed else "pass",
        "job_id": job.id,
        "job_file": str(Path(job_file).expanduser().resolve()),
        "artifact_count": len(artifacts),
        "failed_count": len(failed),
        "warning_count": len(warnings),
        "artifacts": artifacts,
    }

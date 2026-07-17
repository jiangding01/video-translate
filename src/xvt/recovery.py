from __future__ import annotations

import hashlib
import shlex
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from xvt.job_store import get_job_file, load_job
from xvt.models import Artifact, Job, StageName, StageStatus


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
    report_paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status_command"] = f"xvt status {shlex.quote(self.job_file)}"
        return data


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

    workflow = job.config.get("workflow")
    target_language = None
    if isinstance(workflow, dict):
        value = workflow.get("target_language")
        target_language = value if isinstance(value, str) and value else None
    if target_language is None:
        value = job.config.get("target_language")
        target_language = value if isinstance(value, str) and value else None

    source_cues = valid_artifacts.get("source_cues")
    resumable = "media_info" in valid_artifacts and bool(
        {"word_transcript", "normalized_audio"} & valid_artifacts.keys()
    )
    if source_cues is None:
        if resumable:
            command = "continue" if isinstance(workflow, dict) else "resume"
            return plan(
                "retryable",
                "存在 checksum 有效的转写 checkpoint，可以从断点继续。",
                primary_command=_job_command(command, job_file),
            )
        return plan(
            "unrecoverable",
            "没有 checksum 有效的源字幕或转写 checkpoint，当前 Job 不能自动续跑。",
        )

    qa_failed = job.stages[StageName.SUBTITLE_QA].status is StageStatus.FAILED
    if qa_failed:
        reports = tuple(
            str(valid_artifacts[kind])
            for kind in ("translated_subtitle_qa", "subtitle_qa")
            if kind in valid_artifacts
        )
        return plan(
            "manual_fix",
            "字幕 QA 未通过，需要检查报告并修复字幕后再继续。",
            report_paths=reports,
        )

    if job.stages[StageName.TRANSLATE].status is StageStatus.AWAITING_INPUT:
        target = target_language or "LANGUAGE"
        apply_command = _job_command(
            "apply-translation",
            job_file,
            "RESPONSE",
            "--target-language",
            target,
        )
        continue_command = (
            _job_command("continue", job_file) if isinstance(workflow, dict) else None
        )
        return plan(
            "awaiting_input",
            "任务正在等待翻译结果回填。",
            primary_command=continue_command,
            prerequisite_commands=(apply_command,),
        )

    if target_language and "translated_cues" not in valid_artifacts:
        if isinstance(workflow, dict):
            return plan(
                "retryable",
                "源字幕有效，可以继续未完成的翻译阶段。",
                primary_command=_job_command("continue", job_file),
            )
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

    if isinstance(workflow, dict) and workflow.get("burn"):
        if "burned_video" not in valid_artifacts:
            return plan(
                "retryable",
                "字幕产物有效，可以继续未完成的烧录阶段。",
                primary_command=_job_command("continue", job_file),
            )

    return plan(
        "complete",
        "当前目标已完成，可以校验已有产物。",
        primary_command=_job_command("verify", job_file),
    )


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

from __future__ import annotations

import json
import shlex
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from xvt.candidate_qa import translation_candidate_qa_issues
from xvt.checksums import file_checksum as _hash_file
from xvt.job_store import get_job_file, load_job
from xvt.models import Artifact, Job, StageName, StageStatus
from xvt.workflow_config import (
    WorkflowConfig,
    get_workflow_config,
    resolve_target_language,
    resolve_translation_quality,
)

_checksum_cache: ContextVar[dict[tuple[Any, ...], str] | None] = ContextVar(
    "xvt_readonly_checksum_cache", default=None
)


@contextmanager
def readonly_checksum_scope() -> Iterator[None]:
    """只读判定期间的进程内 checksum 记忆化作用域。

    只能包住不写入任何产物的代码。作用域内同一
    ``(path, st_dev, st_ino, st_size, st_mtime_ns)`` 只哈希一次；
    不跨命令、不跨进程、不落盘。嵌套时内层建新字典，退出即复位。

    文件元数据在这里只用于给"同一次判定内的同一个文件"定身份，
    绝不用于替代内容校验：stat 只能证伪（元数据不符 ⇒ 内容必然已变），
    永远不能证实（元数据相符不蕴含内容未变）。
    """

    token = _checksum_cache.set({})
    try:
        yield
    finally:
        _checksum_cache.reset(token)


def file_checksum(path: Path) -> str:
    """文件内容 SHA-256；在 ``readonly_checksum_scope`` 内做记忆化。

    无作用域时逐字等价于 ``xvt.checksums.file_checksum``。
    """

    cache = _checksum_cache.get()
    if cache is None:
        return _hash_file(path)
    try:
        stat = path.stat()
    except OSError:
        # 让原始 OSError 在真正读取时按现有路径抛出，不在这里吞掉。
        return _hash_file(path)
    key = (str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    value = cache.get(key)
    if value is None:
        value = _hash_file(path)
        cache[key] = value
    return value


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


def atomic_translated_qa_rebuild_command(
    job: Job,
    job_file: str | Path | None = None,
) -> str | None:
    """原子 Job 上零 API 成本重建译文 QA 与渲染产物的命令；不可行时返回 None。

    `import-translation` 与端到端 `continue` 的 render 步骤走同一个 ``finalize_translation``
    收尾，只消费 Job 内已经 checksum 有效的 ``translated.srt``，不调用任何翻译提供方。
    承诺仅限于「零 API 成本重建 QA 与渲染产物」——它按当前源 cue 重新映射译文，不承诺
    ``translated_cues`` 逐字不变。代价是这份字幕会被登记为 ``input_translation_subtitles`` /
    ``translation_mapping``，Job 从此带上「导入译文」的痕迹，reason 与文档都要写明。

    三处共用：原子恢复分支的新判定、``qa_blocked`` 上 ``workflow`` 为 None 的死路，
    以及 ``pipeline.burnin`` 的原子 QA 门文案。
    """

    target_language = resolve_target_language(job)
    if not target_language:
        return None
    translated_srt = valid_artifact_path(job, "translated_srt")
    if translated_srt is None:
        return None
    resolved = (
        Path(job_file).expanduser().resolve()
        if job_file is not None
        else get_job_file(job).resolve()
    )
    return _job_command(
        "import-translation",
        resolved,
        shlex.quote(str(translated_srt)),
        "--target-language",
        target_language,
    )


_CUE_RETRANSLATION_WAITING_REASON = (
    "任务正在等待局部重译（retranslate-cues）结果回填；未回填前不要按已完成处理。"
    "放弃这次重译需要手工删除 artifacts/cue-retranslation-batches.json。"
)


def _cue_retranslation_apply_command(job: Job, job_file: Path) -> str:
    """拼出可直接复制的 ``apply-cue-retranslation`` 命令。

    ``xvt apply`` 拿不到 ``--cue`` 与 ``--target-language``，所以这里从 manifest 读出
    具体 cue id 与目标语言；manifest 结构不可解析时退回形状占位，绝不猜测 cue id。
    惰性 import 与本文件其余 pipeline 引用同一模式，避免
    ``recovery ↔ pipeline.retranslate`` 循环导入。
    """

    from xvt.pipeline.retranslate import cue_retranslation_request

    request = cue_retranslation_request(job)
    if request is None:
        return _job_command(
            "apply-cue-retranslation",
            job_file,
            "RESPONSE",
            "--cue",
            "ID",
            "[--cue ID ...]",
            "--target-language",
            "LANG",
        )
    cue_ids, target_language = request
    arguments: list[str] = ["RESPONSE"]
    for cue_id in cue_ids:
        arguments.extend(("--cue", str(cue_id)))
    arguments.extend(("--target-language", shlex.quote(target_language)))
    return _job_command("apply-cue-retranslation", job_file, *arguments)


def _semantic_review_options(
    job: Job,
    job_file: Path,
    *,
    acknowledgment_command: str,
) -> tuple[str, str, tuple[str, ...], tuple[str, ...]]:
    from xvt.pipeline.revision import (
        source_review_input_path,
        source_review_supported,
        source_suspect_cue_ids,
        translation_review_input_path,
    )

    translation_input = translation_review_input_path(job)
    translation_command = (
        _job_command(
            "apply-translation-review-fixes",
            job_file,
            shlex.quote(str(translation_input)),
        )
        if translation_input is not None
        else _job_command("continue", job_file)
    )
    source_ids = source_suspect_cue_ids(job)
    source_supported = source_review_supported(job)
    source_input = (
        source_review_input_path(job, list(source_ids)) if source_ids and source_supported else None
    )
    # 人工修订同时提供 SRT 通道（同 fingerprint 同名），一并露出让用户自选载体。
    source_srt = source_input.with_suffix(".srt") if source_input else None
    if source_srt is not None and (not source_srt.is_file() or source_srt.is_symlink()):
        source_srt = None
    translation_srt = translation_input.with_suffix(".srt") if translation_input else None
    if translation_srt is not None and (
        not translation_srt.is_file() or translation_srt.is_symlink()
    ):
        translation_srt = None
    input_paths = tuple(
        str(path)
        for path in (source_input, source_srt, translation_input, translation_srt)
        if path is not None
    )
    candidate_path = next(
        (
            path
            for kind in (
                "translation_reviewed_cues",
                "translation_repaired_cues",
                "translation_polished_cues",
            )
            if (path := valid_artifact_path(job, kind)) is not None
        ),
        None,
    )
    qa_issues = (
        translation_candidate_qa_issues(job, candidate_path) if candidate_path is not None else None
    )
    if qa_issues:
        alternatives: tuple[str, ...] = ()
        if source_ids and source_supported and source_input is not None:
            alternatives = (
                _job_command(
                    "apply-source-review-fixes",
                    job_file,
                    shlex.quote(str(source_input)),
                ),
            )
        return (
            "候选译文的确定性 QA 仍有可修复问题；请修改译文模板后继续。"
            "这类问题不能通过语义确认绕过，也不会触发重新翻译或润色。",
            translation_command,
            alternatives,
            input_paths,
        )
    if source_ids and source_supported:
        if source_input is not None:
            primary = _job_command(
                "apply-source-review-fixes",
                job_file,
                shlex.quote(str(source_input)),
            )
        else:
            primary = _job_command("continue", job_file)
        return (
            "语义校对发现源文可能有误；源文修订会使译文、润色和下游产物失效，"
            "需要重新翻译，属于高成本路径。若源文无误，可只修订译文；译文修订只会重新执行"
            "语义校对，不会重新翻译或润色。",
            primary,
            (translation_command, acknowledgment_command),
            input_paths,
        )
    return (
        "语义校对仍有 review 问题；译文修订只会重新执行语义校对，不会重新翻译或润色；"
        "也可显式确认当前译文。",
        translation_command,
        (acknowledgment_command,),
        input_paths,
    )


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


def _glossary_violation_cue_ids(report: dict[str, Any]) -> tuple[int, ...] | None:
    """全部 error 级问题都是 ``glossary_violation`` 时返回涉及的 cue id；否则 None。

    只要混进任何一条别的 error（时间轴、缺译文、显示宽度……），局部重译就不是出口，
    必须退回通用的「查看报告并修复字幕」引导。cue id 缺失或不是真整数时同样放弃——
    命令必须能原样复制执行，猜出来的 id 会写坏译文。
    """

    issues = report.get("issues")
    if not isinstance(issues, list):
        return None
    cue_ids: list[int] = []
    for issue in issues:
        if not isinstance(issue, dict) or issue.get("severity") != "error":
            continue
        if issue.get("code") != "glossary_violation":
            return None
        cue_id = issue.get("cue_id")
        # bool 是 int 的子类；与项目其余解析器同口径显式排除。
        if isinstance(cue_id, bool) or not isinstance(cue_id, int) or cue_id < 1:
            return None
        cue_ids.append(cue_id)
    if not cue_ids:
        return None
    return tuple(sorted(dict.fromkeys(cue_ids)))


def _glossary_violation_guidance(
    job: Job,
    job_file: Path,
    reports: dict[str, dict[str, Any] | None],
    blocking: dict[str, Any] | None,
) -> tuple[str, str] | None:
    """译文 QA 只因术语违规而失败时，给出 ``retranslate-cues`` 出口 ``(reason, command)``。

    没有这条出口时该状态是死路：术语校验的 `glossary_violation` 是 error 级，acknowledge
    通道只覆盖 review 级，恢复计划因此给 ``primary_command=None``。出口本身早已存在
    （`retranslate-cues` 复用 Job 保存的术语策略），只是没人告诉用户。

    源字幕 QA 也有 error 时不给：重译译文改不了源文问题。
    """

    translated_report = reports.get("translated_subtitle_qa")
    if translated_report is None or blocking is not translated_report:
        return None
    source_report = reports.get("subtitle_qa")
    if source_report is not None and _qa_report_has_errors(source_report):
        return None
    cue_ids = _glossary_violation_cue_ids(translated_report)
    target_language = resolve_target_language(job)
    if cue_ids is None or not target_language:
        return None

    arguments: list[str] = []
    for cue_id in cue_ids:
        arguments.extend(("--cue", str(cue_id)))
    arguments.extend(("--target-language", shlex.quote(target_language)))
    command = _job_command("retranslate-cues", job_file, *arguments)

    if resolve_translation_quality(job) == "polished":
        # polished Job 上回填走 finalize_translation 且没有 polished_result=True：结果被
        # 写成新的翻译草稿，润色候选、语义校对结论与最终译文一并失效，整条润色腿要重跑
        # （真实模型成本）。因此这里绝不能承诺「其余译文保持不变」。
        reason = (
            "译文 QA 未通过，但阻断的 error 级问题全部是术语表冲突（glossary_violation）。"
            "可以只重译这些 cue：retranslate-cues 复用 Job 保存的术语策略，时间轴保持不变。"
            "注意这是 polished 质量的 Job——回填结果会成为新的翻译草稿，润色候选、语义校对"
            "结论与最终译文一并失效，需要重新走一遍润色与语义校对（会真实调用模型）。"
            "若认为译文没问题，另一条路是调整术语表后重跑翻译。"
        )
    else:
        reason = (
            "译文 QA 未通过，但阻断的 error 级问题全部是术语表冲突（glossary_violation）。"
            "可以只重译这些 cue：retranslate-cues 复用 Job 保存的术语策略，只改写指定 cue，"
            "其余译文与全部时间轴保持不变，回填后重跑完整译文 QA。"
            "若认为译文没问题，另一条路是调整术语表后重跑翻译。"
        )
    return reason, command


def subtitle_qa_failure_guidance(
    job: Job,
    job_file: str | Path | None = None,
) -> SubtitleQaFailureGuidance | None:
    """当 ``subtitle_qa`` 阻断时返回恢复引导；否则返回 None。

    ``needs_review``（且无 error 级问题）指向 ``continue --acknowledge-subtitle-review``；
    error 级失败仍要求修复字幕，不提供 acknowledge 命令。
    """
    if job.stages[StageName.SUBTITLE_QA].status not in {
        StageStatus.FAILED,
        StageStatus.AWAITING_INPUT,
    }:
        return None
    if _translated_qa_needs_rebuild(job):
        return None

    resolved = (
        Path(job_file).expanduser().resolve()
        if job_file is not None
        else get_job_file(job).resolve()
    )
    report_paths: list[str] = []
    blocking: dict[str, Any] | None = None
    reports: dict[str, dict[str, Any] | None] = {}
    for kind in ("translated_subtitle_qa", "subtitle_qa"):
        path = valid_artifact_path(job, kind)
        if path is None:
            continue
        report_paths.append(str(path))
        reports[kind] = _load_qa_report(path)
        if blocking is None:
            blocking = reports[kind]

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

    glossary_guidance = _glossary_violation_guidance(job, resolved, reports, blocking)
    if glossary_guidance is not None:
        return SubtitleQaFailureGuidance(
            reason=glossary_guidance[0],
            message=f"{glossary_guidance[0]} 下一步：{glossary_guidance[1]}",
            report_paths=tuple(report_paths),
            primary_command=glossary_guidance[1],
            is_needs_review=False,
        )

    reason = "字幕 QA 未通过，需要检查报告并修复字幕后再继续。"
    return SubtitleQaFailureGuidance(
        reason=reason,
        message=reason,
        report_paths=tuple(report_paths),
        primary_command=None,
        is_needs_review=False,
    )


def _translated_qa_needs_rebuild(job: Job) -> bool:
    source_path = valid_artifact_path(job, "subtitle_qa")
    source_report = _load_qa_report(source_path) if source_path is not None else None
    return bool(
        source_report is not None
        and not _qa_report_has_errors(source_report)
        and any(item.kind == "translated_subtitle_qa" for item in job.artifacts)
        and valid_artifact_path(job, "translated_subtitle_qa") is None
        and valid_artifact_path(job, "translated_cues") is not None
    )


def _atomic_translated_qa_recovery(
    job: Job,
    job_file: Path,
    target_language: str | None,
    plan: Callable[..., RecoveryPlan],
) -> RecoveryPlan:
    """原子 Job 上「译文 QA 报告失效」的统一分类与引导。

    烧录与预览的 fail-closed 门以该报告为硬前置，报 ``complete`` 会让用户在
    「门说去修、status 说已完成」之间打转。
    """

    rebuild = atomic_translated_qa_rebuild_command(job, job_file)
    if rebuild is not None:
        assert target_language is not None
        return plan(
            "retryable",
            "译文 QA 报告已失效，烧录与预览会被安全门拒绝；可以从 Job 内仍然 checksum 有效的"
            "译文字幕零 API 成本重建 QA 与渲染产物（该命令会把这份字幕登记为导入译文）。",
            primary_command=rebuild,
            alternative_commands=(
                _job_command("translate", job_file, "--target-language", target_language),
            ),
        )
    return plan(
        "manual_fix",
        "译文 QA 报告已失效且 Job 内没有可复用的译文字幕，只能重跑翻译重新生成报告"
        "（会重新调用翻译提供方）。",
        primary_command=(
            _job_command("translate", job_file, "--target-language", target_language)
            if target_language
            else None
        ),
    )


def build_recovery_plan(job_or_file: Job | str | Path) -> RecoveryPlan:
    """生成恢复计划。按契约只读，因此整段判定可以共享一份 checksum 记忆化。

    ``plan_advance_steps`` / ``burned_output_relink_recovery_available`` /
    ``_semantic_review_options`` / ``translation_candidate_qa_issues`` 都不写产物，
    所以作用域内看到的是同一个文件系统快照——这比现状（同一个文件在一次调用里
    被哈希两到四次，两次之间可能被改写）更自洽。
    """

    with readonly_checksum_scope():
        return _build_recovery_plan(job_or_file)


def _build_recovery_plan(job_or_file: Job | str | Path) -> RecoveryPlan:
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

    qa_blocked = job.stages[StageName.SUBTITLE_QA].status in {
        StageStatus.FAILED,
        StageStatus.AWAITING_INPUT,
    }
    if qa_blocked:
        source_report_path = valid_artifacts.get("subtitle_qa")
        translated_report_path = valid_artifacts.get("translated_subtitle_qa")
        source_report = _load_qa_report(source_report_path) if source_report_path else None
        translated_report = (
            _load_qa_report(translated_report_path) if translated_report_path else None
        )
        if _translated_qa_needs_rebuild(job):
            if workflow is None:
                # 原子 Job 上 continue 不适用，此前这里给 primary_command=None，是一条
                # 没有出口的死路；改为与原子分支共用同一条零 API 成本重建引导。
                return _atomic_translated_qa_recovery(job, job_file, target_language, plan)
            return plan(
                "retryable",
                "译文字幕 QA 报告缺失或 checksum 已失效，可以从现有译文重新执行 QA。",
                primary_command=_job_command("continue", job_file),
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
        report_kind = (
            "translation_semantic_qa_reviewed"
            if "translation_semantic_qa_reviewed" in valid_artifacts
            else "translation_semantic_qa_final"
            if "translation_semantic_qa_final" in valid_artifacts
            else "translation_semantic_qa"
        )
        reason, primary, alternatives, inputs = _semantic_review_options(
            job,
            job_file,
            acknowledgment_command=_job_command("acknowledge-translation-review", job_file),
        )
        return plan(
            "awaiting_input",
            reason,
            primary_command=primary,
            alternative_commands=alternatives,
            input_paths=inputs,
            report_paths=tuple(
                str(valid_artifacts[kind])
                for kind in ("translation_candidate_qa", report_kind)
                if kind in valid_artifacts
            ),
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

    from xvt.pipeline.retranslate import cue_retranslation_waiting

    if cue_retranslation_waiting(job):
        # 原子分支此前只看「TRANSLATE 在等输入」，把重译等待当成初译等待，给出的
        # apply-translation 命令对重译响应是错的（fingerprint 与覆盖 ID 都对不上）。
        return plan(
            "awaiting_input",
            _CUE_RETRANSLATION_WAITING_REASON,
            # apply-cue-retranslation 需要 xvt apply 拿不到的显式参数，因此不设首选命令。
            primary_command=None,
            prerequisite_commands=(_cue_retranslation_apply_command(job, job_file),),
            report_paths=(str(valid_artifacts["cue_retranslation_batches"]),),
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

    if (
        target_language
        and "translated_cues" in valid_artifacts
        and any(item.kind == "translated_subtitle_qa" for item in job.artifacts)
        and "translated_subtitle_qa" not in valid_artifacts
    ):
        # 译文 cue 仍有效，缺的只是可确定性重算的译文 QA 报告；但烧录/预览门以它为硬前置，
        # 报 complete 会让用户在「门说去修、status 说已完成」之间打转。SUBTITLE_QA stage
        # 仍是 succeeded 时走不到上面的 qa_blocked 分支，所以这条判定必须留在收尾之前。
        return _atomic_translated_qa_recovery(job, job_file, target_language, plan)

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

    from xvt.pipeline.semantic import semantic_review_satisfied
    from xvt.pipeline.translation import (
        REPAIR_POLISH_DRAFT_MANUAL_FIX_MESSAGE,
        translation_progress_rebuildable,
    )
    from xvt.workflow import burned_output_relink_recovery_available, plan_advance_steps

    steps = plan_advance_steps(job, workflow)
    if not steps:
        return plan(
            "complete",
            "当前目标已完成，可以校验已有产物。",
            primary_command=_job_command("verify", job_file),
            advance_steps=(),
        )

    if steps[0] == "await_cue_retranslation":
        return plan(
            "awaiting_input",
            _CUE_RETRANSLATION_WAITING_REASON,
            # 与原子分支同口径：apply-cue-retranslation 要显式 --cue / --target-language，
            # xvt apply 与 continue 都推不动这一步，因此不给首选命令。
            primary_command=None,
            prerequisite_commands=(_cue_retranslation_apply_command(job, job_file),),
            report_paths=(str(valid_artifacts["cue_retranslation_batches"]),),
            advance_steps=steps,
        )

    if steps[0] == "await_agent_translation":
        target = workflow.target_language or "LANGUAGE"
        return plan(
            "awaiting_input",
            "任务正在等待翻译结果回填。",
            primary_command=_job_command("continue", job_file),
            prerequisite_commands=(_job_command("apply", job_file, "RESPONSE"),),
            alternative_commands=(
                _job_command(
                    "apply-translation",
                    job_file,
                    "RESPONSE",
                    "--target-language",
                    target,
                ),
            ),
            advance_steps=steps,
        )

    if steps[0] == "await_agent_polish":
        return plan(
            "awaiting_input",
            "任务正在等待译文润色结果回填。",
            primary_command=_job_command("continue", job_file),
            prerequisite_commands=(_job_command("apply", job_file, "RESPONSE"),),
            alternative_commands=(_job_command("apply-polish-translation", job_file, "RESPONSE"),),
            advance_steps=steps,
        )

    if steps[0] == "await_agent_semantic_review":
        return plan(
            "awaiting_input",
            "任务正在等待 Agent 语义校对与一次修复结果回填。",
            primary_command=_job_command("continue", job_file),
            prerequisite_commands=(_job_command("apply", job_file, "RESPONSE"),),
            alternative_commands=(_job_command("apply-semantic-review", job_file, "RESPONSE"),),
            report_paths=tuple(
                str(valid_artifacts[kind])
                for kind in ("translation_semantic_qa", "translation_semantic_qa_final")
                if kind in valid_artifacts
            ),
            advance_steps=steps,
        )

    if steps[0] == "await_translation_review":
        reason, primary, alternatives, inputs = _semantic_review_options(
            job,
            job_file,
            acknowledgment_command=_job_command(
                "continue",
                job_file,
                "--acknowledge-translation-review",
            ),
        )
        return plan(
            "awaiting_input",
            reason,
            primary_command=primary,
            alternative_commands=alternatives,
            input_paths=inputs,
            report_paths=tuple(
                str(valid_artifacts[kind])
                for kind in (
                    "translation_candidate_qa",
                    "translation_semantic_qa_reviewed",
                    "translation_semantic_qa_final",
                    "translation_semantic_qa",
                )
                if kind in valid_artifacts
            ),
            advance_steps=steps,
        )

    if steps[0] == "repair_polish_draft":
        # 分类必须与 continue 的实际行为一致：checkpoint 可零成本重建时
        # continue 会放行，此时报 manual_fix 会误导用户；两侧共用同一谓词。
        if translation_progress_rebuildable(job, workflow):
            return plan(
                "retryable",
                "润色草稿已失效，但翻译进度 checkpoint 完整，可零 API 成本重建草稿后继续。",
                primary_command=_job_command("continue", job_file),
                advance_steps=steps,
            )
        return plan(
            "manual_fix",
            REPAIR_POLISH_DRAFT_MANUAL_FIX_MESSAGE,
            primary_command=None,
            advance_steps=steps,
        )

    # 不能只看 ``steps[0]``：译文 QA 报告单独失效时计划会变成 ("render", "burn")，
    # 按首元素匹配会丢掉 relink 导航，把本可零成本重新关联的成品引导成完整重编码。
    # standard 质量下 render 只从现有 cue 重算 QA 与字幕文件、不改写 translated_cues，
    # 因此烧录派生指纹仍然成立，relink 依旧有效（relink 命令本身也不依赖 QA/渲染产物）。
    # 其余会改写 translated_cues 的前置步骤（translate/polish/semantic_review）不放行，
    # 那些情况下重新关联的成品随后必然被重烧录取代。
    # polished 质量另有一条会改写 translated_cues 的 render：语义校对已 approved 但
    # finalize 中途断掉时，计划同样是 ("render", "burn")，而 continue 走的是
    # ``_finalize_candidate`` —— 它发现 translated_cues 与最终候选不一致就会改写它，
    # 指纹随即失效、relink 白做。因此 polished 额外要求「候选已落到 translated_cues」
    # （``semantic_review_satisfied``），此时 render 只补渲染产物，与 standard 同构。
    relink_reachable = "burn" in steps and set(steps) <= {"render", "burn"}
    if relink_reachable and workflow.translation_quality == "polished":
        relink_reachable = semantic_review_satisfied(job)
    if relink_reachable and burned_output_relink_recovery_available(
        job,
        workflow,
        burned_video_valid="burned_video" in valid_artifacts,
    ):
        return plan(
            "awaiting_input",
            "烧录成品可能可重新关联；执行命令时会再次校验源媒体内容。",
            primary_command=_job_command(
                "relink-burned-output",
                job_file,
                "EXISTING_VIDEO",
            ),
            alternative_commands=(_job_command("continue", job_file),),
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
    if media_qa_status is not StageStatus.SUCCEEDED:
        return "烧录尚未通过媒体 QA，可以重新烧录并检查输出视频。"
    if "burned_video" not in valid_artifacts:
        return (
            "媒体 QA 已通过，但烧录成品已不在登记路径或内容已变化（checksum 失效）；"
            "若已自行保存成品可忽略，需要重新产出时运行 continue。"
        )
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

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

from xvt.candidate_qa import overflow_limits, translation_candidate_qa_issues
from xvt.checksums import file_checksum as _hash_file
from xvt.config import DEFAULT_MIN_ALIGNMENT_CONFIDENCE
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

    文件元数据在这里只用于给"同一次判定内的同一个文件"定身份：作用域内首次遇到
    某个 ``(path, st_dev, st_ino, st_size, st_mtime_ns)`` 才真正读文件算哈希，
    之后复用该结果。这是有意的记忆化，不是内容校验的替代品。

    因此有一条已知且被接受的边界：如果在同一个只读作用域执行期间（毫秒级）把文件
    改成等长内容、并把 ``st_mtime_ns`` 恢复成原值，缓存会复用旧哈希。这与 git 的
    racy-timestamp 取舍同类。``build_recovery_plan`` 本身严格只读，但判定结果会被
    调用方消费：``batch._record_recovery`` 会把它写进 ``item["recovery_plan"]``，
    批处理据此把条目落成 ``completed``，所以这个窗口里的误判确实可能进入
    ``batch.json`` 这一持久状态。接受它的理由是：(i) 利用该窗口需要外部写入者在
    xvt 持有 job 锁期间，恰好命中一次 ``build_recovery_plan`` 调用内、该文件首次
    哈希之后的亚毫秒缝隙；(ii) 同样的坏状态在没有缓存时也能达成——「计划返回后、
    条目落盘前」是一个更宽的 TOCTOU 窗口，缓存不是根因；(iii) 逃生口是
    ``xvt verify JOB``，它在本作用域之外重新哈希，必然识破。要绕开记忆化，不进入
    本作用域即可（``file_checksum`` 无作用域时逐字等价于
    ``xvt.checksums.file_checksum``）。
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

    **polished Job 上不可行**：``import-translation`` 同样经 ``finalize_translation``，而
    那里在 ``resolve_translation_quality(job) == "polished"`` 且 ``polished_result`` 为假时
    走 draft 分支——只写出一份新的润色草稿，QA 不重建、渲染不执行，润色候选与语义校对结论
    连同最终译文一并失效，整条润色腿要重跑（真实模型成本）。承诺「零 API 成本重建 QA 与
    渲染产物」在这种 Job 上是假的，因此这里返回 None，让调用方落回各自既有的引导。
    """

    if resolve_translation_quality(job) == "polished":
        return None
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
        shlex.quote(target_language),
    )


_CUE_RETRANSLATION_WAITING_REASON = (
    "任务正在等待局部重译（retranslate-cues）结果回填；未回填前不要按已完成处理。"
    "要放弃这次重译，重新运行一轮完整翻译（xvt translate JOB --target-language LANG）"
    "即可取代它：准备新批次时会把这份重译 manifest 移出登记。continue 本身会停在这次"
    "等待上，不会替你放弃。"
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


def _cue_retranslation_waiting_plan(
    job: Job,
    job_file: Path,
    valid_artifacts: dict[str, Path],
    plan: Callable[..., RecoveryPlan],
    *,
    advance_steps: tuple[str, ...] = (),
) -> RecoveryPlan:
    """局部重译等待的统一恢复计划；原子分支与端到端分支共用同一份文案与命令。

    ``apply-cue-retranslation`` 需要 ``xvt apply`` 拿不到的 ``--cue`` / ``--target-language``，
    ``continue`` 也推不动这一步，因此不设首选命令，只给拼好的前置命令。
    """

    return plan(
        "awaiting_input",
        _CUE_RETRANSLATION_WAITING_REASON,
        primary_command=None,
        prerequisite_commands=(_cue_retranslation_apply_command(job, job_file),),
        report_paths=(str(valid_artifacts["cue_retranslation_batches"]),),
        advance_steps=advance_steps,
    )


_QA_BLOCKER_OVERFLOW_CODE = "bilingual_overflow_risk"
_QA_BLOCKER_LABELS = {
    "missing_translation": "缺少译文",
    "glossary_violation": "违反术语表",
}
_QA_BLOCKER_CODE_ORDER = (
    _QA_BLOCKER_OVERFLOW_CODE,
    "missing_translation",
    "glossary_violation",
)
_QA_BLOCKER_TAIL = "这类问题不能通过语义确认绕过，也不会触发重新翻译或润色。"
_QA_BLOCKER_FALLBACK_REASON = (
    "候选译文的确定性 QA 仍有可修复问题；请修改译文模板后继续。" + _QA_BLOCKER_TAIL
)


def _candidate_qa_blocker_reason(issues: tuple[dict[str, Any], ...]) -> str:
    """把候选译文 QA 的阻断项渲染成点名 cue 的等待原因；严格只读，不碰文件。"""

    grouped: dict[str, list[str]] = {}
    for issue in issues:
        code = str(issue.get("code"))
        cue_id = issue.get("cue_id")
        item = str(cue_id)
        if code == _QA_BLOCKER_OVERFLOW_CODE:
            # 宽度与上限只出现在 message 里；提取不到就退回只列 cue id，绝不猜数字。
            limits = overflow_limits(str(issue.get("message", "")))
            if limits is not None:
                unit = " ASS 像素" if "ASS pixels" in str(issue.get("message", "")) else ""
                item = f"{cue_id}（{limits[0]}/{limits[1]}{unit}）"
        entries = grouped.setdefault(code, [])
        if item not in entries:
            entries.append(item)

    codes = [code for code in _QA_BLOCKER_CODE_ORDER if code in grouped]
    codes.extend(sorted(code for code in grouped if code not in _QA_BLOCKER_CODE_ORDER))
    clauses = []
    for code in codes:
        entries = grouped[code]
        if code == _QA_BLOCKER_OVERFLOW_CODE:
            label = "行宽超限"
        elif code in _QA_BLOCKER_LABELS:
            label = _QA_BLOCKER_LABELS[code]
        else:
            label = f"未通过 {code} 检查"
        clauses.append((code, f"{len(entries)} 条 cue {label}：{'、'.join(entries)}"))
    if not clauses:
        return _QA_BLOCKER_FALLBACK_REASON

    head_code, head_clause = clauses[0]
    action = (
        "请缩短这些译文后继续" if head_code == _QA_BLOCKER_OVERFLOW_CODE else "请修改译文模板后继续"
    )
    parts = [f"候选译文有 {head_clause}；{action}。"]
    parts.extend(f"另有 {clause}。" for _, clause in clauses[1:])
    parts.append(_QA_BLOCKER_TAIL)
    return "".join(parts)


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
    # `xvt apply ... --continue` 只在 Job 持久化了端到端 workflow 目标时可用
    # （见 cli.py 的 `--continue requires a persisted end-to-end workflow goal`）。
    # 判定放在函数内部而不是让调用方传旗标：与 CLI 校验读同一个事实来源，
    # 新增调用点也不会因为漏传参数而推荐一条必然失败的命令。
    allow_continue = get_workflow_config(job) is not None
    if translation_input is None:
        translation_command = _job_command("continue", job_file)
    elif allow_continue:
        translation_command = _job_command(
            "apply",
            job_file,
            shlex.quote(str(translation_input)),
            "--continue",
        )
    else:
        translation_command = _job_command(
            "apply-translation-review-fixes",
            job_file,
            shlex.quote(str(translation_input)),
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
            _candidate_qa_blocker_reason(qa_issues),
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


def _alignment_confidence_deadlock(job: Job, valid_artifacts: dict[str, Path]) -> bool:
    """强制对齐的置信度门是否已经把这个 Job 锁死在「continue 必然再次失败」上。

    判定只看 Job 状态，不猜异常：``--align-subtitles`` 与置信度门都固化在 ``job.config``
    里，重跑时拿的是同一份 ``input_subtitles`` 与同一份 ``word_transcript``，对齐是确定性的，
    因此登记的置信度低于门限就意味着下一次 ``continue`` 会撞上同一个退出码 2。

    两份对齐输入都必须是 checksum 有效的：缺了 ``word_transcript`` 时重跑会重新转写，
    置信度可能不同，那种 Job 仍按既有的「从转写 checkpoint 继续」处理，不能被断言成必然失败。
    """

    if job.config.get("input_kind") == "subtitle":
        return False
    if job.config.get("align_subtitles") is not True:
        return False
    if job.config.get("allow_low_confidence_alignment", False):
        return False
    alignment_path = valid_artifacts.get("subtitle_alignment")
    if alignment_path is None:
        return False
    if not {"word_transcript", "input_subtitles"} <= valid_artifacts.keys():
        return False
    minimum = job.config.get("minimum_alignment_confidence", DEFAULT_MIN_ALIGNMENT_CONFIDENCE)
    if isinstance(minimum, bool) or not isinstance(minimum, int | float):
        return False
    try:
        alignment = json.loads(alignment_path.read_text(encoding="utf-8"))
        confidence = alignment["confidence"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        return False
    if isinstance(confidence, bool) or not isinstance(confidence, int | float):
        return False
    return float(confidence) < float(minimum)


def _alignment_post_qa_deadlock(job: Job, valid_artifacts: dict[str, Path]) -> Path | None:
    """强制对齐的**对齐后字幕 QA error 门**是否已经把 Job 锁死在「continue 必然再次失败」上。

    成立时返回那份登记在册的 ``aligned_subtitle_qa`` 报告路径（供 ``report_paths`` 与
    文案点名），否则返回 ``None``。

    与上一条（置信度门）同一范式：只看 Job 状态，不猜运行期异常。``--align-subtitles``
    固化在 ``job.config`` 里，重跑拿的是同一份 ``input_subtitles`` 与同一份
    ``word_transcript``，对齐与对齐后 QA 都是确定性的，因此登记的报告里有 error 级 issue
    就意味着下一次 ``continue`` 会撞上同一个退出码 2（实测连跑两次 stderr 逐字相同）。

    两条与置信度门刻意不同的守卫：

    - **不看** ``allow_low_confidence_alignment``。那个旗标只放行「低置信但 QA 干净」的
      对齐，对齐后 QA 的 error 门在它之后无条件执行（见 ``pipeline.transcription``），
      照抄那条守卫会让带旗标的 Job 继续卡在必然失败的 ``continue`` 上。
    - 判据来自 ``aligned_subtitle_qa`` 这份报告本身，不是 ``subtitle_alignment``：
      挡住对齐的就是报告里的 error 级 issue，而 ``pipeline.transcription`` 在抛出之前
      就无条件落盘并登记这份报告，所以失败的 Job 上一定有它。

    两份对齐输入仍然都必须 checksum 有效：缺了 ``word_transcript`` 时重跑会重新转写，
    对齐结果可能不同，那种 Job 不能被断言成必然失败。

    最后一条守卫比置信度门**多一道**：还要求 ``_source_can_be_rebuilt``。本条的唯一出口
    就是 ``rebuild-source --strategy asr``，而 ``media_info`` 失效且本地媒体也不在盘上时
    那条命令自己会以「source rebuild requires an available local media file」失败——
    推荐一条跑不起来的命令正是这条判定要修的毛病，所以这里不留这个角。不成立时让既有
    分类原样生效（那种 Job 的既有去向与本改动之前完全相同）。
    """

    if job.config.get("input_kind") == "subtitle":
        return None
    if job.config.get("align_subtitles") is not True:
        # 自动策略下对齐后 QA 失败会回落 ASR 并正常产出 source_cues，不是死局。
        return None
    report_path = valid_artifacts.get("aligned_subtitle_qa")
    if report_path is None:
        return None
    if not {"word_transcript", "input_subtitles"} <= valid_artifacts.keys():
        return None
    report = _load_qa_report(report_path)
    if report is None or not _qa_report_has_errors(report):
        return None
    if not _source_can_be_rebuilt(job, valid_artifacts):
        return None
    return report_path


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
                _job_command(
                    "translate", job_file, "--target-language", shlex.quote(target_language)
                ),
            ),
        )
    return plan(
        "manual_fix",
        "译文 QA 报告已失效且 Job 内没有可复用的译文字幕，只能重跑翻译重新生成报告"
        "（会重新调用翻译提供方）。",
        primary_command=(
            _job_command("translate", job_file, "--target-language", shlex.quote(target_language))
            if target_language
            else None
        ),
    )


def build_recovery_plan(job_or_file: Job | str | Path) -> RecoveryPlan:
    """生成恢复计划。按契约只读，因此整段判定可以共享一份 checksum 记忆化。

    ``plan_advance_steps`` / ``burned_output_relink_recovery_available`` /
    ``_semantic_review_options`` / ``translation_candidate_qa_issues`` 都不写产物，
    因此这里做记忆化是安全的：同一个文件在一次调用里不再被哈希两到四次，也就不会
    在两次哈希之间被改写而给出自相矛盾的判定。

    记忆化只覆盖经本模块 ``file_checksum`` 的调用点。``candidate_qa`` 直接用
    ``checksums.file_checksum``，不进这份缓存，因此它看到的是自己那一刻的文件内容。
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
        if _alignment_confidence_deadlock(job, valid_artifacts):
            # 必须排在下面两条 retryable 之前：对齐输入与门限都固化在 job.config 里，
            # 重跑拿的是同一份字幕与同一份转写，`continue` / `resume` 只会复现同一个
            # 退出码 2。此前这里给 retryable + continue，且 alternative 为空，用户照
            # status 的提示反复重跑同一条注定失败的命令，没有任何出口。
            return plan(
                "manual_fix",
                "强制对齐的置信度低于门限，重跑同一份字幕与转写只会得到同一个结果；"
                "改用 ASR 重建源时间轴，或重新运行 run 调整对齐选项。",
                primary_command=_job_command("rebuild-source", job_file, "--strategy", "asr"),
            )
        aligned_report_path = _alignment_post_qa_deadlock(job, valid_artifacts)
        if aligned_report_path is not None:
            # 强制对齐的第二道门。必须排在置信度门**之后**：两道门可以在同一个 Job 上
            # 同时成立（置信度低且对齐后 QA 也有 error），而执行侧先抛置信度门，文案要
            # 对得上先失败的那一条。也必须排在下面两条 retryable 之前，否则又是一条
            # 「推荐必然失败的 continue」。出口只有一条真的能推进：rebuild-source 用
            # ASR 重建时间轴（实测退出码 0，之后 Job 能走到 complete、verify 全项 PASS）。
            # 想保留用户字幕的文本与原时间轴只能去掉 --align-subtitles 重新 run，那会
            # 创建新 Job、推不动这一个，因此不进推荐位，只在文案里说明。
            return plan(
                "manual_fix",
                "强制对齐的结果没过对齐后字幕 QA（有 error 级问题，明细见 "
                "aligned-subtitle-qa.json），重跑同一份字幕与转写只会得到同一个结果；"
                "改用 ASR 重建源时间轴，或去掉 --align-subtitles（保留源字幕原时间轴）"
                "、修好源字幕后重新运行 run。",
                primary_command=_job_command("rebuild-source", job_file, "--strategy", "asr"),
                report_paths=(str(aligned_report_path),),
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

    from xvt.pipeline.retranslate import cue_retranslation_waiting

    # 延迟导入，避开 workflow → recovery → workflow 的循环。下面原子 Job 的「等待优先」让路
    # （qa_blocked、「润色候选有效」，以及与 newest 组合的语义等待让路）共用这一份谓词。
    from xvt.workflow import agent_full_translation_waiting, full_translation_request_is_newest

    # 必须排在 qa_blocked 之前。术语死锁（阻断的 error 全是 glossary_violation）的出口
    # 就是 retranslate-cues，用户按引导跑完 Agent 模式的重译后 SUBTITLE_QA 仍停在
    # FAILED——译文 QA 要等回填后才重跑。排在后面时 qa_blocked 分支会把这段等待重报成
    # manual_fix 并再次给出同一条 retranslate-cues（用户已经跑过了），端到端 Job 上
    # advance_steps 还会与 plan_advance_steps 的 ('await_cue_retranslation',) 自相矛盾。
    # 重译等待正是这次 QA 阻断的处置进行时，先报等待才是真状态。
    if cue_retranslation_waiting(job):
        if workflow is not None:
            # plan_advance_steps 的函数体第一件事就是同一个谓词，因此这里必然落到
            # await_cue_retranslation 分支，advance_steps 与 continue 保持一致。
            return _recovery_for_workflow(
                job,
                job_file=job_file,
                workflow=workflow,
                valid_artifacts=valid_artifacts,
                plan=plan,
            )
        return _cue_retranslation_waiting_plan(job, job_file, valid_artifacts, plan)

    qa_blocked = job.stages[StageName.SUBTITLE_QA].status in {
        StageStatus.FAILED,
        StageStatus.AWAITING_INPUT,
    }
    if qa_blocked and workflow is not None:
        # 与上面「重译等待优先」同构的第二条让路。SUBTITLE_QA 停在 FAILED 只说明「上一次
        # 译文 QA 没过」，它不会因为随后开了一次新的 Agent 等待而自动复位：
        # prepare_agent_translation 移走重译 manifest、写入新的 translation_batches 并置
        # TRANSLATE=awaiting_input，却不碰 SUBTITLE_QA。此时按 qa_blocked 处理，出口会指向
        # 已经被移出登记的 retranslate-cues manifest，而统一 xvt apply 拿不到
        # await_agent_translation 路由，用户同样无路可走。planner 首步是正式等待步骤时，
        # 等待才是真状态；分类与 continue 也由此保持一致。
        from xvt.workflow import AWAIT_STEPS, plan_advance_steps

        workflow_steps = plan_advance_steps(job, workflow)
        if workflow_steps and workflow_steps[0] in AWAIT_STEPS:
            return _recovery_for_workflow(
                job,
                job_file=job_file,
                workflow=workflow,
                valid_artifacts=valid_artifacts,
                plan=plan,
            )
    if qa_blocked and workflow is None:
        # 原子 Job 上的同一条让路。``plan_advance_steps`` 是 workflow 专用，这里改用它内部
        # 的同一份谓词：TRANSLATE 停在等待态且批次 manifest checksum 有效，说明用户已经改走
        # `translate --provider agent` 开了新的整轮请求。继续按 qa_blocked 处理会推荐
        # retranslate-cues，照做会盖掉刚写入的整轮 manifest。放行后由下面既有的原子等待
        # 分支给出 apply-translation 引导；manifest 已经不在时（等待态残留）不放行，
        # QA 引导照旧生效。
        if agent_full_translation_waiting(job):
            qa_blocked = False
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
        agent_translate_and_polish_progress_ready,
        agent_translate_and_polish_started,
        agent_translate_and_polish_waiting,
    )

    # 语义等待让路：用户在语义等待态上又开了一轮整轮 Agent 翻译时，语义报告仍然 checksum
    # 有效，但它挂着的是已被取代的那份候选；照 apply-semantic-review 回填会静默作废新一轮，
    # 并留下残留死循环。必须同时要求「整轮请求是最新的有效在途请求」：正常的语义等待上
    # `agent_full_translation_waiting` 恒为真（manifest 有效 + TRANSLATE 仍 awaiting），只用
    # 这个裸谓词会把正常语义等待也让掉，推荐 apply-translation 重放初译后回到一模一样的状态
    # （零推进死循环）。与 plan_advance_steps 满足度早退闸门是同一份组合判定。
    # 注意下面「润色候选有效」那道闸门必须保持裸谓词，不能也加 newest：语义报告失效的形态里
    # 登记次序上 semantic_qa 仍在 batches 之后，加了会倒退。
    full_round_supersedes_semantic = agent_full_translation_waiting(
        job
    ) and full_translation_request_is_newest(job)

    if not full_round_supersedes_semantic and agent_semantic_review_waiting(job):
        return plan(
            "awaiting_input",
            "任务正在等待 Agent 语义校对与一次修复结果回填。",
            prerequisite_commands=(_job_command("apply-semantic-review", job_file, "RESPONSE"),),
            report_paths=(str(valid_artifacts["translation_semantic_qa"]),),
        )

    if not full_round_supersedes_semantic and semantic_review_waiting(job):
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

    # 第三条「等待优先」让路，与上面 qa_blocked 的那条同一个判据、同一份谓词。
    # prepare_agent_translation 只把 TRANSLATE 置为等待，不失效 translation_polished_cues，
    # 所以「润色候选有效」与「整轮 Agent 翻译正在等回填」在同一个状态上同时成立。本分支排在
    # 原子等待分支之前，不让路就会推荐 `translate --translation-quality polished`：Agent 模式
    # 下它只是把同一份 manifest 原样重写，状态与推荐逐轮不变（零推进的死循环），而用户当初
    # 显式传过 `--batch-size`（或 `--glossary`）时，重写还会改掉 manifest 的 batch size 与
    # fingerprint——手里那份准备好的响应被 `apply-translation` 的指纹校验拒收，一次本可回填
    # 的等待变成要重新回填。让路后由下面既有的原子等待分支给出 apply-translation 引导；
    # manifest 已经不在的等待态残留不让路（谓词的第二个条件），本分支照旧生效。
    if (
        job.config.get("translation_quality") == "polished"
        and "translation_polished_cues" in valid_artifacts
        and not semantic_review_approved(job)
        and not agent_full_translation_waiting(job)
    ):
        target = shlex.quote(target_language) if target_language else "LANGUAGE"
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

    # 局部重译等待（原子分支此前把它当成初译等待，给出的 apply-translation 命令对重译
    # 响应是错的）已在 qa_blocked 之前统一判定，这里不必再判一次。

    agent_waiting = agent_translate_and_polish_waiting(job)
    agent_progress = agent_translate_and_polish_progress_ready(job)
    invalid_polished_agent = (
        job.config.get("translation_quality") == "polished"
        and agent_translate_and_polish_started(job)
        and not agent_waiting
        and not agent_progress
    )
    if not invalid_polished_agent and (
        job.stages[StageName.TRANSLATE].status is StageStatus.AWAITING_INPUT
        or agent_waiting
        or agent_progress
    ):
        target = shlex.quote(target_language) if target_language else "LANGUAGE"
        if valid_artifact_path(job, "translation_batches") is None:
            # 等待态残留：批次 manifest 已经不在（例如按旧文案手工删掉重译 manifest 后
            # TRANSLATE 仍停在 awaiting_input）。此时建议 apply-translation 等于让用户
            # 回填一份根本不存在的请求的响应，必然被 fingerprint 校验拒绝。
            return plan(
                "retryable",
                "翻译阶段停在等待态，但 Job 内已经没有 checksum 有效的翻译批次 manifest，"
                "没有任何响应能回填这次等待；重新运行翻译会重建批次并给出新的回填请求。",
                primary_command=_job_command(
                    "translate",
                    job_file,
                    "--target-language",
                    target,
                ),
            )
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
                shlex.quote(target_language),
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


def _repair_polish_draft_manual_fix(
    job: Job,
    *,
    job_file: Path,
    workflow: WorkflowConfig,
    valid_artifacts: dict[str, Path],
    plan: Callable[..., RecoveryPlan],
    steps: tuple[str, ...],
    diagnosis: str,
) -> RecoveryPlan:
    """``repair_polish_draft`` 无法零成本重建时的 manual_fix 导航。

    ``diagnosis`` 是 status 与 continue 共用的那句诊断（``REPAIR_POLISH_DRAFT_
    MANUAL_FIX_MESSAGE``），两边逐字一致；本函数只在它后面接一句**导航**。

    那句诊断自己说的「请重新导入……原始译文草稿」就是 ``import-translation``：
    polished Job 上它经 ``finalize_translation`` 的 draft 分支，把回填结果写成
    ``translation-draft-cues.json`` 并重新登记 ``translation_draft_cues``，于是
    planner 从 ``repair_polish_draft`` 回到 ``polish``。此前这条出口存在但恢复
    计划没接上：``primary_command=None`` 且 ``alternative_commands`` 为空，人类
    可读输出只剩一句「after repairs 再看 status」，既没命令也没路径。

    **只认 checksum 有效的 ``translated_srt``**（约束 9：产物有效性只认内容
    checksum）。``_reset_for_polish`` 只是把下游产物**移出登记**，物理文件往往
    还躺在 artifacts 目录里——照「通常还在」去推荐，就是再推荐一条可能指向不
    存在文件的命令。没有有效字幕时宁可不给 ``primary_command``，只把那个缺失或
    已变化的草稿文件点名出来，让用户知道要放回哪个文件。

    ``translated_srt`` 是对的那个 kind：它是用户可见的**纯译文**字幕，cue ID 与
    时间轴和 ``source_cues`` 一一对应（``_translation_satisfied`` 也拿它当译文
    交付物之一）；``bilingual_srt`` 掺着原文，导入回去会污染译文文本。

    ``alternative_commands`` 固定给整轮重译，并在 reason 里讲明它连初译一起重做
    ——openai-compatible 模式下那是真实 API 成本。``continue``（退出码 4）与
    ``polish-translation``（退出码 2）在这个状态下必然失败，任何推荐位都不放。
    """

    target_language = workflow.target_language
    alternatives: tuple[str, ...] = ()
    if target_language:
        alternatives = (
            _job_command(
                "translate",
                job_file,
                "--target-language",
                shlex.quote(target_language),
                "--provider",
                shlex.quote(workflow.provider),
            ),
        )

    translated_srt = valid_artifacts.get("translated_srt")
    if translated_srt is not None and target_language:
        return plan(
            "manual_fix",
            f"{diagnosis}"
            "Job 内仍有 checksum 有效的译文字幕，可以把它重新导入为译文草稿；"
            "该命令会把这份字幕登记为导入译文，并失效现有译文与语义校对结论，"
            "润色腿要重跑（openai-compatible 模式下是真实 API 成本）。"
            "备选的整轮重译会连初译一起重做，成本更高。",
            primary_command=_job_command(
                "import-translation",
                job_file,
                shlex.quote(str(translated_srt)),
                "--target-language",
                shlex.quote(target_language),
            ),
            alternative_commands=alternatives,
            advance_steps=steps,
        )

    draft = next(
        (artifact for artifact in job.artifacts if artifact.kind == "translation_draft_cues"),
        None,
    )
    missing = (
        f"缺失或内容已变化的草稿文件是 {draft.path}，把它放回原处后重新运行 xvt status；"
        if draft is not None
        else ""
    )
    return plan(
        "manual_fix",
        f"{diagnosis}"
        f"Job 内没有 checksum 有效的译文字幕可以重新导入；{missing}"
        "备选是整轮重译，它会重新调用翻译提供方并重跑润色腿。",
        primary_command=None,
        alternative_commands=alternatives,
        advance_steps=steps,
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

    from xvt.pipeline.common import pending_translation_import_blocker
    from xvt.pipeline.semantic import semantic_review_satisfied
    from xvt.pipeline.translation import (
        REPAIR_POLISH_DRAFT_MANUAL_FIX_MESSAGE,
        translation_progress_rebuildable,
    )
    from xvt.workflow import (
        burned_output_relink_recovery_available,
        media_qa_failure,
        missing_burn_source_media,
        plan_advance_steps,
        source_media_relink_available,
    )

    steps = plan_advance_steps(job, workflow)
    if not steps:
        return plan(
            "complete",
            "当前目标已完成，可以校验已有产物。",
            primary_command=_job_command("verify", job_file),
            advance_steps=(),
        )

    if steps[0] == "await_cue_retranslation":
        return _cue_retranslation_waiting_plan(
            job,
            job_file,
            valid_artifacts,
            plan,
            advance_steps=steps,
        )

    if steps[0] == "await_agent_translation":
        target = shlex.quote(workflow.target_language) if workflow.target_language else "LANGUAGE"
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
        return _repair_polish_draft_manual_fix(
            job,
            job_file=job_file,
            workflow=workflow,
            valid_artifacts=valid_artifacts,
            plan=plan,
            steps=steps,
            diagnosis=REPAIR_POLISH_DRAFT_MANUAL_FIX_MESSAGE,
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
    # 源媒体不在登记路径时 render 与 burn 都会以「video file does not exist」失败
    # （两步都要读实际媒体文件），于是 primary_command 还推荐 continue 就是推荐一条
    # **必然再次失败**的命令，而 alternative_commands 是空的——用户没有任何出口。
    # 这是本仓库第 6 次同类缺陷，归 manual_fix 并点名那个路径。
    # 出口有两条：把源媒体原样放回登记路径，或者（媒体是永久搬家时）用
    # `xvt relink-source-media JOB NEW_MEDIA_PATH` 把持久化路径指过去——它只认与 v2
    # proof 登记的 source_media_checksum 相同的文件。
    # 后者**只在它真的会成功时**才进推荐位：命令本身要求 checksum 有效的 v2 proof，
    # 拿不到就以退出码 2 失败，所以这里用 workflow 侧的 `source_media_relink_available`
    # 把关（legacy fingerprint 的旧 Job 维持原样：不给首选命令，只点名那个路径）。
    # 判定刻意留在 workflow 侧而不是在这里重造：只暴露 `_burn_proof` 会让「有没有烧录
    # 目标」「是不是远程 Job」两条前置条件在调用点各写一遍，命令收紧时导航不跟着动。
    # 两支都不承诺「放回去就不必 continue」：快路径要 `{path,size,mtime_ns}` 全等，
    # mtime 变了得走内容 checksum 回退，而存量 proof 缺 source_media_identity 时回退
    # 拿不到授权、Job 仍落到 burn。因此统一让用户之后重新运行 `xvt status` 看下一步。
    # 刻意**不**推荐 `xvt burn JOB --media NEW`：它虽然能产出合格成品，却不改持久化
    # 路径，Job 会继续停在这里，等于把用户引进另一个死角。
    # 判定范围刻意收窄到「计划只剩 render / burn」：还有 translate 等步骤时 continue
    # 仍能真正推进那些步骤，不该拦。``local_only`` 同理：远程 Job 的下载产物缺失时
    # continue 能重新获取媒体，那是真出口，不归 manual_fix。
    burn_is_the_rest = "burn" in steps and set(steps) <= {"render", "burn"}
    if burn_is_the_rest:
        missing_media = missing_burn_source_media(job, workflow, local_only=True)
        if missing_media is not None:
            if source_media_relink_available(job, workflow):
                return plan(
                    "manual_fix",
                    f"源媒体不在登记路径 {missing_media}，render 与 burn 都要读它；"
                    "把该文件原样放回该路径，或用 relink-source-media 把持久化路径指到"
                    "新位置（只接受内容与原媒体完全相同的文件）；之后重新运行 xvt status "
                    "查看下一步。",
                    primary_command=_job_command(
                        "relink-source-media",
                        job_file,
                        "NEW_MEDIA_PATH",
                    ),
                    advance_steps=steps,
                )
            return plan(
                "manual_fix",
                f"源媒体不在登记路径 {missing_media}，render 与 burn 都要读它；"
                "把该文件原样放回该路径，之后重新运行 xvt status 查看下一步。",
                primary_command=None,
                advance_steps=steps,
            )

    relink_reachable = burn_is_the_rest
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
        # 这一步不一定会调用翻译提供方：Job 内留着已存译文时 `_ensure_translation`
        # 会把它重新套到当前源 cue 上。套不回去时 `continue` 必然在同一处失败且
        # 一个字节都不改 Job，继续给 retryable + continue 就是推荐一条注定失败的
        # 命令（第 7 次同类缺陷）。谓词与执行侧同一份实现，见
        # `pipeline.common.pending_translation_import_blocker`。
        blocker = pending_translation_import_blocker(job, workflow)
        if blocker is not None:
            return _pending_translation_import_manual_fix(
                job_file=job_file,
                workflow=workflow,
                plan=plan,
                steps=steps,
                diagnosis=blocker,
            )
        reason = "源字幕有效，可以继续未完成的翻译阶段。"
    elif "polish" in steps:
        reason = "译文草稿有效，可以继续未完成的润色阶段。"
    elif "semantic_review" in steps:
        reason = "润色候选有效，可以继续未完成的语义校对阶段。"
    elif "render" in steps:
        reason = "译文 cue 已就绪，可以补齐字幕渲染产物后继续。"
    elif "burn" in steps:
        reason = _burn_recovery_reason(valid_artifacts, media_qa_blocker=media_qa_failure(job))
    else:
        reason = "存在未完成的工作流步骤，可以继续。"

    return plan(
        "retryable",
        reason,
        primary_command=_job_command("continue", job_file),
        advance_steps=steps,
    )


def _pending_translation_import_manual_fix(
    *,
    job_file: Path,
    workflow: WorkflowConfig,
    plan: Callable[..., RecoveryPlan],
    steps: tuple[str, ...],
    diagnosis: str,
) -> RecoveryPlan:
    """已存译文套不回当前源 cue 时的 manual_fix 导航。

    ``diagnosis`` 是 ``status`` 与 ``continue`` 共用的那句诊断（由
    ``pipeline.common.pending_translation_import_blocker`` 产出，两边逐字一致）；
    本函数只在它后面接一句**导航**，与 ``_repair_polish_draft_manual_fix`` 同构。

    两条出口都经过实测，但不是等价的：

    - 首选 ``import-translation``：导入一份对齐当前源 cue 的译文后 Job 走到
      ``completed``，而且 ``add_artifact`` 按 kind 去重，这一次导入会**顶掉**那份
      错位的 ``input_translation_subtitles`` 登记——死局本身被拆掉。
    - 备选 ``drop-imported-translation``：用户不打算再用这份译文时的**诚实**出口。
      它把两处来源一起清掉（Job 内副本的登记 + 持久化目标里的
      ``translation_subtitles``，文件都保留）并带走整个失效闭包，随后 ``continue``
      按常规路径重新翻译。排在整轮重译之前，因为它拆掉了复发的地雷。
    - 备选整轮重译：同样能走到 ``completed``，但它**只是绕过**。错位的
      ``input_translation_subtitles`` 与 ``config.workflow.translation_subtitles``
      原样留着，再跑一次 ``rebuild-source`` 会在同一处复发，所以代价必须讲明。

    ``continue`` 必然再次失败，任何推荐位都不放。``resegment-translation`` 也不是
    出口：它换的是源 cue 的切分，解决不了「这份译文对不上」；持久化了
    ``workflow.translation_subtitles`` 的 Job 上它还有一道配置级闸门，恒以退出码 2
    拒绝。

    分类选 ``manual_fix`` 而不是 ``awaiting_input``：这不是「等一份可回填的输入」，
    而且 ``batch`` 把 ``manual_fix`` 归入「绝不自动推进」并记 ``failed``，
    ``batch-continue`` 因此不再每轮重撞同一个失败。
    """

    # 谓词只在有 target_language 时出声，所以这里两条命令总能拼出来；守卫留着是因为
    # 两者都必须带 --target-language，少了它推荐出去就是一条跑不通的命令。
    target_language = workflow.target_language
    primary: str | None = None
    # 放弃这份译文不需要知道目标语言，所以它不在下面的 target_language 守卫里。
    alternatives: tuple[str, ...] = (_job_command("drop-imported-translation", job_file),)
    if target_language:
        primary = _job_command(
            "import-translation",
            job_file,
            "TRANSLATED_SUBTITLE",
            "--target-language",
            shlex.quote(target_language),
        )
        alternatives = (
            *alternatives,
            _job_command(
                "translate",
                job_file,
                "--target-language",
                shlex.quote(target_language),
                "--provider",
                shlex.quote(workflow.provider),
            ),
        )
    return plan(
        "manual_fix",
        f"{diagnosis}"
        "导入一份对齐当前源 cue 时间轴的译文字幕即可继续，"
        "这次导入会按 kind 顶掉 Job 里那份错位的导入译文登记，死局随之拆除。"
        "不打算再用这份译文时，drop-imported-translation 会同时清掉 Job 内副本的登记与"
        "持久化目标里的 translation_subtitles（两份文件都保留），之后由 continue 重新翻译。"
        "直接整轮重译也能跑完，但它只是绕过：两处来源原样留着，下一次 rebuild-source "
        "会在同一处复发（openai-compatible 模式下它还是真实 API 成本）。",
        primary_command=primary,
        alternative_commands=alternatives,
        advance_steps=steps,
    )


def _burn_recovery_reason(valid_artifacts: dict[str, Path], *, media_qa_blocker: str | None) -> str:
    """烧录未完成时的人类可读原因。

    ``media_qa_blocker`` 由调用方用 ``workflow.media_qa_failure`` 算出，这里**不再**
    自己重写 stage 判定（那是收敛前「媒体 QA 过了」的第四种口径）。

    刻意只在 ``"stage"`` 这一个标签上分叉：本函数是文案排序，不是放行判定。报告
    侧的三类失败（``missing`` / ``unreadable`` / ``status``）继续沿用下面的成品与
    指纹文案——它们对用户更具体，而且 ``_burn_satisfied`` 早已独立地把这些状态判成
    未完成，不存在误报完成的风险。要改这个排序属于用户可见行为变更，应单独决策。
    """

    if media_qa_blocker == "stage":
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

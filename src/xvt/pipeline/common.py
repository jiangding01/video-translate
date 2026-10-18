"""pipeline 包内共享的 Job、stage、artifact 记账工具。

仅存放被多个业务子模块共用、不含具体业务流程语义的基础函数：
输入探测、stage 状态迁移、原子 JSON 写入、artifact 登记与失效。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from xvt.artifacts import ArtifactKind, downstream_of

# 分块 SHA-256 的唯一实现在 xvt.checksums。这里只做包内别名，
# 保持 pipeline 各模块既有的 `checksum(path)` 调用名不变（34 处调用点零改动）。
from xvt.checksums import file_checksum as checksum
from xvt.download import inspect_remote
from xvt.errors import UserInputError, XvtError
from xvt.events import (
    EventSink,
    EventType,
    create_event,
    error_event_fields,
    job_event_fields,
    redact_text,
)
from xvt.job_store import get_job_file, load_job, save_job, utc_now
from xvt.media import MediaInfo, probe_media
from xvt.models import Artifact, Cue, Job, StageName, StageStatus, Word
from xvt.render import AssStyleOptions
from xvt.subtitles import SUPPORTED_SUBTITLE_EXTENSIONS, load_subtitles
from xvt.translate import (
    TranslationPolicy,
    map_subtitle_translations,
    translation_policy_from_dict,
)
from xvt.workflow_config import WorkflowConfig, clear_burn_fingerprint, get_workflow_config


def inspect_input(input_value: str) -> dict[str, Any]:
    parsed = urlparse(input_value)
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        return {"kind": "remote", **asdict(inspect_remote(input_value))}
    return {"kind": "local", **asdict(probe_media(input_value))}


def is_subtitle_input(input_value: str) -> bool:
    parsed = urlparse(input_value)
    return not parsed.scheme and Path(input_value).suffix.lower() in SUPPORTED_SUBTITLE_EXTENSIONS


def translation_qa_context(job: Job) -> dict[str, Any]:
    """候选 QA、修订预检与译文收尾使用同一媒体及字体上下文。"""
    from xvt.recovery import valid_artifact_path

    config = get_workflow_config(job)
    data: dict[str, Any] = {}
    if config is not None and config.burn_media:
        data = asdict(probe_media(config.burn_media))
    elif job.config.get("input_kind") != "subtitle":
        path = valid_artifact_path(job, "media_info")
        if path is None:
            raise ValueError("translation QA requires checksum-valid media metadata")
        data = json.loads(path.read_text(encoding="utf-8"))
        source = valid_artifact_path(job, "source_video")
        if source is not None and str(source) != data.get("path"):
            data = asdict(probe_media(source))
    return {
        "media_duration_ms": data.get("duration_ms"),
        "video_width": data.get("width"),
        "video_height": data.get("height"),
        "measure_layout": True,
        "render_options": AssStyleOptions(
            target_font=config.target_font if config else None,
            source_font=config.source_font if config else None,
        ),
        "required_terms": required_glossary_terms(job),
    }


def resolved_translation_policy(
    job: Job,
    config: WorkflowConfig | None = None,
) -> TranslationPolicy:
    """翻译策略回退的**唯一**实现：workflow 快照优先，顶层 ``translation_policy`` 兜底。

    翻译侧与译文 QA 侧必须共用这一份。两派口径曾并存（O2）：``workflow`` 与
    ``translation_progress_rebuildable`` 缺快照时回退空 ``TranslationPolicy()``，
    本函数与语义链回退顶层键。快照缺失而顶层键有术语的 Job（策略快照落地前创建的
    老 Job 上跑过一次 ``xvt translate --glossary G``，它只写顶层键不回写快照）于是
    翻译不带术语、译文 QA 按术语判 ``glossary_violation``，术语死锁没有出口。

    ``config`` 由调用方显式给出时优先采用，用于「执行中的 workflow 配置尚未回到
    磁盘」的推进路径；省略时从 Job 读回快照。顶层键的回退对两者一致。
    """

    resolved = config if config is not None else get_workflow_config(job)
    if resolved is not None and resolved.translation_policy is not None:
        return resolved.translation_policy
    return translation_policy_from_dict(job.config.get("translation_policy"))


def required_glossary_terms(job: Job) -> tuple[tuple[str, str], ...]:
    return tuple((term.source, term.target) for term in resolved_translation_policy(job).terms)


def media_info_from_remote_metadata(url: str, duration_ms: int) -> MediaInfo:
    return MediaInfo(
        path=url,
        duration_ms=duration_ms,
        size_bytes=0,
        format_name="remote",
        width=None,
        height=None,
        frame_rate=None,
        video_codec=None,
        audio_codec=None,
        has_video=False,
        has_audio=False,
    )


# 字幕 QA needs_review 的阻断文案：QA 门与源字幕修订共用同一份，
# 确认后的 stage 收口按这条消息识别过期错误。
SUBTITLE_REVIEW_REQUIRED_MESSAGE = (
    "subtitle QA needs_review; inspect the QA artifact, then run "
    "xvt continue <job> --acknowledge-subtitle-review"
)


_STAGE_EVENT_TYPES: dict[StageStatus, EventType] = {
    StageStatus.RUNNING: EventType.STAGE_STARTED,
    StageStatus.AWAITING_INPUT: EventType.STAGE_WAITING,
    StageStatus.FAILED: EventType.STAGE_FAILED,
    StageStatus.SUCCEEDED: EventType.STAGE_COMPLETED,
    StageStatus.SKIPPED: EventType.STAGE_COMPLETED,
}


def error_category(error: Exception) -> str:
    return getattr(error, "category", "workflow")


def set_stage(
    job: Job,
    stage: StageName,
    status: StageStatus,
    event_sink: EventSink | None,
    *,
    error: str | None = None,
    error_category: str = "workflow",
    extras: dict[str, Any] | None = None,
) -> None:
    # AGENTS 不可破坏约束 5：任何会持久化的错误文本都必须先脱敏。失败原因来自
    # yt-dlp / ffmpeg 的原始 stderr、403 detail、翻译提供方 4xx 响应，里面带签名
    # URL、Cookie、代理凭据与 base_url。它经 record.error 写进 job.json，再被
    # `status --json` 原样输出——这条链路不经过 create_event，必须在**这个边界**
    # 集中脱敏，而不是指望十几个调用点各自记得调 redact_text。
    # 事件分支复用同一份脱敏文本：redact_text 幂等，create_event 再脱敏一次结果不变，
    # 两条出口因此不会给出不一致的失败原因。
    redacted_error = redact_text(error) if error is not None else None
    record = job.stages[stage]
    now = utc_now()
    record.status = status
    record.error = redacted_error
    if status == StageStatus.RUNNING:
        record.started_at = now
        record.completed_at = None
    elif status != StageStatus.AWAITING_INPUT:
        record.completed_at = now
    save_job(job)
    if event_sink:
        event_type = _STAGE_EVENT_TYPES[status]
        payload: dict[str, Any] = {
            **job_event_fields(job.id, get_job_file(job)),
            "stage": stage.value,
            "status": status.value,
        }
        if status == StageStatus.FAILED and redacted_error is not None:
            payload.update(error_event_fields(error_category, redacted_error))
        if extras:
            # 不允许覆盖 stage 信封核心字段，避免调用方误伤事件契约。
            for key, value in extras.items():
                if key not in {"job_id", "job_file", "stage", "status", "error"}:
                    payload[key] = value
        event_sink(create_event(event_type, **payload))


def interrupt_running_stage(
    job_file: str | Path,
    stage: str,
    *,
    event_sink: EventSink | None = None,
) -> bool:
    try:
        stage_name = StageName(stage)
        job = load_job(job_file)
        if job.stages[stage_name].status is not StageStatus.RUNNING:
            return False
        set_stage(
            job,
            stage_name,
            StageStatus.FAILED,
            event_sink,
            error="interrupted by user",
        )
    # XvtError 必须在元组里：本函数是 cli 的 Ctrl-C 处理块的**第一条语句**，job.json
    # 缺失或不可解析时 load_job 抛的是 UserInputError（XvtError 子类），逃逸出去会顶掉
    # 整个中断收尾——不发 cli_failed、不打印恢复引导，退出码变成 1 而不是 130/143。
    # 中断收尾是尽力而为的记账，任何一种读不出 Job 的方式都只该让它安静地返回 False。
    except (KeyError, OSError, RuntimeError, ValueError, XvtError):
        return False
    return True


def emit_agent_fallback(
    job: Job,
    *,
    manifest_path: Path,
    reason: str,
    event_sink: EventSink,
    phase: str | None = None,
) -> None:
    """发射 Agent 降级事件 ``translation_fallback``（初译与润色共用的唯一实现）。

    ``phase=None`` 时**不写**这个键（而不是写 null）：初译侧的降级事件历来不带 phase，
    润色侧带 ``phase="polish"``，协议快照把这条不对称冻在 ``standard_agent_fallback``
    与 ``polish_agent_rerun`` 两个场景里。

    只发事件，不设 stage。两处调用点的 ``AWAITING_INPUT`` 位置与 extras 本就不同
    （初译在 ``prepare_agent_translation`` 内受 ``await_input`` 控制，润色在 agent 分支里
    先 RUNNING 再 AWAITING_INPUT 且带 ``extras={"phase": "polish"}``），把 stage 迁移并进来
    只会制造无谓的分支。

    ``reason`` / ``event_sink`` 都是必填：「没有降级原因或没有 sink 就不发」这条守卫留在
    调用点（``if fallback_reason and event_sink:``），与重构前完全一致。
    """
    event_sink(
        create_event(
            EventType.TRANSLATION_FALLBACK,
            **job_event_fields(job.id, get_job_file(job)),
            **({"phase": phase} if phase is not None else {}),
            mode="agent",
            reason=reason,
            path=str(manifest_path),
        )
    )


def write_json(path: Path, data: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def add_artifact(job: Job, kind: str, path: Path, event_sink: EventSink | None) -> None:
    artifact = Artifact(kind=kind, path=str(path), checksum=checksum(path))
    job.artifacts = [item for item in job.artifacts if item.kind != kind]
    job.artifacts.append(artifact)
    save_job(job)
    if event_sink:
        event_sink(
            create_event(
                EventType.ARTIFACT_CREATED,
                **job_event_fields(job.id, get_job_file(job)),
                kind=kind,
                path=str(path),
            )
        )


def artifact_checksum_for(job: Job, kind: str) -> str | None:
    artifact = next((item for item in job.artifacts if item.kind == kind), None)
    return artifact.checksum if artifact else None


def record_media_download_selection(job: Job, media: Path, event_sink: EventSink | None) -> None:
    path = media.with_suffix(".download.json")
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(data, dict) or data.get("media_checksum") != checksum(media):
        return
    add_artifact(job, "media_download_selection", path, event_sink)
    if event_sink:
        event_sink(
            create_event(
                EventType.MEDIA_DOWNLOAD_SELECTION,
                **job_event_fields(job.id, get_job_file(job)),
                summary_path=str(path),
            )
        )


def invalidate_rendered_artifacts(job: Job) -> None:
    # 失效集合由依赖图闭包推导：translated_cues 的全部下游产物。
    invalidated_kinds = downstream_of(ArtifactKind.TRANSLATED_CUES)
    job.artifacts = [
        artifact for artifact in job.artifacts if artifact.kind not in invalidated_kinds
    ]
    clear_burn_fingerprint(job)
    for stage in (StageName.SUBTITLE_QA, StageName.RENDER, StageName.BURN, StageName.MEDIA_QA):
        record = job.stages[stage]
        record.status = StageStatus.PENDING
        record.started_at = None
        record.completed_at = None
        record.error = None


def invalidate_preview_artifacts(job: Job) -> None:
    # preview_ass 自身及其下游（preview_video）。
    invalidated_kinds = downstream_of(ArtifactKind.PREVIEW_ASS) | {ArtifactKind.PREVIEW_ASS}
    job.artifacts = [
        artifact for artifact in job.artifacts if artifact.kind not in invalidated_kinds
    ]


def artifact_path(job: Job, kind: str) -> Path:
    artifact = next((item for item in job.artifacts if item.kind == kind), None)
    if artifact is None:
        raise ValueError(f"job has no {kind} artifact")
    return Path(artifact.path)


# 与 models.Job.from_dict 同款前向兼容策略：只保留已知字段，
# 新 schema 写入的额外键被忽略而不是抛裸 TypeError。
_CUE_FIELDS = {field.name for field in fields(Cue)}
_WORD_FIELDS = {field.name for field in fields(Word)}


def load_user_response_json(response_file: str | Path, *, what: str) -> Any:
    """读取用户提交的 response 文件。

    `response_file` 永远是用户直接输入的 CLI 位置参数，读不出或解析不了都是用户输入
    错误，不能逃逸成 OSError / JSONDecodeError 被顶层归到 dependency_or_process
    （退出码 3）。**已登记 artifact 的读取不要走这个函数**：那些路径来自 Job 自己，
    缺失是 Job 状态问题而不是用户敲错了命令行。

    还要注意间接路径：``translation.load_translation_response`` 内部就调用本函数，
    因此它同样只适用于用户提交的响应文件。读已登记 artifact 时改用
    ``translation.translations_from_response``（接受已解析的载荷），并把
    ``UserInputError``（``XvtError`` 子类）算进调用点的 except 元组——只读谓词漏掉它
    就会让本该判否的分支抛出异常。
    """

    path = Path(response_file).expanduser()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        # UnicodeDecodeError 是 ValueError 子类但不是 JSONDecodeError 子类：漏掉它，非 UTF-8
        # 响应就会绕过 UserInputError，stage_failed 记 workflow 而顶层按 isinstance(ValueError)
        # 记 invalid_input，同一次失败出现两个分类。
        raise UserInputError(f"cannot read {what}: {error}") from error


def load_cues(path: Path) -> list[Cue]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("cue artifact must contain a JSON array")
    cues = []
    try:
        for item in data:
            if not isinstance(item, dict):
                raise TypeError("cue records must be objects")
            raw_words = item.get("words", [])
            if not isinstance(raw_words, list):
                raise TypeError("cue words must be an array")
            words = []
            for word in raw_words:
                if not isinstance(word, dict):
                    raise TypeError("word records must be objects")
                words.append(
                    Word(**{key: value for key, value in word.items() if key in _WORD_FIELDS})
                )
            known = {key: value for key, value in item.items() if key in _CUE_FIELDS}
            cues.append(Cue(**{**known, "words": words}))
    except (AttributeError, TypeError) as error:
        raise ValueError(f"invalid cue artifact: {error}") from error
    return cues


def pending_translation_import_blocker(job: Job, config: WorkflowConfig) -> str | None:
    """待执行的 ``import_translation`` / ``translate`` 注定失败时给出诊断，否则 ``None``。

    端到端 Job 上「补齐译文」这一步并不总是调用翻译提供方：``_ensure_translation``
    会优先把 Job 内保留的 ``input_translation_subtitles`` 副本（退而求其次是
    ``config.workflow.translation_subtitles`` 指向的用户原始文件）重新套到当前
    ``source_cues`` 上。``rebuild-source --strategy asr`` 之后源时间轴已经变过，这份
    已存译文可能根本套不回去，于是 ``continue`` 每次都在 ``map_subtitle_translations``
    原地失败、Job 一个字节都不改——而 ``build_recovery_plan`` 严格只读、不感知运行期
    异常，照旧给出 ``retryable`` + ``continue``，用户没有任何出口（本仓库第 7 次同类
    缺陷，前 6 次见 docs/REVIEW_FINDINGS_2026_10_17.md）。

    判定与「强制对齐置信度不足的死循环出口」同一范式：**只看 Job 状态，不猜异常**，
    从 artifact + config 纯推导，并且只在能**确证**必然失败时才出声——源 cue 或译文
    任一侧拿不到可信输入时返回 ``None``，让既有分类原样生效。

    严格只读：不写盘、不登记 artifact、不改 stage。``status`` / ``continue`` /
    ``rebuild-source`` 收尾三处消费同一份实现与同一句诊断，重叠度量直接复用
    ``map_subtitle_translations``（连同它的 ``require_timeline_overlap`` 语义），
    不在这里重造一套阈值。

    两种成因给用户的动作不同，文案必须分开：文件读不出来是「那份文件没了」，
    映射不上是「时间轴对不上」。
    """

    # recovery 顶层 import 了 pipeline，这里只能函数级 import（与 translation_qa_context
    # 同一处理）。pipeline 包内依赖全是模块顶层 import，本行不在守卫范围内。
    from xvt.recovery import valid_artifact_path

    target_language = config.target_language
    if not target_language:
        return None
    # polished 腿不走这条路：`_ensure_translation` 的 polished 分支永远在自己的块里
    # 返回，draft 失效时直接 `run_translation`，从不重新套用已存译文副本。对它出声
    # 会把一次真能推进的 continue 误判成 manual_fix，连带让 batch-continue 停摆。
    if config.translation_quality == "polished":
        return None
    # 与 _ensure_translation 的取值顺序逐字一致：Job 内副本优先，其次是持久化目标里
    # 记着的用户原始文件。两者都没有时这一步会真的去调用翻译提供方，不归本谓词管。
    retained = valid_artifact_path(job, "input_translation_subtitles")
    if retained is not None:
        subtitle_path: Path = retained
    elif config.translation_subtitles:
        subtitle_path = Path(config.translation_subtitles).expanduser()
    else:
        return None

    # 约束 9：只认 checksum 有效的源 cue。失效或缺失时这一步会先在别处失败，
    # 断言「译文套不上」没有依据。
    source_path = valid_artifact_path(job, "source_cues")
    if source_path is None:
        return None
    try:
        source_cues = load_cues(source_path)
    except (OSError, ValueError):
        # UnicodeError / JSONDecodeError 都是 ValueError 的子类，这里一并接住。
        return None
    if not source_cues:
        return None

    try:
        translated_cues = load_subtitles(
            subtitle_path,
            role="translated",
            target_language=target_language,
        )
    except (OSError, ValueError, XvtError) as error:
        return (
            f"这个 Job 要回填的译文字幕 {subtitle_path} 现在读不出来（{error}）；"
            "continue 会在读取这份文件时原地失败，Job 状态不会有任何变化。"
        )
    if not translated_cues:
        return (
            f"这个 Job 要回填的译文字幕 {subtitle_path} 不含任何 cue；"
            "continue 会在读取这份文件时原地失败，Job 状态不会有任何变化。"
        )

    try:
        map_subtitle_translations(
            source_cues,
            translated_cues,
            # 与 _ensure_translation 同口径：Job 内副本没有人为当前这套源 cue 主张过
            # 归属，等长只是巧合，必须由时间轴证实。
            require_timeline_overlap=retained is not None,
        )
    except ValueError as error:
        return (
            f"这个 Job 保留的已存译文 {subtitle_path} 已经套不回当前源 cue 的时间轴"
            f"（{error}）；源时间轴在那份译文导入之后变过（例如 rebuild-source "
            "--strategy asr），continue 每次都会在同一处失败，Job 状态不会有任何变化。"
        )
    return None


def parse_cue_text_items(items: Any) -> dict[int, str] | None:
    """把 ``[{"id": int, "text": str}, ...]`` 解析成 ``{cue_id: 去空白文本}``。

    结构非法（不是数组、item 不是对象、id 不是 int 或是 bool、id 重复、
    text 不是非空字符串）时返回 ``None``，由调用方决定是抛异常、判否还是
    丢弃 checkpoint —— 这里刻意不产生任何用户可见文案。
    """
    if not isinstance(items, list):
        return None
    translations: dict[int, str] = {}
    for item in items:
        cue_id = item.get("id") if isinstance(item, dict) else None
        text = item.get("text") if isinstance(item, dict) else None
        if (
            not isinstance(cue_id, int)
            or isinstance(cue_id, bool)
            or cue_id in translations
            or not isinstance(text, str)
            or not text.strip()
        ):
            return None
        translations[cue_id] = text.strip()
    return translations

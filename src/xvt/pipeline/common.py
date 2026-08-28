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
from xvt.errors import UserInputError
from xvt.events import (
    EventSink,
    EventType,
    create_event,
    error_event_fields,
    job_event_fields,
)
from xvt.job_store import get_job_file, load_job, save_job, utc_now
from xvt.media import MediaInfo, probe_media
from xvt.models import Artifact, Cue, Job, StageName, StageStatus, Word
from xvt.subtitles import SUPPORTED_SUBTITLE_EXTENSIONS
from xvt.workflow_config import clear_burn_fingerprint


def inspect_input(input_value: str) -> dict[str, Any]:
    parsed = urlparse(input_value)
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        return {"kind": "remote", **asdict(inspect_remote(input_value))}
    return {"kind": "local", **asdict(probe_media(input_value))}


def is_subtitle_input(input_value: str) -> bool:
    parsed = urlparse(input_value)
    return not parsed.scheme and Path(input_value).suffix.lower() in SUPPORTED_SUBTITLE_EXTENSIONS


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
    record = job.stages[stage]
    now = utc_now()
    record.status = status
    record.error = error
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
        if status == StageStatus.FAILED and error is not None:
            payload.update(error_event_fields(error_category, error))
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
    except (KeyError, OSError, RuntimeError, ValueError):
        return False
    return True


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
    """

    path = Path(response_file).expanduser()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
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

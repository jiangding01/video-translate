from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# JSONL 事件协议版本。兼容性承诺见 docs/EVENTS.md：
# 新增可选字段不升版本；删除或修改既有字段的语义必须升版本。
EVENT_PROTOCOL_VERSION = 1


class EventType(StrEnum):
    """全部 JSONL 事件类型的单一事实来源。

    成员值即写入 JSONL 的 ``type`` 字符串，生产端与消费端都应引用本枚举而非裸字符串。
    分组与各类型的 data 字段契约见 docs/EVENTS.md。
    """

    # 工作流生命周期
    WORKFLOW_STARTED = "workflow_started"
    WORKFLOW_PLANNED = "workflow_planned"
    WORKFLOW_WAITING = "workflow_waiting"
    WORKFLOW_COMPLETED = "workflow_completed"
    WORKFLOW_FAILED = "workflow_failed"

    # Job 生命周期
    JOB_STARTED = "job_started"
    JOB_COMPLETED = "job_completed"
    JOB_RESUMED = "job_resumed"

    # stage 推进
    STAGE_STARTED = "stage_started"
    STAGE_WAITING = "stage_waiting"
    STAGE_COMPLETED = "stage_completed"
    STAGE_FAILED = "stage_failed"

    # artifact 登记
    ARTIFACT_CREATED = "artifact_created"
    ARTIFACT_INVALIDATED = "artifact_invalidated"

    # 下载
    DOWNLOAD_PROGRESS = "download_progress"

    # 转写
    TRANSCRIPTION_PROGRESS = "transcription_progress"
    REMOTE_SUBTITLES_SELECTED = "remote_subtitles_selected"

    # 翻译
    TRANSLATION_PROVIDER_SELECTED = "translation_provider_selected"
    TRANSLATION_FALLBACK = "translation_fallback"
    TRANSLATION_PROGRESS_RESET = "translation_progress_reset"
    TRANSLATION_RESEGMENTED = "translation_resegmented"
    TRANSLATION_BATCH_STARTED = "translation_batch_started"
    TRANSLATION_BATCH_RETRYING = "translation_batch_retrying"
    TRANSLATION_BATCH_COMPLETED = "translation_batch_completed"

    # 烧录与预览
    BURN_PROGRESS = "burn_progress"
    PREVIEW_STARTED = "preview_started"
    PREVIEW_PROGRESS = "preview_progress"
    PREVIEW_COMPLETED = "preview_completed"

    # 批处理
    BATCH_STARTED = "batch_started"
    BATCH_ITEM_STARTED = "batch_item_started"
    BATCH_ITEM_COMPLETED = "batch_item_completed"
    BATCH_ITEM_WAITING = "batch_item_waiting"
    BATCH_ITEM_FAILED = "batch_item_failed"
    BATCH_ITEM_INTERRUPTED = "batch_item_interrupted"
    BATCH_FINISHED = "batch_finished"
    BATCH_MESSAGE = "batch_message"
    BATCH_ERROR = "batch_error"

    # 外部命令
    COMMAND_STARTED = "command_started"
    COMMAND_COMPLETED = "command_completed"
    COMMAND_FAILED = "command_failed"

    # CLI 终态
    CLI_COMPLETED = "cli_completed"
    CLI_FAILED = "cli_failed"


_REDACTED = "<redacted>"
_URL_PATTERN = re.compile(r"https?://[^\s\"']+")
_SENSITIVE_QUERY_KEYS = {
    "access_token",
    "api_key",
    "auth",
    "authorization",
    "key",
    "signature",
    "sig",
    "token",
    "x-amz-credential",
    "x-amz-security-token",
    "x-amz-signature",
}


@dataclass(frozen=True, slots=True)
class Event:
    type: str
    timestamp: str
    data: dict[str, Any]
    v: int = field(default=EVENT_PROTOCOL_VERSION)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


EventSink = Callable[[Event], None]


def _redact_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return value
    netloc = (
        f"{_REDACTED}@{parsed.netloc.rsplit('@', 1)[1]}"
        if parsed.username is not None
        else parsed.netloc
    )
    query = urlencode(
        [
            (key, _REDACTED if key.lower() in _SENSITIVE_QUERY_KEYS else query_value)
            for key, query_value in parse_qsl(parsed.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))


def _redact_event_value(value: Any) -> Any:
    if isinstance(value, str):
        return _URL_PATTERN.sub(lambda match: _redact_url(match.group()), value)
    if isinstance(value, list):
        return [_redact_event_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_event_value(item) for item in value)
    if isinstance(value, dict):
        return {key: _redact_event_value(item) for key, item in value.items()}
    return value


def create_event(event_type: EventType | str, **data: Any) -> Event:
    """构造一个带协议版本信封的事件。

    过渡期同时接受 ``EventType`` 与裸 ``str``，以便生产端逐步迁移。P3c 完成生产端全面
    迁移后，本签名应收紧为纯 ``EventType``，届时删除 ``str`` 兼容分支。
    """
    return Event(
        type=str(event_type),
        timestamp=datetime.now(UTC).isoformat(),
        data=_redact_event_value(data),
    )


def job_event_fields(job_id: str | None, job_file: str | Path | None) -> dict[str, Any]:
    """构造关联 Job 事件的 v1 信封字段。

    同时返回 ``job_id`` 与 ``job_file`` 两键，即使取值为 ``None`` 也写入，保证下游消费端
    可以稳定读取两个键。生产端在 P3b/P3c 逐步接入本辅助函数。
    """
    return {
        "job_id": job_id,
        "job_file": str(job_file) if job_file is not None else None,
    }


def error_event_fields(category: str, message: str) -> dict[str, Any]:
    """构造失败类事件的 v1 统一错误载荷 ``error: {category, message}``。

    生产端在 P3b/P3c 逐步接入本辅助函数，取代旧的 ``error`` / ``error_type`` /
    ``message`` + ``category`` 三种分裂写法。
    """
    return {"error": {"category": category, "message": message}}

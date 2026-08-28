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

    # 字幕 QA 人工确认
    SUBTITLE_REVIEW_ACKNOWLEDGED = "subtitle_review_acknowledged"
    TRANSLATION_REVIEW_ACKNOWLEDGED = "translation_review_acknowledged"
    TRANSLATION_REVIEW_INPUT_READY = "translation_review_input_ready"
    TRANSLATION_REVIEW_REVISED = "translation_review_revised"
    SOURCE_REVIEW_INPUT_READY = "source_review_input_ready"
    SOURCE_REVIEW_REVISED = "source_review_revised"

    # 下载
    DOWNLOAD_PROGRESS = "download_progress"

    # 转写
    TRANSCRIPTION_PROGRESS = "transcription_progress"
    REMOTE_SUBTITLES_SELECTED = "remote_subtitles_selected"
    SOURCE_SELECTED = "source_selected"

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
# 自由文本中的鉴权头与 Cookie：外部命令的原始 stderr/stdout（如 yt-dlp -v 回显的
# 请求头）会进入 command_failed 等事件的错误消息，必须在事件边界整体脱敏。
# 策略刻意选最钝的：命中敏感头名后一律吞到行尾，不解析引号或转义
# （转义引号的 Digest Header 曾绕过精细匹配）——误伤同行尾随文本的代价
# 远小于泄漏凭据。
_SENSITIVE_HEADER_PATTERN = re.compile(
    r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie|x-api-key)"
    r"\s*(?:[:=]|\"\s*:|'\s*:)[^\n]*"
)
_BEARER_PATTERN = re.compile(r"(?i)\bbearer[ \t]+[A-Za-z0-9._~+/=-]+")
# 任意 scheme 的 URL userinfo：@ 之前的一切（user:pass、:pass、仅 token）都视为
# 凭据整体脱敏；redact_url 只处理 http/https，这里兜底其余协议。
_USERINFO_PATTERN = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@]+@")
# 事件 data 中的敏感字典 key：值不做解析、整体替换。覆盖嵌套 Header 字典
# （{"Cookie": ...}）与常见凭据字段名。
_SENSITIVE_DATA_KEYS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "api_key",
    "api-key",
    "apikey",
    "password",
    "access_token",
    "token",
    "secret",
}
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


def redact_url(value: str) -> str:
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


def _redact_free_text(value: str) -> str:
    value = _SENSITIVE_HEADER_PATTERN.sub(lambda match: f"{match.group(1)}: {_REDACTED}", value)
    value = _USERINFO_PATTERN.sub(lambda match: f"{match.group(1)}{_REDACTED}@", value)
    return _BEARER_PATTERN.sub(f"Bearer {_REDACTED}", value)


def _redact_event_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_free_text(_URL_PATTERN.sub(lambda match: redact_url(match.group()), value))
    if isinstance(value, list):
        return [_redact_event_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_event_value(item) for item in value)
    if isinstance(value, dict):
        return {
            key: (
                _REDACTED
                if isinstance(key, str) and key.lower() in _SENSITIVE_DATA_KEYS
                else _redact_event_value(item)
            )
            for key, item in value.items()
        }
    return value


def create_event(event_type: EventType, **data: Any) -> Event:
    """构造一个带协议版本信封的事件。

    ``event_type`` 必须是 ``EventType`` 枚举成员；生产端不应传入裸 ``str``。
    """
    return Event(
        type=str(event_type),
        timestamp=datetime.now(UTC).isoformat(),
        data=_redact_event_value(data),
    )


def job_event_fields(job_id: str | None, job_file: str | Path | None) -> dict[str, Any]:
    """构造关联 Job 事件的 v1 信封字段。

    同时返回 ``job_id`` 与 ``job_file`` 两键，即使取值为 ``None`` 也写入，保证下游消费端
    可以稳定读取两个键。生产端已全量接入本辅助函数。
    """
    return {
        "job_id": job_id,
        "job_file": str(job_file) if job_file is not None else None,
    }


def error_event_fields(category: str, message: str) -> dict[str, Any]:
    """构造失败类事件的 v1 统一错误载荷 ``error: {category, message}``。

    生产端已全量接入本辅助函数，取代旧的 ``error`` / ``error_type`` /
    ``message`` + ``category`` 三种分裂写法。
    """
    return {"error": {"category": category, "message": message}}

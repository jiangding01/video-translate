from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote_plus, unquote_plus, urlencode, urlsplit, urlunsplit

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
    MEDIA_DOWNLOAD_SELECTION = "media_download_selection"

    # 转写
    TRANSCRIPTION_PROGRESS = "transcription_progress"
    TRANSCRIPTION_FALLBACK = "transcription_fallback"
    REMOTE_SUBTITLES_SELECTED = "remote_subtitles_selected"
    SOURCE_SELECTED = "source_selected"

    # 翻译
    TRANSLATION_PROVIDER_SELECTED = "translation_provider_selected"
    TRANSLATION_FALLBACK = "translation_fallback"
    TRANSLATION_PROGRESS_RESET = "translation_progress_reset"
    TRANSLATION_RESEGMENTED = "translation_resegmented"
    TRANSLATION_IMPORT_DROPPED = "translation_import_dropped"
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
#
# 字符类**必须允许 `@`**：密码里出现 `@` 是合法且常见的（`socks5://u:p@ss@host`），
# 排除 `@` 会让匹配停在第一个 `@`，后半段密码原样留下（实测 socks5 泄漏）。改为
# 贪婪匹配 + 回溯到 authority 里的**最后**一个 `@`，与 http/https 分支
# （`redact_url` 的 `netloc.rsplit("@", 1)`）同口径——这是按 URL 语法而不是 scheme
# 白名单判定的，因此对任意 scheme 都成立。
#
# 过度匹配由 RFC 3986 的 authority 终止符兜住：authority 止于首个 `/`、`?`、`#`，
# 字符类排除这三者（外加空白），`@` 必须落在 authority 内才算 userinfo。
# 没有 `scheme://` 前缀的 `@`（邮箱、`git@host:path`、装饰器）本就不被本规则触碰。
_USERINFO_PATTERN = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/?#]+@")
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
# 签名 URL 的 query 参数名没有跨云统一命名，精确名单必然追不上（AWS 的
# x-amz-signature、GCS v4 的 X-Goog-Signature、阿里云的 auth_key、腾讯的
# q-signature 各写各的）。改为三层判定，见 _is_sensitive_query_key。
#
# 第一层：历史精确名单。下面两层如今已经完全覆盖它（api_key → 分段 "key"，
# x-amz-signature → 子串 "signature"，等等），保留它是为了把「这些键确定敏感」
# 写死在代码里：后两层的规则日后若被收窄，这一层仍然兜住既有行为。
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
# 第二层：这几个词出现在键名任意位置都视为凭据。它们几乎不会构成正常参数名的
# 一部分，误伤面最小，而覆盖面最广（x-goog-signature / x-amz-credential /
# csrf_token / OSSAccessKeyId / SecretAccessKey 全部命中）。
_SENSITIVE_QUERY_SUBSTRINGS = (
    "signature",
    "credential",
    "token",
    "secret",
    "password",
    "accesskey",
    "apikey",
)
# 第三层：这几个词做子串必然误伤（key → monkey/keyword，sig → design/signal，
# auth → author，pwd → 无常见误伤但保持同口径），因此只在把键名按非字母数字
# 字符切段后、某一整段与之相等时才命中（auth_key → ["auth", "key"] 命中）。
_SENSITIVE_QUERY_SEGMENTS = frozenset(
    {
        "auth",
        "authorization",
        "key",
        "sig",
        "pwd",
        "passwd",
    }
)
_QUERY_KEY_SEGMENT_SEPARATOR = re.compile(r"[^a-z0-9]+")


def _is_sensitive_query_key(key: str) -> bool:
    """判定一个 URL query 参数名是否承载凭据。

    精确名单 → 子串规则 → 分段整词规则，三层任一命中即脱敏。分层的唯一目的是
    在「漏掉新签名方案」与「误伤正常参数」之间取舍：覆盖面靠子串，安全边界靠分段。
    """
    normalized = key.lower()
    if normalized in _SENSITIVE_QUERY_KEYS:
        return True
    if any(marker in normalized for marker in _SENSITIVE_QUERY_SUBSTRINGS):
        return True
    segments = _QUERY_KEY_SEGMENT_SEPARATOR.split(normalized)
    return any(segment in _SENSITIVE_QUERY_SEGMENTS for segment in segments)


# 不含 scheme 的 `路径?query`：yt-dlp / urllib3 的失败消息常常只带路径与 query
# （`unable to download video data: /videoplayback?sig=...`、
# `HTTPSConnectionPool(host=..., port=443): /videoplayback?sig=...`、协议相对的
# `//host/videoplayback?sig=...`）。`_URL_PATTERN` 要求 `https?://`、
# `_USERINFO_PATTERN` 要求 `scheme://`，三种形态两条都够不着（2026-10-18 实测原样
# 返回，签名明文落进事件与 `job.json`，违反不可破坏约束 5）。
#
# 口径刻意**不是**「对整个不含 scheme 的 `路径?query` 脱敏」——`?k=v` / `k=v` 这个
# 形状在 ffmpeg 滤镜串（`ass=filename=...`、`scale=640:360`）、yt-dlp 格式选择器
# （`bv*+ba/b`）与普通日志散文（`width=161 limit=492`）里到处都是，整体脱敏的误伤面
# 大一个量级。改为只脱敏**键名已被 `_is_sensitive_query_key` 判为敏感**的那些
# `key=value` 的值，与有没有 scheme 无关：误伤面从「任何 `?k=v` 形状」收窄到
# 「键名命中策展名单」，判定本体复用上面那三层匹配，不在这里重造。
#
# 分隔符只认 `?` 与 `&`，与 `redact_url` 依赖的 `parse_qsl`（Python 3.10 起不再按 `;`
# 切分）同口径——两条路径对同一段 query 的切分必须一致，否则「有没有 scheme」就成了
# 凭据能否泄漏的开关。键名在判定前按 `parse_qsl` 的口径解码一次（`%73ig` → `sig`）。
#
# 值的终止边界取 RFC 3986 之外再加一圈日志常见包裹符：空白、`&`（下一个参数）、
# `#`（fragment）、引号、反引号、`<` `>`、右括号 `)` `]` `}`、`;`、`|`。真实消息里
# query 后面跟的就是这些（样本之一整段被 `HTTPSConnectionPool(...)` 包着、query 跟在
# `: ` 后面；另一种形态是 `(input /videoplayback?sig=...)`）。排除 `<` 顺带让本规则
# 不会二次改写自己产出的 `<redacted>`，幂等性不靠额外判断。
_SENSITIVE_QUERY_PAIR_PATTERN = re.compile(r"""(?i)([?&])([a-z0-9_.%+~-]+)=([^\s&#"'<>)\]};|`]+)""")
# `redact_url` 的 `urlencode` 会把标记串规范化成 `%3Credacted%3E`。带 scheme 的
# http/https 路径本来就工作正常，本规则必须对它的产物零改动，否则同一个键在
# `redact_text` 里会按有没有 scheme 给出两种拼写（既有测试也钉着这一份）。
_ENCODED_REDACTED_QUERY_VALUE = quote_plus(_REDACTED).casefold()


def _redact_query_pair(match: re.Match[str]) -> str:
    separator, key, value = match.group(1), match.group(2), match.group(3)
    if not _is_sensitive_query_key(unquote_plus(key)):
        return match.group(0)
    if value.casefold() == _ENCODED_REDACTED_QUERY_VALUE:
        return match.group(0)
    return f"{separator}{key}={_REDACTED}"


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
            (key, _REDACTED if _is_sensitive_query_key(key) else query_value)
            for key, query_value in parse_qsl(parsed.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))


def _redact_free_text(value: str) -> str:
    value = _SENSITIVE_HEADER_PATTERN.sub(lambda match: f"{match.group(1)}: {_REDACTED}", value)
    value = _USERINFO_PATTERN.sub(lambda match: f"{match.group(1)}{_REDACTED}@", value)
    value = _BEARER_PATTERN.sub(f"Bearer {_REDACTED}", value)
    # 必须排在 `redact_text` 的 `_URL_PATTERN` 之后（它在调用本函数前已经跑过）：
    # 带 scheme 的 URL 由 `redact_url` 规范化，本规则只收不含 scheme 的残余形态。
    return _SENSITIVE_QUERY_PAIR_PATTERN.sub(_redact_query_pair, value)


def redact_text(value: str) -> str:
    """对一段自由文本做完整脱敏：URL 中的 userinfo / 敏感 query，再是鉴权头、Bearer 与 userinfo，
    最后是不含 scheme 的 `路径?query`（只脱敏键名被 `_is_sensitive_query_key` 判为敏感的那些值）。

    两条持久化边界都已集中接入本函数，调用方不需要（也不应依赖自觉）逐处调用：

    - 事件边界：``create_event`` → ``_redact_event_value``。
    - ``job.json`` 边界：``pipeline.common.set_stage`` 在写 ``record.error`` 之前统一调用，
      因此 ``status --json`` 输出的失败原因也是脱敏后的。

    本函数**幂等**：对已脱敏文本再调用一次结果不变（``set_stage`` 脱敏后，其事件分支
    会再经 ``create_event`` 脱敏一次，两条出口必须给出同一份文本）。

    边界之外新增持久化出口时，仍需显式调用本函数——但不要把它作为**已有**两条边界的
    替代方案，在调用点重复脱敏只会掩盖边界缺口。
    """
    return _redact_free_text(_URL_PATTERN.sub(lambda match: redact_url(match.group()), value))


def _redact_event_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
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

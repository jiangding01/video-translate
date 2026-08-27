from __future__ import annotations

import contextlib
import os
import re
import tempfile
from pathlib import Path

import pysubs2
from pysubs2.exceptions import Pysubs2Error

from xvt.errors import UserInputError
from xvt.models import Cue

SUPPORTED_SUBTITLE_EXTENSIONS = frozenset({".srt", ".vtt", ".ass", ".ssa"})
# 写侧只支持纯文本字幕：ASS/SSA 的 `{...}` 是覆盖标签语法，直接写入 cue 原文会让字面
# 花括号被 libass 吞掉。真正需要 ASS 输出的路径走 `render.save_bilingual_ass`（它会转义）。
SAVE_SUBTITLE_EXTENSIONS = frozenset({".srt", ".vtt"})

# SRT/VTT 里 `{...}` 是普通文本，只有 `{\...}` 形态才是 pysubs2 由 HTML 标签转换出来的
# SSA 覆盖块。libass 忽略 `{` 与 `\` 之间的空白，所以 `{ \an8}` 同样按覆盖块处理。
_SSA_OVERRIDE_BLOCK = re.compile(r"\{\s*\\[^}]*\}")
# pysubs2 读到的格式名（`SSAFile.format`），按实际内容而非文件后缀判定，避免
# 「`.srt` 后缀装着 ASS 内容」被误当成纯文本字幕。
_LITERAL_BRACE_INPUT_FORMATS = frozenset({"srt", "vtt"})

# UTF-16/32 的 BOM 字节序列，只用于把「不是 UTF-8」的报错文案说得更准，不做编码探测。
# 长的（UTF-32）必须先判：UTF-32 LE 的前两字节与 UTF-16 LE 完全相同。
_UTF_BOM_PREFIXES = (
    b"\xff\xfe\x00\x00",
    b"\x00\x00\xfe\xff",
    b"\xff\xfe",
    b"\xfe\xff",
)

# 人工修订 SRT 每条正文的首行 cue ID 注释，供用户与语义报告的 cue_ids 对照。
# 只匹配整行的 `#<数字>`，正常译文不会以这种形式独占一行。
_REVIEW_ID_MARKER = re.compile(r"^#(\d+)$")
_REVIEW_TIME_RANGE = re.compile(
    r"^(?P<start>\d{2,}:\d{2}:\d{2},\d{3})[ \t]+-->[ \t]+"
    r"(?P<end>\d{2,}:\d{2}:\d{2},\d{3})(?:\s+.*)?$"
)


def normalize_display_text(text: str) -> str:
    return " ".join(text.split())


def load_subtitles(
    path: str | Path,
    *,
    role: str = "source",
    source_language: str | None = None,
    target_language: str | None = None,
    display_path: str | Path | None = None,
) -> list[Cue]:
    """读入字幕文件为 cue 列表。

    ``display_path`` 只影响编码/解析报错里出现的路径：调用方若先把用户文件 copy2 成
    Job 内部副本再读（``artifacts/input-subtitles.srt``），必须把用户传入的原始路径传进
    来——让用户去转码内部副本是没有意义的。内容由 copy2 保证与原文件一致。
    """

    subtitle_path = Path(path).expanduser()
    # 存在性/后缀校验针对真实读取的文件，不替换成展示路径（内部副本缺失时指向原始路径
    # 会指错地方）；只有编码与解析报错用展示路径。
    if not subtitle_path.is_file():
        raise ValueError(f"subtitle file does not exist: {subtitle_path}")
    if subtitle_path.suffix.lower() not in SUPPORTED_SUBTITLE_EXTENSIONS:
        raise ValueError(f"unsupported subtitle format: {subtitle_path.suffix}")
    if role not in {"source", "translated"}:
        raise ValueError(f"unsupported subtitle role: {role}")

    reported_path = Path(display_path).expanduser() if display_path is not None else subtitle_path
    try:
        # utf-8-sig 与 load_review_srt 口径一致：无 BOM 时与 utf-8 完全等价，带 BOM 时把
        # ﻿ 去掉。BOM 残留会让 pysubs2 的格式嗅探（`lstrip().startswith("WEBVTT")`）漏判
        # VTT——轻则按 SRT 解析出错位内容，重则抛 FormatAutodetectionError。
        subtitles = pysubs2.load(str(subtitle_path), encoding="utf-8-sig")
    except UnicodeDecodeError as error:
        raise UserInputError(_encoding_error_message(subtitle_path, reported_path)) from error
    except Pysubs2Error as error:
        # Pysubs2Error 直接继承 Exception，不包装会穿透 CLI 顶层兜底变成裸 traceback。
        raise UserInputError(
            f"subtitle file could not be parsed as "
            f"{subtitle_path.suffix.lower().lstrip('.')}: {reported_path} ({error})"
        ) from error
    keep_literal_braces = subtitles.format in _LITERAL_BRACE_INPUT_FORMATS
    cues = []
    for event in subtitles:
        raw = _event_text(event, keep_literal_braces=keep_literal_braces)
        # 空判定必须用最终文本：`{x}` 这类只含字面花括号的 cue 在保留后不再是空的。
        if event.is_comment or not raw.strip():
            continue
        if event.end <= event.start:
            raise ValueError(f"invalid subtitle timestamp: {event.start} --> {event.end}")
        text = "\n".join(line.strip() for line in raw.splitlines() if line.strip())
        cue = Cue(
            id=len(cues) + 1,
            start_ms=event.start,
            end_ms=event.end,
            source_text=text if role == "source" else "",
            target_text=text if role == "translated" else None,
            source_language=source_language,
            target_language=target_language,
            timing_confidence=1.0,
            provenance=("user_source_subtitle" if role == "source" else "user_translated_subtitle"),
        )
        cues.append(cue)
    return cues


def _encoding_error_message(subtitle_path: Path, reported_path: Path) -> str:
    """拼非 UTF-8 字幕的可操作报错；命中 UTF-16/32 BOM 时点名疑似编码。

    只做 BOM 前缀比对（零误判），不做字符集探测：gb18030 之类的宽松解码器会把 Big5、
    Shift_JIS、cp1252 的内容「成功」解成乱码，乱码流进翻译 API 与 checksum 的代价远高
    于直接报错。消息只含路径与建议，不回显文件内容。
    """

    header = b""
    with contextlib.suppress(OSError):
        with subtitle_path.open("rb") as handle:
            header = handle.read(4)
    matched = next((prefix for prefix in _UTF_BOM_PREFIXES if header.startswith(prefix)), None)
    if matched is None:
        hint, source_encoding = "", "gb18030"
    else:
        family = "UTF-32" if len(matched) == 4 else "UTF-16"
        hint, source_encoding = f"疑似 {family} 编码，", family.lower()
    return (
        f"subtitle file is not UTF-8 encoded: {reported_path}; {hint}"
        f"请先转成 UTF-8（例如 iconv -f {source_encoding} -t utf-8 old.srt > new.srt，"
        "或用编辑器另存为 UTF-8）"
    )


def _event_text(event: pysubs2.SSAEvent, *, keep_literal_braces: bool) -> str:
    """取事件正文；SRT/VTT 保留字面花括号，ASS/SSA 仍走 pysubs2 的 ``plaintext``。

    ``plaintext`` 会剥掉全部 ``{...}``，对 ASS/SSA 是正确的（那里 ``{...}`` 是覆盖标签
    或注释块），但对 SRT/VTT 会把 ``{x}`` 这类普通文本整段吞掉。保留分支的替换顺序与
    ``plaintext`` 一致：先剥覆盖块，再依次处理 ``\\h``、``\\n``、``\\N``。
    """

    if not keep_literal_braces:
        return event.plaintext
    text = _SSA_OVERRIDE_BLOCK.sub("", event.text)
    text = text.replace(r"\h", " ")
    text = text.replace(r"\n", "\n")
    text = text.replace(r"\N", "\n")
    return text


def _cue_text(cue: Cue, mode: str, target_first: bool) -> str:
    if mode == "source":
        return cue.source_text
    if not cue.target_text:
        raise ValueError(f"cue {cue.id} has no translated text")
    if mode == "translated":
        return cue.target_text
    if mode == "bilingual":
        lines = (
            (normalize_display_text(cue.target_text), normalize_display_text(cue.source_text))
            if target_first
            else (
                normalize_display_text(cue.source_text),
                normalize_display_text(cue.target_text),
            )
        )
        return r"\N".join(lines)
    raise ValueError(f"unsupported subtitle mode: {mode}")


def save_subtitles(
    cues: list[Cue],
    path: str | Path,
    *,
    mode: str = "source",
    target_first: bool = True,
) -> Path:
    subtitle_path = Path(path).expanduser()
    if subtitle_path.suffix.lower() not in SAVE_SUBTITLE_EXTENSIONS:
        raise ValueError(f"unsupported subtitle output format: {subtitle_path.suffix}")

    subtitles = pysubs2.SSAFile()
    for cue in cues:
        if cue.end_ms <= cue.start_ms:
            raise ValueError(f"cue {cue.id} has an invalid timestamp")
        subtitles.events.append(
            pysubs2.SSAEvent(
                start=cue.start_ms,
                end=cue.end_ms,
                text=_cue_text(cue, mode, target_first),
            )
        )

    subtitle_path.parent.mkdir(parents=True, exist_ok=True)
    # keep_ssa_tags=True：pysubs2 写 SRT/VTT 时默认会剥掉 `{...}` 块，译文里的字面
    # 花括号（例如 `变量 {x}`）会被静默丢内容。该 kwarg 让文本原样落盘，`\N` 仍正常
    # 拆行。写侧只接受 SRT/VTT（见 SAVE_SUBTITLE_EXTENSIONS），ASS 由
    # `render.save_bilingual_ass` 负责，它会把花括号转义成 libass 的 `\{` / `\}`。
    subtitles.save(str(subtitle_path), encoding="utf-8", keep_ssa_tags=True)
    return subtitle_path


def save_review_srt(
    cues: list[Cue],
    path: str | Path,
    *,
    mode: str = "translated",
) -> Path:
    """写出人工修订用的 SRT：每条正文为 ``#<cue id>`` 注释行加当前文本。

    只包含传入的 cue（通常是语义报告命中的子集），时间轴原样保留。回填时按时间轴
    映射回 cue ID，注释行仅供人工对照。
    """

    subtitle_path = Path(path).expanduser()
    if subtitle_path.suffix.lower() != ".srt":
        raise ValueError(f"review subtitles must be .srt: {subtitle_path.suffix}")
    if mode not in {"source", "translated"}:
        raise ValueError(f"unsupported review subtitle mode: {mode}")

    blocks: list[str] = []
    for index, cue in enumerate(cues, start=1):
        if cue.end_ms <= cue.start_ms:
            raise ValueError(f"cue {cue.id} has an invalid timestamp")
        text = cue.source_text if mode == "source" else cue.target_text or ""
        text = _encode_review_blank_lines(text)
        blocks.append(
            f"{index}\n{_format_review_timestamp(cue.start_ms)} --> "
            f"{_format_review_timestamp(cue.end_ms)}\n#{cue.id}\n{text}"
        )

    subtitle_path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=subtitle_path.parent,
            prefix=f".{subtitle_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write("\n\n".join(blocks) + ("\n" if blocks else ""))
        os.replace(temporary, subtitle_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return subtitle_path


def load_review_srt(path: str | Path) -> list[tuple[int | None, int, int, str]]:
    """解析人工修订 SRT，返回 ``(注释 cue ID 或 None, 起始毫秒, 结束毫秒, 译文)``。

    注释行缺失时返回 ``None``，由调用方按时间轴确定 cue ID。不在此处判定归属或
    完整性，只做格式解析。
    """

    subtitle_path = Path(path).expanduser()
    if not subtitle_path.is_file():
        raise ValueError(f"subtitle file does not exist: {subtitle_path}")
    if subtitle_path.suffix.lower() != ".srt":
        raise ValueError(f"review subtitles must be .srt: {subtitle_path.suffix}")

    content = subtitle_path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    if not content.strip():
        return []

    entries: list[tuple[int | None, int, int, str]] = []
    for block in re.split(r"\n[ \t]*\n", content.strip()):
        lines = block.split("\n")
        if len(lines) < 2 or not lines[0].strip().isdigit():
            raise ValueError("invalid review SRT cue index")
        timing = _REVIEW_TIME_RANGE.match(lines[1].strip())
        if timing is None:
            raise ValueError(f"invalid review SRT timestamp: {lines[1].strip()}")
        start_ms = _parse_review_timestamp(timing.group("start"))
        end_ms = _parse_review_timestamp(timing.group("end"))
        if end_ms <= start_ms:
            raise ValueError(f"invalid subtitle timestamp: {start_ms} --> {end_ms}")
        text_lines = lines[2:]
        marker = _REVIEW_ID_MARKER.match(text_lines[0].strip()) if text_lines else None
        if marker is not None:
            text_lines = text_lines[1:]
        text = _decode_review_blank_lines("\n".join(text_lines).strip())
        entries.append((int(marker.group(1)) if marker else None, start_ms, end_ms, text))
    return entries


_REVIEW_BLANK_LINE = "\u2060"


def _format_review_timestamp(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def _parse_review_timestamp(value: str) -> int:
    hours, minutes, remainder = value.split(":")
    seconds, milliseconds = remainder.split(",")
    if int(minutes) >= 60 or int(seconds) >= 60:
        raise ValueError(f"invalid review SRT timestamp: {value}")
    return int(hours) * 3_600_000 + int(minutes) * 60_000 + int(seconds) * 1_000 + int(milliseconds)


def _encode_review_blank_lines(text: str) -> str:
    return "\n".join(
        _REVIEW_BLANK_LINE * (len(line) + 1)
        if line and set(line) == {_REVIEW_BLANK_LINE}
        else _REVIEW_BLANK_LINE
        if not line
        else line
        for line in text.split("\n")
    )


def _decode_review_blank_lines(text: str) -> str:
    return "\n".join(
        _REVIEW_BLANK_LINE * (len(line) - 1) if line and set(line) == {_REVIEW_BLANK_LINE} else line
        for line in text.split("\n")
    )

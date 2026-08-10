from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import pysubs2

from xvt.models import Cue

SUPPORTED_SUBTITLE_EXTENSIONS = frozenset({".srt", ".vtt", ".ass", ".ssa"})

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
) -> list[Cue]:
    subtitle_path = Path(path).expanduser()
    if not subtitle_path.is_file():
        raise ValueError(f"subtitle file does not exist: {subtitle_path}")
    if subtitle_path.suffix.lower() not in SUPPORTED_SUBTITLE_EXTENSIONS:
        raise ValueError(f"unsupported subtitle format: {subtitle_path.suffix}")
    if role not in {"source", "translated"}:
        raise ValueError(f"unsupported subtitle role: {role}")

    subtitles = pysubs2.load(str(subtitle_path), encoding="utf-8")
    cues = []
    for event in subtitles:
        if event.is_comment or not event.plaintext.strip():
            continue
        if event.end <= event.start:
            raise ValueError(f"invalid subtitle timestamp: {event.start} --> {event.end}")
        text = "\n".join(line.strip() for line in event.plaintext.splitlines() if line.strip())
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
    if subtitle_path.suffix.lower() not in SUPPORTED_SUBTITLE_EXTENSIONS:
        raise ValueError(f"unsupported subtitle format: {subtitle_path.suffix}")

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
    subtitles.save(str(subtitle_path), encoding="utf-8")
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

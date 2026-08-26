from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pysubs2

from xvt.models import Cue
from xvt.subtitles import normalize_display_text


@dataclass(frozen=True, slots=True)
class AssStyleOptions:
    target_font: str | None = None
    source_font: str | None = None
    target_size: int | None = None
    source_size: int | None = None
    margin_vertical: int = 16


def escape_ass_text(text: str) -> str:
    """转义 cue 文本里的花括号，避免被 libass 当成覆盖标签块吞掉。

    ASS 用 ``{...}`` 包裹覆盖标签，未转义时译文里的字面 ``{x}`` 会被静默丢弃，
    而 ``{\\an8}`` 之类会真的改变字幕位置。``\\{`` / ``\\}`` 是 libass 的扩展转义
    （VSFilter 不支持），本项目烧录固定走 ffmpeg/libass，可以接受。
    反斜杠刻意不处理：ASS 没有标准的反斜杠转义，而 libass 逐字符解析下未知的
    ``\\x`` 序列（含结尾悬挂的 ``\\``）会被原样输出，转义反而会引入可见杂字符。
    """

    return text.replace("{", "\\{").replace("}", "\\}")


def font_for_language(language: str | None, platform_name: str | None = None) -> str:
    normalized = (language or "").casefold()
    platform_key = platform_name or sys.platform
    if platform_key == "win32":
        if normalized.startswith("zh"):
            return "Microsoft YaHei"
        if normalized.startswith("ja"):
            return "Yu Gothic"
        if normalized.startswith("ko"):
            return "Malgun Gothic"
        return "Arial"
    if platform_key != "darwin":
        if normalized.startswith("zh"):
            return "Noto Sans CJK SC"
        if normalized.startswith("ja"):
            return "Noto Sans CJK JP"
        if normalized.startswith("ko"):
            return "Noto Sans CJK KR"
        if normalized.startswith(("ar", "fa", "ur")):
            return "Noto Sans Arabic"
        return "DejaVu Sans"
    if normalized.startswith("zh"):
        return "Hiragino Sans GB"
    if normalized.startswith("ja"):
        return "Hiragino Sans"
    if normalized.startswith("ko"):
        return "Apple SD Gothic Neo"
    if normalized.startswith(("ar", "fa", "ur")):
        return "Geeza Pro"
    return "Helvetica"


def default_target_size(video_height: int) -> int:
    if video_height <= 480:
        return 22
    if video_height <= 720:
        return 22
    return 20


def save_bilingual_ass(
    cues: list[Cue],
    path: str | Path,
    *,
    video_width: int,
    video_height: int,
    options: AssStyleOptions = AssStyleOptions(),
) -> Path:
    if video_width <= 0 or video_height <= 0:
        raise ValueError("video dimensions must be positive")

    target_size = options.target_size or default_target_size(video_height)
    source_size = options.source_size or max(8, round(target_size / 1.7))
    target_language = next((cue.target_language for cue in cues if cue.target_language), None)
    source_language = next((cue.source_language for cue in cues if cue.source_language), None)

    subtitles = pysubs2.SSAFile()
    subtitles.info["PlayResY"] = "288"
    subtitles.info["PlayResX"] = str(round(288 * video_width / video_height))
    subtitles.info["WrapStyle"] = "2"
    subtitles.info["ScaledBorderAndShadow"] = "yes"
    subtitles.styles["Target"] = pysubs2.SSAStyle(
        fontname=options.target_font or font_for_language(target_language),
        fontsize=target_size,
        bold=True,
        alignment=pysubs2.Alignment.BOTTOM_CENTER,
        marginv=options.margin_vertical,
        outline=1.2,
        shadow=0,
    )
    subtitles.styles["Source"] = pysubs2.SSAStyle(
        fontname=options.source_font or font_for_language(source_language),
        fontsize=source_size,
        bold=False,
        alignment=pysubs2.Alignment.BOTTOM_CENTER,
        marginv=options.margin_vertical,
        outline=1.0,
        shadow=0,
    )

    for cue in cues:
        if not cue.target_text:
            raise ValueError(f"cue {cue.id} has no translated text")
        target_text = escape_ass_text(normalize_display_text(cue.target_text))
        source_text = escape_ass_text(normalize_display_text(cue.source_text))
        # `\N` 与 `{\rSource}` 是这里刻意拼接的换行与样式切换结构，不参与转义。
        text = f"{target_text}\\N{{\\rSource}}{source_text}"
        subtitles.events.append(
            pysubs2.SSAEvent(
                start=cue.start_ms,
                end=cue.end_ms,
                style="Target",
                text=text,
            )
        )

    output_path = Path(path).expanduser()
    if output_path.suffix.casefold() != ".ass":
        raise ValueError("bilingual subtitle output must use the .ass extension")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subtitles.save(str(output_path), encoding="utf-8")
    return output_path

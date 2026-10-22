from __future__ import annotations

import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pysubs2

from xvt.models import Cue
from xvt.process import run_command
from xvt.segment import wrap_overflowing_target
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


def _target_ass_text(cue: Cue, *, video_width: int, video_height: int) -> str:
    """译文的 ASS 事件文本：归一化 → 超限才换行 → 逐行转义 → ``\\N`` 拼接。

    ASS 写出与 libass 测量共用这一份，和 ``qa._overflow_risk`` 一样只经
    ``segment.wrap_overflowing_target`` 决定切不切、怎么切，QA 量的就是烧出去的。
    整段不超限（或切不开）时与换行引入前逐字相同。
    """

    normalized = normalize_display_text(cue.target_text or "")
    lines = wrap_overflowing_target(
        normalized, cue.target_language, video_width=video_width, video_height=video_height
    )
    if lines is None:
        return escape_ass_text(normalized)
    # `\N` 是这里刻意拼接的换行结构，不参与转义。
    return "\\N".join(escape_ass_text(line) for line in lines)


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


def _styled_subtitles(
    cues: list[Cue],
    *,
    video_width: int,
    video_height: int,
    options: AssStyleOptions = AssStyleOptions(),
) -> pysubs2.SSAFile:
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
    return subtitles


def measure_bilingual_line_widths(
    cues: list[Cue],
    *,
    video_width: int,
    video_height: int,
    options: AssStyleOptions = AssStyleOptions(),
) -> dict[tuple[int, str], tuple[int, int]]:
    """用烧录样式与 libass 测量可见文字及描边，单位为 ASS 坐标像素。"""
    subtitles = _styled_subtitles(
        cues, video_width=video_width, video_height=video_height, options=options
    )
    available = int(subtitles.info["PlayResX"])
    # 扩大画布测自然宽度；碰到测量边界时仍判为超宽，不把裁切后的宽度当通过。
    canvas_width = max(4096, available * 2)
    subtitles.info["PlayResX"] = str(canvas_width)
    entries = []
    limits = []
    for role, name in (("target", "Target"), ("source", "Source")):
        style = subtitles.styles[name]
        limits.append(available - style.marginl - style.marginr)
        style.alignment = pysubs2.Alignment.MIDDLE_LEFT
        style.marginl = 32
        style.marginr = 0
        style.outlinecolor = style.primarycolor
        for cue in cues:
            text = cue.target_text if role == "target" else cue.source_text
            if not text or not text.strip():
                continue
            index = len(entries)
            entries.append((cue.id, role, limits[-1]))
            subtitles.events.append(
                pysubs2.SSAEvent(
                    start=index * 1000,
                    end=(index + 1) * 1000,
                    style=name,
                    text=(
                        _target_ass_text(cue, video_width=video_width, video_height=video_height)
                        if role == "target"
                        else escape_ass_text(normalize_display_text(text))
                    ),
                )
            )
    if not entries:
        return {}
    with tempfile.TemporaryDirectory(prefix="xvt-layout-") as directory:
        path = Path(directory) / "measure.ass"
        subtitles.save(str(path), encoding="utf-8")
        result = run_command(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostdin",
                "-f",
                "lavfi",
                "-i",
                f"color=black@0:s={canvas_width}x288:r=1:d={len(entries)},format=rgba",
                "-vf",
                "ass=filename=measure.ass:alpha=1,extractplanes=a,bbox=min_val=0",
                "-frames:v",
                str(len(entries)),
                "-f",
                "null",
                "-",
            ],
            cwd=directory,
            timeout=60,
        )
    if "failed to find any fallback with glyph" in result.stderr:
        raise ValueError("subtitle font cannot render every glyph")
    measured = {}
    seen_frames: set[int] = set()
    pattern = r"n:(\d+)\s+pts:.*?x1:(\d+) x2:(\d+).*?w:(\d+) h:(\d+)"
    for match in re.finditer(pattern, result.stderr):
        index, left, right, width, height = map(int, match.groups())
        if (
            index in seen_frames
            or index >= len(entries)
            or height <= 0
            or width <= 0
            or width != right - left + 1
        ):
            raise ValueError("invalid subtitle width measurement")
        seen_frames.add(index)
        cue_id, role, limit = entries[index]
        if left == 0 or right >= canvas_width - 1:
            width = max(width, limit + 1)
        measured[(cue_id, role)] = (width, limit)
    if len(measured) != len(entries):
        raise ValueError("incomplete subtitle width measurement")
    return measured


def save_bilingual_ass(
    cues: list[Cue],
    path: str | Path,
    *,
    video_width: int,
    video_height: int,
    options: AssStyleOptions = AssStyleOptions(),
) -> Path:
    subtitles = _styled_subtitles(
        cues, video_width=video_width, video_height=video_height, options=options
    )

    for cue in cues:
        if not cue.target_text:
            raise ValueError(f"cue {cue.id} has no translated text")
        target_text = _target_ass_text(cue, video_width=video_width, video_height=video_height)
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

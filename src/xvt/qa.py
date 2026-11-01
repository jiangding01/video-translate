from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any

from xvt.models import Cue
from xvt.process import CommandError
from xvt.render import (
    AssStyleOptions,
    FontGlyphError,
    measure_bilingual_line_widths,
    resolved_style_binding,
)
from xvt.segment import (
    TARGET_WRAP_MAX_LINES,
    target_wrap_binding,
    target_wrap_limit,
    wrap_overflowing_target,
)
from xvt.subtitles import normalize_display_text
from xvt.textwidth import (
    character_limit,
    display_width,
    display_width_limit,
    display_width_review_limit,
    is_cjk_language,
    is_japanese_or_korean,
    reading_speed_limit,
    reading_units,
)

__all__ = [
    "SubtitleIssue",
    "SubtitleQaReport",
    "check_subtitles",
    "target_overflow_cue_ids",
    "character_limit",
    "display_width",
    "display_width_limit",
    "display_width_review_limit",
    "is_cjk_language",
    "is_japanese_or_korean",
    "reading_speed_limit",
    "reading_units",
]


@dataclass(frozen=True, slots=True)
class SubtitleIssue:
    code: str
    severity: str
    cue_id: int
    message: str


@dataclass(frozen=True, slots=True)
class SubtitleQaReport:
    status: str
    cue_count: int
    issues: tuple[SubtitleIssue, ...]
    layout: dict[str, Any] | None = None


# 缺字之外的测量失败沿用这条文案（ffmpeg 缺失、超时、bbox 解析不出）；E2E 的降级
# 分路断言与存量报告都认它，不要改写。
_MEASUREMENT_UNAVAILABLE_MESSAGE = (
    "rendered width unavailable; conservative display-unit limits applied"
)
# 零宽与控制字符缺字形不改变观感，不足以把烧录拦下来；变体选择符同理。
_INVISIBLE_GLYPH_CATEGORIES = frozenset({"Cc", "Cf"})


def _glyph_is_invisible(codepoint: int) -> bool:
    if 0xFE00 <= codepoint <= 0xFE0F or 0xE0100 <= codepoint <= 0xE01EF:
        return True
    return unicodedata.category(chr(codepoint)) in _INVISIBLE_GLYPH_CATEGORIES


def _glyph_label(codepoint: int) -> str:
    """``U+1E900 '𞤀'``；不可见字符只给码位，回显字面量只会把报告弄脏。"""

    if _glyph_is_invisible(codepoint):
        return f"U+{codepoint:04X}"
    return f"U+{codepoint:04X} {chr(codepoint)!r}"


def _measurement_unavailable_issue(detail: str = "") -> SubtitleIssue:
    message = _MEASUREMENT_UNAVAILABLE_MESSAGE
    if detail:
        message = f"rendered width unavailable ({detail}); conservative display-unit limits applied"
    return SubtitleIssue("layout_measurement_unavailable", "warning", 0, message)


def _missing_glyph_issues(cues: list[Cue], error: FontGlyphError) -> list[SubtitleIssue]:
    """缺字时除了全局降级 warning，再按 cue 与腿点名「哪个字渲染不出来」。

    码位解析不出（libass 改了文案）时退回原来的笼统 warning：降级方向安全，不猜。
    码位在任何 cue 文本里都找不到（来自样式或头部）时同样不编归属。
    """

    if not error.codepoints:
        return [_measurement_unavailable_issue()]
    detail = f"subtitle font cannot render {', '.join(_glyph_label(c) for c in error.codepoints)}"
    if error.fonts:
        detail = f"{detail}; font: {', '.join(error.fonts)}"
    issues = [_measurement_unavailable_issue(detail)]
    for codepoint in error.codepoints:
        character = chr(codepoint)
        severity = "warning" if _glyph_is_invisible(codepoint) else "review"
        for cue in cues:
            for role, text in (("source", cue.source_text), ("target", cue.target_text or "")):
                if character not in text:
                    continue
                issues.append(
                    SubtitleIssue(
                        "font_missing_glyph",
                        severity,
                        cue.id,
                        f"{role} text contains {_glyph_label(codepoint)} "
                        "which the subtitle font cannot render",
                    )
                )
    return issues


def _contains_glossary_term(text: str, term: str) -> bool:
    normalized_text = text.casefold()
    normalized_term = term.casefold()
    if not normalized_term:
        return False
    if normalized_term[0].isascii() and normalized_term[0].isalnum():
        return bool(
            re.search(
                rf"(?<![a-z0-9_]){re.escape(normalized_term)}(?![a-z0-9_])",
                normalized_text,
            )
        )
    return normalized_term in normalized_text


# 译文整段超宽、经 ``segment.wrap_overflowing_target`` 切成两行后都放得下时的 issue code。
TARGET_WRAPPED_CODE = "target_wrapped"
# 尝试过换行仍要 review 时追加在文案末尾；前缀 ``line width N exceeds M`` 不变，
# ``candidate_qa.overflow_limits`` 照旧能解析（D5）。
_WRAP_ATTEMPTED_SUFFIX = f" after wrapping into {TARGET_WRAP_MAX_LINES} lines"


def _wrapped_warning(lines: tuple[str, ...]) -> tuple[str, str, str]:
    widest = max(display_width(line) for line in lines)
    return (
        "warning",
        f"target wrapped into {len(lines)} lines (widest {widest} display units)",
        TARGET_WRAPPED_CODE,
    )


def _overflow_risk(
    text: str,
    language: str | None,
    role: str,
    *,
    video_width: int | None,
    video_height: int | None,
    rendered_width: tuple[int, int] | None = None,
) -> tuple[str, str, str | None] | None:
    """返回 ``(severity, message, code)``；``code`` 为 ``None`` 时由调用方按语境定。

    源文永不换行，判定与换行引入前逐字相同。译文整段超过 ``target_wrap_limit`` 时
    先按同一个入口切成两行：切得开且放得下记 ``target_wrapped`` warning，切不开或
    libass 实测仍超限才 review（文案带 ``after wrapping`` 后缀）。整段不超限的译文
    不进换行，报告字节与换行引入前逐字相同（D6）。
    """

    normalized = normalize_display_text(text)
    widths = [display_width(normalized)]
    soft_limit = display_width_limit(language)
    if role == "target":
        hard_limit = target_wrap_limit(language, video_width=video_width, video_height=video_height)
    else:
        hard_limit = display_width_review_limit(
            language,
            video_width=video_width,
            video_height=video_height,
        )
    wrap_attempted = role == "target" and widths[0] > hard_limit
    wrapped = (
        wrap_overflowing_target(
            normalized, language, video_width=video_width, video_height=video_height
        )
        if wrap_attempted
        else None
    )
    suffix = _WRAP_ATTEMPTED_SUFFIX if wrap_attempted else ""
    if rendered_width is not None:
        # libass 量的是 render 按同一入口切好之后的事件，宽度天然是最宽那行。
        width, limit = rendered_width
        if width > limit:
            return "review", f"{role} line width {width} exceeds {limit} ASS pixels{suffix}", None
        if wrapped:
            return _wrapped_warning(wrapped)
        if widths[0] > soft_limit:
            return (
                "warning",
                f"{role} line width {widths[0]} exceeds preferred {soft_limit} display units; "
                "rendered line fits",
                None,
            )
        return None
    if widths[0] > hard_limit:
        if wrapped:
            widest = max(display_width(line) for line in wrapped)
            if widest <= hard_limit:
                return _wrapped_warning(wrapped)
            return (
                "review",
                f"{role} line width {widest} exceeds {hard_limit} display units{suffix}",
                None,
            )
        return (
            "review",
            f"{role} line width {widths[0]} exceeds {hard_limit} display units{suffix}",
            None,
        )
    if widths[0] > soft_limit:
        message = f"{role} line width {widths[0]} exceeds preferred {soft_limit} display units"
        return "warning", message, None
    return None


def target_overflow_cue_ids(
    cues: list[Cue],
    *,
    video_width: int | None = None,
    video_height: int | None = None,
) -> tuple[int, ...]:
    overflow_ids = []
    for cue in cues:
        if not cue.target_text:
            continue
        # 整段超过 target_wrap_limit 的 cue（进入过换行分支）一律计入，无论最终是
        # target_wrapped 还是 review：libass 实测仍超像素的 cue 在 QA 里是 review，
        # 重切必须能处理它，不能成为 no-op。
        limit = target_wrap_limit(
            cue.target_language, video_width=video_width, video_height=video_height
        )
        if display_width(normalize_display_text(cue.target_text)) > limit:
            overflow_ids.append(cue.id)
    return tuple(overflow_ids)


def check_subtitles(
    cues: list[Cue],
    *,
    media_duration_ms: int | None = None,
    video_width: int | None = None,
    video_height: int | None = None,
    require_translation: bool = False,
    required_terms: tuple[tuple[str, str], ...] = (),
    measure_layout: bool = False,
    render_options: AssStyleOptions = AssStyleOptions(),
) -> SubtitleQaReport:
    issues = []
    measured = {}
    layout = None
    if require_translation and measure_layout and video_width and video_height:
        layout = {
            "version": 1,
            "video_width": video_width,
            "video_height": video_height,
            "target_font": render_options.target_font,
            "source_font": render_options.source_font,
            "style_options": asdict(render_options),
            # style_options 回答「用户指定了什么」（取证与复现命令用），resolved_style 回答
            # 「实际用了什么」（绑定用）。顶层 target_font / source_font 是前者的历史快捷方式，
            # 保持原样不动。三者各答各的，别把它们当重复信息合并掉。
            #
            # 推导与 render._styled_subtitles 共用一份，测量用的就是这里记下的样式。
            # measurement 为 unavailable 时照样记：样式是确定的（不依赖测量是否成功），
            # 而那种报告本来就不会被复用——burnin 要求 measurement == "measured"。
            "resolved_style": resolved_style_binding(
                cues, video_height=video_height, options=render_options
            ),
            "measurement": "unavailable",
            # 绑定译文换行算法：烧录腿只复用 wrap 绑定一致的报告（D4）。
            "wrap": target_wrap_binding(),
        }
        try:
            measured = measure_bilingual_line_widths(
                cues, video_width=video_width, video_height=video_height, options=render_options
            )
            layout["measurement"] = "measured"
            layout["widths"] = [
                [cue_id, role, width, limit]
                for (cue_id, role), (width, limit) in sorted(measured.items())
            ]
        except FontGlyphError as error:
            issues.extend(_missing_glyph_issues(cues, error))
        except (CommandError, ValueError, OSError):
            issues.append(_measurement_unavailable_issue())
    seen_ids: set[int] = set()
    for index, cue in enumerate(cues):
        if cue.id in seen_ids:
            issues.append(
                SubtitleIssue("duplicate_cue_id", "error", cue.id, "cue IDs must be unique")
            )
        seen_ids.add(cue.id)
        if cue.end_ms <= cue.start_ms:
            issues.append(
                SubtitleIssue("invalid_timestamp", "error", cue.id, "end must be after start")
            )
            continue
        if cue.start_ms < 0 or (media_duration_ms is not None and cue.end_ms > media_duration_ms):
            issues.append(
                SubtitleIssue("outside_media", "error", cue.id, "cue is outside media duration")
            )
        if index and cue.start_ms < cues[index - 1].end_ms:
            issues.append(SubtitleIssue("overlap", "error", cue.id, "cue overlaps previous cue"))

        duration_ms = cue.end_ms - cue.start_ms
        if duration_ms < 600:
            issues.append(
                SubtitleIssue("too_short", "warning", cue.id, "cue is shorter than 600ms")
            )
        if duration_ms > 7000:
            issues.append(SubtitleIssue("too_long", "warning", cue.id, "cue is longer than 7s"))

        text = cue.target_text if require_translation else cue.source_text
        language = cue.target_language if require_translation else cue.source_language
        if require_translation and not cue.target_text:
            issues.append(
                SubtitleIssue("missing_translation", "error", cue.id, "translated text is missing")
            )
            continue
        if require_translation:
            for source_term, target_term in required_terms:
                if _contains_glossary_term(
                    cue.source_text, source_term
                ) and not _contains_glossary_term(cue.target_text or "", target_term):
                    issues.append(
                        SubtitleIssue(
                            "glossary_violation",
                            "error",
                            cue.id,
                            f"glossary term {source_term!r} requires {target_term!r}",
                        )
                    )
        if len(text.splitlines()) > 2:
            issues.append(
                SubtitleIssue("too_many_lines", "warning", cue.id, "cue exceeds two lines")
            )

        if require_translation:
            # 源文与译文各自独立判定：任一侧超宽都要单独产出一条 issue，
            # 否则源文超宽会吞掉译文超宽，下游按 "target line width " 前缀筛选的阻断项会丢失。
            for overflow in (
                _overflow_risk(
                    cue.source_text,
                    cue.source_language,
                    "source",
                    video_width=video_width,
                    video_height=video_height,
                    rendered_width=measured.get((cue.id, "source")),
                ),
                _overflow_risk(
                    cue.target_text or "",
                    cue.target_language,
                    "target",
                    video_width=video_width,
                    video_height=video_height,
                    rendered_width=measured.get((cue.id, "target")),
                ),
            ):
                if overflow:
                    severity, message, code = overflow
                    issues.append(
                        SubtitleIssue(code or "bilingual_overflow_risk", severity, cue.id, message)
                    )
        else:
            overflow = _overflow_risk(
                cue.source_text,
                cue.source_language,
                "source",
                video_width=video_width,
                video_height=video_height,
            )
            if overflow:
                severity, message, _code = overflow
                issues.append(SubtitleIssue("line_too_wide", severity, cue.id, message))

        units_per_second = reading_units(text, language) / (duration_ms / 1000)
        limit = reading_speed_limit(language)
        if units_per_second > limit:
            issues.append(
                SubtitleIssue(
                    "reading_speed",
                    "warning",
                    cue.id,
                    f"reading speed {units_per_second:.1f} exceeds {limit}/s",
                )
            )
        if cue.timing_confidence is not None and cue.timing_confidence < 0.5:
            # ASR 分段置信度偏低很常见，降为 warning 避免端到端几乎必卡确认；
            # 对齐/用户导入字幕的低置信度仍需人工复核。
            asr_derived = cue.provenance in {"asr", "asr_resegmented"}
            issues.append(
                SubtitleIssue(
                    "low_timing_confidence",
                    "warning" if asr_derived else "review",
                    cue.id,
                    (
                        "ASR timing confidence is below 0.5"
                        if asr_derived
                        else "timeline requires manual review"
                    ),
                )
            )
        if "trimmed trailing repeated ASR output with unreliable timestamps" in cue.warnings:
            issues.append(
                SubtitleIssue(
                    "tail_hallucination_trimmed",
                    "warning",
                    cue.id,
                    "trimmed trailing repeated ASR output with unreliable timestamps",
                )
            )

    severities = {issue.severity for issue in issues}
    if "error" in severities:
        status = "failed"
    elif "review" in severities:
        status = "needs_review"
    elif "warning" in severities:
        status = "warning"
    else:
        status = "pass"
    return SubtitleQaReport(status=status, cue_count=len(cues), issues=tuple(issues), layout=layout)

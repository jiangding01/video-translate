from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from xvt.models import Cue
from xvt.process import CommandError
from xvt.render import AssStyleOptions, measure_bilingual_line_widths
from xvt.subtitles import normalize_display_text
from xvt.textwidth import (
    character_limit,
    display_width,
    display_width_limit,
    display_width_review_limit,
    is_cjk_language,
    reading_speed_limit,
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
    "reading_speed_limit",
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


def _reading_units(text: str, language: str | None) -> int:
    compact = "".join(character for character in text if not character.isspace())
    if is_cjk_language(language):
        return len(compact)
    return len(text.replace("\n", ""))


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


def _overflow_risk(
    text: str,
    language: str | None,
    role: str,
    *,
    video_width: int | None,
    video_height: int | None,
    rendered_width: tuple[int, int] | None = None,
) -> tuple[str, str] | None:
    widths = [display_width(normalize_display_text(text))]
    soft_limit = display_width_limit(language)
    if rendered_width is not None:
        width, limit = rendered_width
        if width > limit:
            return "review", f"{role} line width {width} exceeds {limit} ASS pixels"
        if widths[0] > soft_limit:
            return (
                "warning",
                f"{role} line width {widths[0]} exceeds preferred {soft_limit} display units; "
                "rendered line fits",
            )
        return None
    hard_limit = display_width_review_limit(
        language,
        video_width=video_width,
        video_height=video_height,
    )
    if widths[0] > hard_limit:
        return "review", f"{role} line width {widths[0]} exceeds {hard_limit} display units"
    if widths[0] > soft_limit:
        message = f"{role} line width {widths[0]} exceeds preferred {soft_limit} display units"
        return "warning", message
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
        overflow = _overflow_risk(
            cue.target_text,
            cue.target_language,
            "target",
            video_width=video_width,
            video_height=video_height,
        )
        if overflow and overflow[0] == "review":
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
            "measurement": "unavailable",
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
        except (CommandError, ValueError, OSError):
            issues.append(
                SubtitleIssue(
                    "layout_measurement_unavailable",
                    "warning",
                    0,
                    "rendered width unavailable; conservative display-unit limits applied",
                )
            )
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
                    severity, message = overflow
                    issues.append(
                        SubtitleIssue("bilingual_overflow_risk", severity, cue.id, message)
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
                severity, message = overflow
                issues.append(SubtitleIssue("line_too_wide", severity, cue.id, message))

        units_per_second = _reading_units(text, language) / (duration_ms / 1000)
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

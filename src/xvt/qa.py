from __future__ import annotations

from dataclasses import dataclass
from unicodedata import east_asian_width

from xvt.models import Cue
from xvt.subtitles import normalize_display_text


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


def is_cjk_language(language: str | None) -> bool:
    return bool(language and language.casefold().startswith(("zh", "ja", "ko")))


def character_limit(language: str | None) -> int:
    return 18 if is_cjk_language(language) else 64


def reading_speed_limit(language: str | None) -> int:
    return 9 if is_cjk_language(language) else 20


def display_width(text: str) -> int:
    return sum(2 if east_asian_width(character) in {"W", "F"} else 1 for character in text)


def display_width_limit(language: str | None) -> int:
    limit = character_limit(language)
    return limit * 2 if is_cjk_language(language) else limit


def display_width_review_limit(
    language: str | None,
    *,
    video_width: int | None = None,
    video_height: int | None = None,
) -> int:
    if is_cjk_language(language):
        return display_width_limit(language)
    if not video_width or not video_height:
        return 72
    aspect_ratio = video_width / video_height
    if aspect_ratio < 1.2:
        return 56
    if aspect_ratio < 1.6:
        return 64
    return 72


def _reading_units(text: str, language: str | None) -> int:
    compact = "".join(character for character in text if not character.isspace())
    if is_cjk_language(language):
        return len(compact)
    return len(text.replace("\n", ""))


def _overflow_risk(
    text: str,
    language: str | None,
    role: str,
    *,
    video_width: int | None,
    video_height: int | None,
) -> tuple[str, str] | None:
    widths = [display_width(normalize_display_text(text))]
    soft_limit = display_width_limit(language)
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
) -> SubtitleQaReport:
    issues = []
    for index, cue in enumerate(cues):
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
        if len(text.splitlines()) > 2:
            issues.append(
                SubtitleIssue("too_many_lines", "warning", cue.id, "cue exceeds two lines")
            )

        if require_translation:
            overflow = _overflow_risk(
                cue.source_text,
                cue.source_language,
                "source",
                video_width=video_width,
                video_height=video_height,
            )
            overflow = overflow or _overflow_risk(
                cue.target_text or "",
                cue.target_language,
                "target",
                video_width=video_width,
                video_height=video_height,
            )
            if overflow:
                severity, message = overflow
                issues.append(SubtitleIssue("bilingual_overflow_risk", severity, cue.id, message))
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
            issues.append(
                SubtitleIssue(
                    "low_timing_confidence",
                    "review",
                    cue.id,
                    "timeline requires manual review",
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
    return SubtitleQaReport(status=status, cue_count=len(cues), issues=tuple(issues))

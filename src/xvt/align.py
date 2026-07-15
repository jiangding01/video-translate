from __future__ import annotations

from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from statistics import median
from unicodedata import category

from xvt.models import Cue, Transcript, Word


@dataclass(frozen=True, slots=True)
class AlignmentMatch:
    cue_id: int
    word_start: int
    word_end: int
    score: float


@dataclass(frozen=True, slots=True)
class AlignmentResult:
    cues: tuple[Cue, ...]
    matches: tuple[AlignmentMatch, ...]
    offset_ms: float
    scale: float
    confidence: float
    warnings: tuple[str, ...]


def normalize_text(text: str) -> str:
    return "".join(
        character.casefold()
        for character in text
        if not category(character).startswith(("P", "Z", "C"))
    )


def _match_cues(
    cues: list[Cue],
    words: tuple[Word, ...],
    *,
    minimum_score: float,
    lookahead_words: int,
    maximum_window_words: int,
) -> list[AlignmentMatch]:
    matches = []
    cursor = 0
    for cue in cues:
        needle = normalize_text(cue.source_text)
        if not needle:
            continue

        best: AlignmentMatch | None = None
        last_start = min(len(words), cursor + lookahead_words)
        for start in range(cursor, last_start):
            combined = ""
            last_end = min(len(words), start + maximum_window_words)
            for end in range(start + 1, last_end + 1):
                combined += words[end - 1].text
                score = SequenceMatcher(None, needle, normalize_text(combined)).ratio()
                if best is None or score > best.score:
                    best = AlignmentMatch(cue.id, start, end, score)
                if len(normalize_text(combined)) > len(needle) * 2 + 8:
                    break

        if best and best.score >= minimum_score:
            matches.append(best)
            cursor = best.word_end
    return matches


def _fit_timeline(
    cues_by_id: dict[int, Cue],
    words: tuple[Word, ...],
    matches: list[AlignmentMatch],
) -> tuple[float, float]:
    points = []
    for match in matches:
        cue = cues_by_id[match.cue_id]
        subtitle_midpoint = (cue.start_ms + cue.end_ms) / 2
        audio_midpoint = (words[match.word_start].start_ms + words[match.word_end - 1].end_ms) / 2
        points.append((subtitle_midpoint, audio_midpoint))

    if not points:
        return 0.0, 1.0
    if len(points) == 1:
        return points[0][1] - points[0][0], 1.0

    x_mean = sum(point[0] for point in points) / len(points)
    y_mean = sum(point[1] for point in points) / len(points)
    variance = sum((point[0] - x_mean) ** 2 for point in points)
    if variance == 0:
        return median(point[1] - point[0] for point in points), 1.0
    scale = sum((x - x_mean) * (y - y_mean) for x, y in points) / variance
    offset = y_mean - scale * x_mean

    residuals = [abs((scale * x + offset) - y) for x, y in points]
    retained = [
        point for point, residual in zip(points, residuals, strict=True) if residual <= 1000
    ]
    if 1 < len(retained) < len(points):
        return _fit_points(retained)
    return offset, scale


def _fit_points(points: list[tuple[float, float]]) -> tuple[float, float]:
    x_mean = sum(point[0] for point in points) / len(points)
    y_mean = sum(point[1] for point in points) / len(points)
    variance = sum((point[0] - x_mean) ** 2 for point in points)
    if variance == 0:
        return median(point[1] - point[0] for point in points), 1.0
    scale = sum((x - x_mean) * (y - y_mean) for x, y in points) / variance
    return y_mean - scale * x_mean, scale


def align_source_cues(
    cues: list[Cue],
    transcript: Transcript,
    *,
    minimum_score: float = 0.6,
    lookahead_words: int = 30,
    maximum_window_words: int = 24,
) -> AlignmentResult:
    if not cues or not transcript.words:
        return AlignmentResult(tuple(cues), (), 0.0, 1.0, 0.0, ("no alignment data",))

    matches = _match_cues(
        cues,
        transcript.words,
        minimum_score=minimum_score,
        lookahead_words=lookahead_words,
        maximum_window_words=maximum_window_words,
    )
    cues_by_id = {cue.id: cue for cue in cues}
    offset, scale = _fit_timeline(cues_by_id, transcript.words, matches)
    matches_by_id = {match.cue_id: match for match in matches}
    aligned = []
    for cue in cues:
        match = matches_by_id.get(cue.id)
        if match:
            matched_words = transcript.words[match.word_start : match.word_end]
            aligned.append(
                replace(
                    cue,
                    start_ms=matched_words[0].start_ms,
                    end_ms=matched_words[-1].end_ms,
                    words=list(matched_words),
                    timing_confidence=match.score,
                    provenance="aligned",
                )
            )
        else:
            aligned.append(
                replace(
                    cue,
                    start_ms=max(0, round(cue.start_ms * scale + offset)),
                    end_ms=max(0, round(cue.end_ms * scale + offset)),
                    timing_confidence=0.0,
                    warnings=[*cue.warnings, "timeline inferred from neighboring anchors"],
                )
            )

    for index in range(1, len(aligned)):
        previous = aligned[index - 1]
        current = aligned[index]
        if current.start_ms < previous.end_ms:
            boundary = (current.start_ms + previous.end_ms) // 2
            previous.end_ms = boundary
            current.start_ms = boundary

    coverage = len(matches) / len(cues)
    confidence = coverage * (sum(match.score for match in matches) / len(matches)) if matches else 0
    warnings = []
    if coverage < 0.7:
        warnings.append("less than 70% of subtitle cues matched ASR words")
    if not 0.95 <= scale <= 1.05:
        warnings.append("detected unusually large subtitle timeline drift")
    return AlignmentResult(
        cues=tuple(aligned),
        matches=tuple(matches),
        offset_ms=offset,
        scale=scale,
        confidence=confidence,
        warnings=tuple(warnings),
    )

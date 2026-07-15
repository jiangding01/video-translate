from __future__ import annotations

from dataclasses import dataclass
from math import ceil

from xvt.models import Cue, Transcript, Word
from xvt.qa import display_width, display_width_limit

SENTENCE_END = frozenset(".?!。？！…")
SOFT_BREAK = frozenset(",;:，；：、")


@dataclass(frozen=True, slots=True)
class SegmentationConfig:
    pause_ms: int = 650
    max_duration_ms: int = 7000
    max_characters: int | None = None
    tail_padding_ms: int = 150
    hallucination_repeat_words: int = 3
    hallucination_confidence: float = 0.5
    pathological_repeat_words: int = 8
    pathological_zero_duration_ratio: float = 0.5


def _text(words: list[Word]) -> str:
    return "".join(word.text for word in words).strip()


def _to_cue(words: list[Word], language: str) -> Cue:
    probabilities = [word.probability for word in words if word.probability is not None]
    confidence = sum(probabilities) / len(probabilities) if probabilities else None
    return Cue(
        id=0,
        start_ms=words[0].start_ms,
        end_ms=words[-1].end_ms,
        source_text=_text(words),
        words=list(words),
        source_language=language,
        text_confidence=confidence,
        timing_confidence=confidence,
    )


def _soft_cut(words: list[Word]) -> int | None:
    for index in range(len(words) - 2, 0, -1):
        text = words[index].text.strip()
        if text and (text[0] in SOFT_BREAK or text[-1] in SOFT_BREAK):
            return index + 1
    return None


def _split_by_width(words: list[Word], max_width: int) -> list[list[Word]]:
    total_width = display_width(_text(words))
    minimum_parts = max(1, ceil(total_width / max_width))
    for part_count in range(min(minimum_parts, len(words)), len(words) + 1):
        cuts = []
        previous = 0
        for part in range(1, part_count):
            first = previous + 1
            last = len(words) - (part_count - part)
            desired_width = total_width * part / part_count
            cut = min(
                range(first, last + 1),
                key=lambda index: abs(display_width(_text(words[:index])) - desired_width),
            )
            cuts.append(cut)
            previous = cut
        boundaries = [0, *cuts, len(words)]
        chunks = [words[start:end] for start, end in zip(boundaries, boundaries[1:])]
        if all(display_width(_text(chunk)) <= max_width for chunk in chunks):
            return chunks
    return [[word] for word in words]


def _append_cues(cues: list[Cue], words: list[Word], language: str, max_width: int) -> None:
    cues.extend(_to_cue(chunk, language) for chunk in _split_by_width(words, max_width))


def _normalize_token(word: Word) -> str:
    return "".join(character for character in word.text.casefold() if character.isalnum())


def _trim_trailing_hallucination(
    words: list[Word], config: SegmentationConfig
) -> tuple[list[Word], bool]:
    if len(words) < config.hallucination_repeat_words:
        return words, False
    repeated_token = _normalize_token(words[-1])
    if not repeated_token:
        return words, False
    repeat_start = len(words) - 1
    while repeat_start and _normalize_token(words[repeat_start - 1]) == repeated_token:
        repeat_start -= 1
    repeated = words[repeat_start:]
    repeat_count = len(repeated)
    if repeat_count < config.hallucination_repeat_words:
        return words, False

    repeated_probabilities = [word.probability for word in repeated]
    low_confidence_repeat = (
        all(probability is not None for probability in repeated_probabilities)
        and sum(probability for probability in repeated_probabilities if probability is not None)
        / repeat_count
        < config.hallucination_confidence
    )
    zero_duration_ratio = sum(word.end_ms <= word.start_ms for word in repeated) / repeat_count
    pathological_timestamps = (
        repeat_count >= config.pathological_repeat_words
        and zero_duration_ratio >= config.pathological_zero_duration_ratio
    )
    if not low_confidence_repeat and not pathological_timestamps:
        return words, False

    trim_start = repeat_start
    while trim_start:
        previous = words[trim_start - 1]
        probability = previous.probability
        if words[trim_start].start_ms - previous.end_ms >= config.pause_ms:
            break
        if previous.end_ms > previous.start_ms and (
            probability is None or probability >= config.hallucination_confidence
        ):
            break
        trim_start -= 1
    return words[:trim_start], True


def _normalize_boundaries(
    cues: list[Cue], tail_padding_ms: int, media_duration_ms: int | None
) -> None:
    for index in range(1, len(cues)):
        previous = cues[index - 1]
        current = cues[index]
        if current.start_ms < previous.end_ms:
            boundary = (current.start_ms + previous.end_ms) // 2
            previous.end_ms = boundary
            current.start_ms = boundary

    for index, cue in enumerate(cues):
        next_start = cues[index + 1].start_ms if index + 1 < len(cues) else None
        padded_end = cue.end_ms + tail_padding_ms
        limits = [padded_end]
        if next_start is not None:
            limits.append(next_start)
        if media_duration_ms is not None:
            limits.append(media_duration_ms)
        cue.end_ms = min(limits)


def segment_transcript(
    transcript: Transcript,
    config: SegmentationConfig = SegmentationConfig(),
    *,
    media_duration_ms: int | None = None,
) -> list[Cue]:
    words = [word for word in transcript.words if word.text.strip()]
    words, tail_trimmed = _trim_trailing_hallucination(words, config)
    words = [word for word in words if word.end_ms > word.start_ms]
    if not words:
        return []

    cues = []
    current: list[Word] = []
    max_width = config.max_characters or display_width_limit(transcript.language)
    for index, word in enumerate(words):
        current.append(word)
        next_word = words[index + 1] if index + 1 < len(words) else None
        gap_ms = next_word.start_ms - word.end_ms if next_word else 0
        text = word.text.rstrip()
        sentence_ended = bool(text) and text[-1] in SENTENCE_END
        paused = next_word is not None and gap_ms >= config.pause_ms
        too_long = word.end_ms - current[0].start_ms >= config.max_duration_ms

        if too_long and not sentence_ended and not paused:
            cut = _soft_cut(current)
            if cut:
                _append_cues(cues, current[:cut], transcript.language, max_width)
                current = current[cut:]
                continue

        if sentence_ended or paused or too_long or next_word is None:
            _append_cues(cues, current, transcript.language, max_width)
            current = []

    _normalize_boundaries(cues, config.tail_padding_ms, media_duration_ms)
    for cue_id, cue in enumerate(cues, 1):
        cue.id = cue_id
    if tail_trimmed:
        cues[-1].warnings.append("trimmed trailing repeated ASR output with unreliable timestamps")
    return cues

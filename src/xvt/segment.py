from __future__ import annotations

from dataclasses import dataclass
from math import ceil

from xvt.models import Cue, Transcript, Word
from xvt.qa import display_width, display_width_limit

SENTENCE_END = frozenset(".?!。？！…")
SOFT_BREAK = frozenset(",;:，；：、")
BREAK_BEFORE = frozenset(
    {
        "and",
        "as",
        "because",
        "but",
        "for",
        "if",
        "or",
        "so",
        "that",
        "to",
        "when",
        "while",
        "with",
    }
)


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


SEGMENTATION_PRESETS = {
    "conservative": SegmentationConfig(pause_ms=850, max_duration_ms=8500),
    "balanced": SegmentationConfig(),
    "compact": SegmentationConfig(pause_ms=450, max_duration_ms=5500),
}


def segmentation_config_for_preset(preset: str) -> SegmentationConfig:
    try:
        return SEGMENTATION_PRESETS[preset]
    except KeyError as error:
        raise ValueError(f"unknown segmentation preset: {preset}") from error


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


def _break_penalty(words: list[Word], index: int) -> int:
    previous = words[index - 1].text.rstrip()
    following = _normalize_token(words[index])
    if previous and previous[-1] in SENTENCE_END:
        return 0
    if previous and previous[-1] in SOFT_BREAK:
        return 1
    if following in BREAK_BEFORE:
        return 2
    return 4


def _partition_words(
    words: list[Word],
    part_count: int,
    *,
    max_width: int | None = None,
) -> list[list[Word]] | None:
    target_width = display_width(_text(words)) / part_count
    best: dict[tuple[int, int], tuple[int, list[int]]] = {(0, 0): (0, [])}
    for chunk_count in range(part_count):
        for (start, completed), (cost, cuts) in tuple(best.items()):
            if completed != chunk_count:
                continue
            last_end = len(words) - (part_count - chunk_count - 1)
            for end in range(start + 1, last_end + 1):
                width = display_width(_text(words[start:end]))
                if max_width is not None and width > max_width:
                    break
                chunk_cost = round(abs(width - target_width))
                if len(words[start:end]) == 1 and len(words) > part_count:
                    chunk_cost += max_width or round(target_width)
                if end < len(words):
                    chunk_cost += _break_penalty(words, end) * (max_width or round(target_width))
                key = (end, completed + 1)
                candidate = (cost + chunk_cost, [*cuts, end])
                if key not in best or candidate[0] < best[key][0]:
                    best[key] = candidate
    result = best.get((len(words), part_count))
    if result:
        boundaries = [0, *result[1][:-1]]
        return [words[start:end] for start, end in zip(boundaries, result[1])]
    return None


def _split_by_width(words: list[Word], max_width: int) -> list[list[Word]]:
    total_width = display_width(_text(words))
    minimum_parts = max(1, ceil(total_width / max_width))
    for part_count in range(min(minimum_parts, len(words)), len(words) + 1):
        if chunks := _partition_words(words, part_count, max_width=max_width):
            return chunks
    return [[word] for word in words]


def split_cue_by_parts(cue: Cue, part_count: int, *, tail_padding_ms: int = 150) -> list[Cue]:
    if part_count < 2:
        raise ValueError("part_count must be at least 2")
    if len(cue.words) < part_count:
        raise ValueError(f"cue {cue.id} has insufficient word timestamps for re-segmentation")
    chunks = _partition_words(cue.words, part_count)
    if chunks is None:
        raise ValueError(f"cue {cue.id} cannot be re-segmented")

    split_cues = [_to_cue(chunk, cue.source_language or "unknown") for chunk in chunks]
    for index, split_cue in enumerate(split_cues):
        split_cue.id = 0
        split_cue.provenance = "asr_resegmented"
        split_cue.start_ms = cue.start_ms if index == 0 else split_cue.start_ms
        if index + 1 < len(split_cues):
            split_cue.end_ms = min(
                split_cue.end_ms + tail_padding_ms,
                split_cues[index + 1].start_ms,
            )
        else:
            split_cue.end_ms = cue.end_ms
    return split_cues


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


MINIMUM_CUE_DURATION_MS = 1

OVERLAP_SHIFT_WARNING = "shifted cue start past the previous cue to resolve a degenerate overlap"
MINIMUM_DURATION_WARNING = "extended cue end to keep a positive duration"
# 解码器偶尔在音频末尾吐出一个 ``start_ms == end_ms`` 的词（2026-10-17 的真实素材里，
# 515 个词里恰好一个，就是那条「ご視聴ありがとうございました」尾部幻觉的后半截）。
# ``segment_transcript`` 必须丢掉它（零时长词无法构成 cue），但丢得无声无息，
# 事后没有任何产物说明它存在过。这条 warning 只是**留痕**：它不参与任何 QA 判定
# （``qa.check_subtitles`` 不读它，与 ``OVERLAP_SHIFT_WARNING`` /
# ``MINIMUM_DURATION_WARNING`` 同属纯证据），目的是让后续采样的人能从
# ``source-cues.json`` 直接捞出这种样本，再回 ``words.json`` 看词级证据。
TRAILING_ZERO_DURATION_WARNING_PREFIX = (
    "dropped zero-duration trailing ASR words before segmentation"
)
_TRAILING_ZERO_DURATION_TEXT_LIMIT = 80


def _trailing_zero_duration_words(words: list[Word]) -> list[Word]:
    """Return the maximal suffix of ``words`` that :func:`segment_transcript` drops.

    判据与那条过滤器互为取反（``end_ms <= start_ms``），所以**时间戳回退的词**
    （``end_ms < start_ms``，词级时间戳没有在任何地方被钳过）也算在内；警告文本
    同时打出首尾毫秒，读的人不必靠措辞判断是零时长还是回退。
    刻意只看**尾部**：转写中段的零时长词是另一回事（成因与量级都不同），
    这里不替它表态。
    """

    index = len(words)
    while index and words[index - 1].end_ms <= words[index - 1].start_ms:
        index -= 1
    return words[index:]


def _trailing_zero_duration_warning(dropped: list[Word]) -> str:
    text = "".join(word.text for word in dropped).strip()
    if len(text) > _TRAILING_ZERO_DURATION_TEXT_LIMIT:
        text = text[: _TRAILING_ZERO_DURATION_TEXT_LIMIT - 1] + "…"
    probabilities = [word.probability for word in dropped if word.probability is not None]
    probability = f"{max(probabilities):.3f}" if probabilities else "unknown"
    noun = "word" if len(dropped) == 1 else "words"
    return (
        f"{TRAILING_ZERO_DURATION_WARNING_PREFIX}: {len(dropped)} {noun} "
        f"spanning {dropped[0].start_ms}\u2192{dropped[-1].end_ms}ms, "
        f"max probability {probability}, text {text!r}"
    )


def _append_warning(cue: Cue, message: str) -> None:
    if message not in cue.warnings:
        cue.warnings.append(message)


def _assert_monotonic(cues: list[Cue]) -> None:
    """Fail loudly instead of emitting a timeline the QA stage would reject."""

    for index, cue in enumerate(cues):
        if cue.end_ms <= cue.start_ms:
            raise RuntimeError(f"cue {cue.id} has a non-positive duration after normalization")
        if index and cue.start_ms < cues[index - 1].end_ms:
            raise RuntimeError(f"cue {cue.id} still overlaps its predecessor after normalization")


def normalize_cue_boundaries(cues: list[Cue]) -> None:
    """Remove overlaps in place while keeping every cue's duration positive.

    Overlapping pairs are split at their midpoint, which is the historical
    behaviour. When that midpoint cannot leave both cues a positive duration
    (heavily overlapping, fully nested or non-monotonic input), the later cue is
    pushed to ``[previous.end_ms, previous.end_ms + 1]`` instead. Touching
    boundaries are valid for :func:`xvt.qa.check_subtitles` (its overlap check is
    a strict ``<``), and processing pairs in order lets the shift propagate to
    the following pair naturally.
    """

    for index, cue in enumerate(cues):
        if index:
            previous = cues[index - 1]
            if cue.start_ms < previous.end_ms:
                boundary = (cue.start_ms + previous.end_ms) // 2
                if previous.start_ms < boundary < cue.end_ms:
                    previous.end_ms = boundary
                    cue.start_ms = boundary
                else:
                    cue.start_ms = previous.end_ms
                    _append_warning(cue, OVERLAP_SHIFT_WARNING)
        if cue.end_ms <= cue.start_ms:
            cue.end_ms = cue.start_ms + MINIMUM_CUE_DURATION_MS
            _append_warning(cue, MINIMUM_DURATION_WARNING)

    _assert_monotonic(cues)


def _normalize_boundaries(
    cues: list[Cue], tail_padding_ms: int, media_duration_ms: int | None
) -> None:
    normalize_cue_boundaries(cues)

    for index, cue in enumerate(cues):
        next_start = cues[index + 1].start_ms if index + 1 < len(cues) else None
        padded_end = cue.end_ms + tail_padding_ms
        limits = [padded_end]
        if next_start is not None:
            limits.append(next_start)
        if media_duration_ms is not None:
            limits.append(media_duration_ms)
        # ``media_duration_ms`` can sit before the cue itself (short probe metadata),
        # so clamp back to a positive duration instead of emitting end <= start.
        capped_end = min(limits)
        cue.end_ms = max(cue.start_ms + MINIMUM_CUE_DURATION_MS, capped_end)
        if cue.end_ms != capped_end:
            _append_warning(cue, MINIMUM_DURATION_WARNING)

    _assert_monotonic(cues)


def segment_transcript(
    transcript: Transcript,
    config: SegmentationConfig = SegmentationConfig(),
    *,
    media_duration_ms: int | None = None,
) -> list[Cue]:
    words = [word for word in transcript.words if word.text.strip()]
    words, tail_trimmed = _trim_trailing_hallucination(words, config)
    dropped_tail = _trailing_zero_duration_words(words)
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
    if dropped_tail:
        _append_warning(cues[-1], _trailing_zero_duration_warning(dropped_tail))
    return cues

from __future__ import annotations

import re
from dataclasses import dataclass
from math import ceil

from xvt.models import Cue, Transcript, Word
from xvt.textwidth import (
    display_width,
    display_width_limit,
    display_width_review_limit,
    is_cjk_language,
)

SENTENCE_END = frozenset(".?!。？！…")
SOFT_BREAK = frozenset(",;:，；：、")
# 禁則処理 (kinsoku shori) — 哪里**不许**切，与上面三组「哪里适合切」正交。
#
# 字符类取自 JIS X 4051 / W3C JLREQ 的行頭禁則 cl-02..cl-11（闭合括号、连字符、
# 分隔标点、中点、句号、逗号、叠字记号、长音符、小字假名）与行末禁則 cl-01（起始
# 括号），并与 Unicode UAX #14 的 CL / CP / EX / IS / NS / CJ / OP 类对齐；中文侧
# 按 GB/T 15834 的行首行尾禁则补齐全角标点与货币前缀。CSS Text 3 的
# ``line-break: strict`` 把 CJ（小字假名与长音符 U+30FC）解析为 NS，字幕行短，这里
# 取 strict 一档。
#
# **刻意不含全角假名**：`ん`（U+3093）的 Line_Break 是 ID，JLREQ 的行頭禁則里也
# 没有它——「ど|んなに」这类切点是**词法**问题，不是禁则问题，这组集合挡不住，
# 不要往里加（见 docs/REVIEW_FINDINGS_2026_10_17.md 的 P7）。
#
# ASCII 直引号 `'` `"` 开闭同形，无法判定方向，两组都不收。
NO_LINE_START = frozenset(
    # cl-02 闭合括号 / UAX#14 CL, CP
    "）］｝〕〉》」』】〙〗〟｠»’”)]}"
    # cl-03 连字符 / UAX#14 NS, BA
    "‐–—〜～゠"
    # cl-04 分隔标点 / UAX#14 EX
    "！？!?‼⁇⁈⁉"
    # cl-05 中点与冒号分号 / UAX#14 NS, IS
    "・･·：；:;∶"
    # cl-06 句号 / cl-07 逗号 / UAX#14 CL, IS，以及 UAX#14 IN 的省略号
    "。．、，.,…‥〃〆"
    # cl-09 叠字记号 / UAX#14 NS
    "々〻ゝゞヽヾ"
    # cl-10 长音符 + cl-11 小字假名 / UAX#14 CJ（strict 档解析为 NS）
    "ーぁぃぅぇぉっゃゅょゎゕゖァィゥェォッャュョヮヵヶ"
    "ㇰㇱㇲㇳㇴㇵㇶㇷㇸㇹㇺㇻㇼㇽㇾㇿ"
    # GB/T 15834 行首禁则补充
    "％‰℃°"
)
NO_LINE_END = frozenset(
    # cl-01 起始括号 / UAX#14 OP
    "（［｛〔〈《「『【〘〖〝｟«‘“([{"
    # GB/T 15834 行尾禁则：货币前缀 / UAX#14 PR
    "＄￡￥$£¥"
)

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


# 禁則違反のコスト。``_break_penalty`` 的返回值会乘以 ``max_width``，普通罚分
# 最大是 4，所以 64 让任何一个禁则违反都压过现实 cue 里的宽度失衡总和。
# 刻意用**罚分**而不是硬禁：一整行全是禁则字符（例如连续的长音符）时仍然切得
# 动，不会死循环也不会返回空——兜底由结构保证，不靠额外分支。
KINSOKU_BREAK_PENALTY = 64


def forbidden_break(previous: Word, following: Word) -> bool:
    """Would a line break between these two ASR words violate 禁則処理?

    这是全仓关于「哪里**不许**切」的唯一判定。切分有**四个**产出点——
    ``segment_transcript`` 主循环的句末/停顿/超时切、``_soft_cut`` 的标点让位切、
    ``_partition_words`` 的宽度切，以及 ``wrap_target_lines`` 的 cue 内译文展示换行
    （它经 ``_partition_words`` 走一遍，切完再对每个边界硬性复核一遍）——四处都走
    这一个谓词，不要在调用点另造一份字符比较，否则规则只盖住一部分出口，比没有更糟。
    """

    tail = previous.text.rstrip()
    head = following.text.lstrip()
    if head and head[0] in NO_LINE_START:
        return True
    return bool(tail) and tail[-1] in NO_LINE_END


def _soft_cut(words: list[Word]) -> int | None:
    for index in range(len(words) - 2, 0, -1):
        text = words[index].text.strip()
        if not text or not (text[0] in SOFT_BREAK or text[-1] in SOFT_BREAK):
            continue
        if forbidden_break(words[index], words[index + 1]):
            continue
        return index + 1
    return None


def _break_penalty(words: list[Word], index: int) -> int:
    previous = words[index - 1].text.rstrip()
    following = _normalize_token(words[index])
    # 禁則判定排在最前：下面三条问的是「这里适不适合切」，而禁則问的是
    # 「这里许不许切」。例如上一个词以 ``、`` 收尾（本来是 penalty 1）而下一个
    # 词以 ``」`` 开头时，仍然不许在这里断行。
    if forbidden_break(words[index - 1], words[index]):
        return KINSOKU_BREAK_PENALTY
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


# cue 内译文展示换行的绑定：算法或行数一变就升 ``version``，让旧 QA 报告的布局
# 不再被烧录腿复用（``pipeline.burnin._reusable_rendered_layout``）。
TARGET_WRAP_VERSION = 1
TARGET_WRAP_MAX_LINES = 2

# zh / ja 译文按字切（韩语走 _LATIN_WRAP_TOKEN 按空白分词），但 Latin / 数字连续段
# （含段内空格，如 ``OpenAI GPT-5``）整体算一个 token 不拆；
# 每个 token 吞掉尾随空白，``_text`` 拼回时原样还原。
_CJK_WRAP_TOKEN = re.compile(r"(?:[A-Za-z0-9](?:[A-Za-z0-9_.+'/&-]|\s+(?=[A-Za-z0-9]))*|\S)\s*")
_LATIN_WRAP_TOKEN = re.compile(r"\S+\s*")


def _wraps_by_character(language: str | None) -> bool:
    """zh / ja 按字切；韩语词间有空格，与拉丁语系一样按空白分词。"""

    return (
        bool(language)
        and is_cjk_language(language)
        and not str(language).casefold().startswith("ko")
    )


def target_wrap_binding() -> dict[str, int]:
    """QA 报告 ``layout["wrap"]`` 的唯一来源；qa 写入与 burnin 复用判定共用这一份。"""

    return {"version": TARGET_WRAP_VERSION, "max_lines": TARGET_WRAP_MAX_LINES}


def target_wrap_limit(
    language: str | None,
    *,
    video_width: int | None = None,
    video_height: int | None = None,
) -> int:
    """译文换行的显示单位上限：译文 QA、ASS 写出与 libass 测量三处同源取这一个值。"""

    return display_width_review_limit(language, video_width=video_width, video_height=video_height)


def wrap_target_lines(
    text: str,
    language: str | None,
    *,
    max_width: int,
    max_lines: int = TARGET_WRAP_MAX_LINES,
) -> tuple[str, ...] | None:
    """把一段（已经 ``normalize_display_text`` 过的）译文确定性地切成 ``max_lines`` 行。

    分词：zh / ja 按字切（Latin / 数字连续段整体算一个 token）；韩语与拉丁语系按
    空白分词，边界只落在空格上，不会把一个词从中间劈开。
    复用 ``_partition_words`` 的宽度 DP（禁则惩罚 + 句末/软标点/连接词代价 + 孤词
    惩罚），每行显示宽度都不超过 ``max_width``。DP 里禁则只是罚分、无路可走时仍会
    给出违禁切点，分段腿可以接受，展示换行不行：切完对每个边界再过一遍
    ``forbidden_break``，违禁即返回 ``None``。切不开（行数不足、单 token、DP 无解）
    同样返回 ``None``，调用方据此维持 review。
    """

    if max_lines < 2:
        return None
    pattern = _CJK_WRAP_TOKEN if _wraps_by_character(language) else _LATIN_WRAP_TOKEN
    tokens = [Word(token, start_ms=0, end_ms=0) for token in pattern.findall(text.strip())]
    if len(tokens) < max_lines:
        return None
    chunks = _partition_words(tokens, max_lines, max_width=max_width)
    if chunks is None or len(chunks) != max_lines:
        return None
    for previous, following in zip(chunks, chunks[1:]):
        if forbidden_break(previous[-1], following[0]):
            return None
    lines = tuple(_text(chunk).strip() for chunk in chunks)
    if any(not line for line in lines):
        return None
    return lines


def wrap_overflowing_target(
    text: str,
    language: str | None,
    *,
    video_width: int | None = None,
    video_height: int | None = None,
) -> tuple[str, ...] | None:
    """整段译文超过 ``target_wrap_limit`` 时给出换行结果，不超限或切不开返回 ``None``。

    ``text`` 必须已经过 ``normalize_display_text``（归一化在切分之前，否则会折掉
    ``\\n``）。qa、``render.save_bilingual_ass`` 与 ``render.measure_bilingual_line_widths``
    都只经这一个入口决定「切不切、怎么切」，QA 量的就是烧出去的。
    """

    limit = target_wrap_limit(language, video_width=video_width, video_height=video_height)
    if display_width(text) <= limit:
        return None
    return wrap_target_lines(text, language, max_width=limit)


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
            # 禁則処理: 句末、停顿和超时都只是「该切了」的信号，不是「这里许切」。
            # 下一个词以禁则字符开头（最常见的是 ``。``+``」``）或当前词以起始
            # 括号收尾时，推迟一个词再切——``index`` 每轮都前进，所以最坏情况是
            # 把剩下的词并成一条 cue 交给宽度切分，不会卡住。
            if next_word is not None and forbidden_break(word, next_word):
                continue
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

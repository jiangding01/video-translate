"""源 cue 的确定性翻译上下文抽取：专名、术语与主题线索候选（Q7a）。

零依赖叶子（只用标准库），不读盘、不写盘、不碰 Job，也不调用任何模型。artifact
落盘与 provenance 由 ``xvt.pipeline.context`` 负责。

本模块只产出**候选**，不产出事实。它既不改写源文，也不自动成为强制术语：
``--glossary`` 的 ``TranslationPolicy.terms`` 仍是唯一进确定性 QA
（``qa.check_subtitles`` 的 ``glossary_violation``）的强约束，候选要经用户审核后
显式传回才生效。

主判据（规则 D）是「只出现大写形态**且**至少一次出现在非句首位置」。它在真实英文
人工字幕上实测的精度与召回都是 6/6（样本见
``tests/fixtures/source-en-mazda-mx5.srt``，验证用例在
``tests/test_translation_context.py``）。两条被实测否决的替代判据不要再捡回来：

- 「出现 ≥2 次才收」会漏掉 4/6 真专名——``Apple`` / ``CarPlay`` / ``Android`` /
  ``Auto`` 各只出现一次，而它们恰是最该统一译名的产品名。频次不是判据：噪声词
  同样只出现一次。
- 「只收形态上不可能是普通词的」（含数字、连字符、内部大写）精度满分但召回只有
  2/6，``Mazda`` / ``Apple`` / ``Android`` / ``Auto`` 全漏。

规则 D 同时依赖两个信号：句末标点（定句首）与大小写双形态（定「只大写」）。任一
缺失它就失效，因此**缺信号时不产出专名候选**并记 ``extractor_degraded``，而不是
输出一份不可信的清单（与 ``qa`` 侧 ``layout_measurement_unavailable`` 回退保守
规则同口径）。形态类候选（代码标识符、含数字或连字符、内部大写）不依赖这两个
信号，降级时照旧产出。

已知边界，刻意不做：

- **CJK 与德语零召回**。中日韩没有大小写形态，德语所有名词大写，规则 D 在这两类
  语料上分别是零召回与全量误收，所以它们走语言门直接降级。CJK 专名抽取需要词法
  边界（分词器），与 ``docs/REVIEW_FINDINGS_2026_10_17.md`` 里「译文切分的词法
  边界」是同一个卡点，归 Q7b 的模型腿。
- ``source_language`` 为 ``auto``（语言未知）时同样降级：规则 D 不能在不知道语言
  大小写习惯的语料上放行。
- 多词候选（``capitalized_run``）**永不进** glossary 骨架。短语级的恒等映射是个强
  主张：译文把 ``Android Auto`` 写成「安卓 Auto」就会触发 error 级
  ``glossary_violation``，而那条没有 ``--acknowledge`` 出口。短语要不要固定由人决定。
- 多词专名拆出的单词**照进骨架**，只加 ``_covered_by`` 标注，**不排除**。「被多词覆盖
  就排除」看着对，被真实样本否掉：2026-11-01 验收那份语料里 ``Claude`` 同时被
  ``Claude Code`` 与 ``Claude GitHub`` 覆盖，却出现 18 次、命中平台标题，是整份语料
  最核心的专名——排除它等于把最该固定译名的词丢出骨架。mazda 样本是同一问题的另一
  面：``Apple CarPlay`` / ``Android Auto`` 拆出的四个词各自也是合理专名，而且它们在
  语料里**只**在多词串中出现过，连「是否独立出现」都区分不了。形态上 ``Remote``
  （该删）与 ``Claude``（必留）没有任何可确定性检测的差别，差别在语义，而语义是本
  模块不做的事。所以这里只给证据、不替用户下判断。
- 非语音标记只剥 ``[...]`` 与 ``>>``（真实样本里各出现 9 次与 8 次）。``(laughs)``
  一类圆括号形态没有实测样本，不猜——圆括号也出现在正常口播里。
- ``H.264`` 这类「字母+点+数字」的型号名抓不到：点后首字符是数字，不符合任何一条
  形态规则。
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

# 产物 schema 版本与抽取器版本分开：schema 管读取方的兼容，抽取器版本管「同一份
# 源 cue 换了判据后结论可能不同」。改判据只升后者。
CONTEXT_SCHEMA_VERSION = 1
EXTRACTOR_VERSION = 1

DEGRADED_NO_CASE_SIGNAL = "no_case_signal"
DEGRADED_NO_SENTENCE_BOUNDARY = "no_sentence_boundary"

KIND_PROPER_NAME = "proper_name"
KIND_CAPITALIZED_RUN = "capitalized_run"
# 形态可辨的术语：型号、产品名、缩写（`MX-5`、`CarPlay`、`GPT-4`）。与 code_identifier
# 分开是因为后者指真正的代码符号（反引号、`foo()`、snake_case、点号路径），把
# `MX-5` 叫成 code identifier 是错的标签，而 kind 是用户要读的字段。
KIND_SHAPED_TERM = "shaped_term"
KIND_CODE_IDENTIFIER = "code_identifier"

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"

RISK_NONE = "none"
RISK_HIGH = "high"

# 语言门：规则 D 要求「句中普通名词小写」。这里只放满足该习惯的拉丁字母语言。
# 德语（de）与卢森堡语（lb）所有名词大写，一进来就是全量误收，刻意不收；中日韩
# 没有大小写形态，不在集合里即走降级。不在本集合里不是「不支持这门语言」，而是
# 「这门语言上这条判据没有依据」。
CASE_SIGNAL_LANGUAGES: frozenset[str] = frozenset(
    {
        "ca",
        "cs",
        "da",
        "en",
        "es",
        "et",
        "fi",
        "fr",
        "gl",
        "hr",
        "hu",
        "id",
        "it",
        "lt",
        "lv",
        "ms",
        "nb",
        "nl",
        "nn",
        "no",
        "pl",
        "pt",
        "ro",
        "sk",
        "sl",
        "sr",
        "sv",
        "tr",
        "vi",
    }
)

# 非语音与说话人标记，在 token 化之前剥除。不剥的话 `[Music]` / `[Applause]` 会
# 以「只大写且非句首」的姿态变成假专名。
_NON_SPEECH = re.compile(r"\[[^\[\]]*\]|>>+")

# 词 token：首字符必须是字母，词内允许数字与连字符（`MX-5`、`GPT-4`）。刻意不含
# 下划线与撇号——`don't` 切成 `don` + `t`、`Mazda's` 切成 `Mazda` + `s`，两个尾巴
# 都是小写，不会污染候选；snake_case 由 _CODE_PATTERNS 单独扫。
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9-]*")

_SENTENCE_END = frozenset(".?!")
# 句末标点与下一个词之间可能夹着引号或开括号，回看时一并跳过。逗号、分号一类**不**
# 跳过：它们表示句子没结束，正是我们要的信息。
_SKIPPABLE_BEFORE_TOKEN = frozenset("\"'“”‘’(（[【《「『")

# 形态类候选的子判据，作用在单个 token 上。不依赖大小写双形态与句首位置，所以
# 降级时照旧产出。
#
# 数字形态与连字符形态**都额外要求 token 里有大写字母**，这条是实测逼出来的：
# 只看连字符会把 `low-slung` / `two-seater` 这类普通英文复合词收成术语，而它们
# 一旦带着恒等映射进 glossary 骨架，就要求译文里出现英文原词，踩中 error 级
# `glossary_violation`（那条没有 --acknowledge 出口）。纯小写的连字符复合词是
# 构词法，不是术语。代价是 `h264` 这种全小写型号名收不到——可接受，且与「不收
# 数字与单位」的既定取舍同向。
_SHAPE_INTERNAL_CAPS = re.compile(r"^[A-Za-z][A-Za-z0-9]*[a-z][A-Z][A-Za-z0-9]*$")
_SHAPE_WITH_DIGIT = re.compile(r"^(?=[A-Za-z0-9-]*[A-Z])[A-Za-z][A-Za-z0-9-]*[0-9][A-Za-z0-9-]*$")
_SHAPE_WITH_HYPHEN = re.compile(r"^(?=[A-Za-z0-9-]*[A-Z])[A-Za-z][A-Za-z0-9]*-[A-Za-z0-9-]+$")

# 直接在文本流上扫的代码标识符形态（_TOKEN 切不出来的那些）。
_CODE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"`([^`\n]{1,64})`"),
    re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\(\)"),
    re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b"),
    re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]+)+)\b"),
)

# 只大写但几乎不可能是专名的形态：人称代词 I、口语应答词、称谓缩写。这些在英文
# 口播里普遍「只出现大写形态且出现在句中」，规则 D 的位置判据挡不住。
# **只作用于最终候选清单**，不参与 rule_d_partition 的位置判定——那个函数报的是
# 规则 D 本身的机制，掺进词表会让它的实测结论不可复核。
_PROPER_NAME_STOPWORDS: frozenset[str] = frozenset(
    {
        "ah",
        "dr",
        "hey",
        "hi",
        "mr",
        "mrs",
        "ms",
        "no",
        "oh",
        "ok",
        "okay",
        "uh",
        "um",
        "yeah",
        "yes",
    }
)

_TOPIC_HINT_LIMIT = 12
_TOPIC_HINT_MIN_LENGTH = 4
_TOPIC_HINT_MIN_COUNT = 2
_TOPIC_STOPWORDS: frozenset[str] = frozenset(
    {
        "about",
        "actually",
        "after",
        "again",
        "also",
        "always",
        "another",
        "around",
        "because",
        "been",
        "before",
        "being",
        "below",
        "best",
        "better",
        "both",
        "came",
        "come",
        "could",
        "doesn",
        "does",
        "done",
        "down",
        "even",
        "ever",
        "every",
        "from",
        "gets",
        "going",
        "gone",
        "good",
        "have",
        "here",
        "into",
        "just",
        "kind",
        "know",
        "like",
        "little",
        "long",
        "look",
        "made",
        "make",
        "many",
        "more",
        "most",
        "much",
        "need",
        "never",
        "only",
        "other",
        "over",
        "pretty",
        "quite",
        "really",
        "right",
        "same",
        "says",
        "should",
        "since",
        "some",
        "something",
        "still",
        "such",
        "sure",
        "take",
        "than",
        "that",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "thing",
        "things",
        "think",
        "this",
        "those",
        "through",
        "time",
        "very",
        "want",
        "well",
        "went",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "will",
        "with",
        "without",
        "would",
        "your",
    }
)

# 单条候选记多少个 cue id 作为证据。够人工抽查，又不让产物随片长线性膨胀；
# 真实出现次数另记在 occurrence_count 里，不靠 len(cue_ids) 推。
_EVIDENCE_CUE_ID_LIMIT = 10


@dataclass(frozen=True, slots=True)
class _Token:
    surface: str
    start: int
    cue_id: int
    sentence_initial: bool


def normalize_language(source_language: str | None) -> str:
    """把 ``en-US`` / ``zh_Hans`` 一类标签归一到主语言子标签的小写形式。"""

    if not source_language:
        return ""
    return re.split(r"[-_]", source_language.strip())[0].casefold()


def language_has_case_signal(source_language: str | None) -> bool:
    """该语言的「句中普通名词小写」习惯是否成立（规则 D 的前提）。"""

    return normalize_language(source_language) in CASE_SIGNAL_LANGUAGES


def strip_non_speech(text: str) -> str:
    """剥掉 ``[...]`` 非语音标记与 ``>>`` 说话人标记。必须在 token 化之前调用。"""

    return _NON_SPEECH.sub(" ", text)


def build_text_stream(cues: Sequence[Any]) -> tuple[str, tuple[tuple[int, int, int], ...]]:
    """把全部源 cue 按 id 顺序拼成一条文本流，并返回 ``(start, end, cue_id)`` 区间。

    句边界靠句末标点判定，**不是** cue 边界：真实字幕里 cue 经常切在句中
    （fixture 里 cue 3 的 ``...for the seventh year. And`` 接 cue 4 的
    ``here's why...`` 就是一例），逐 cue 判句首必然判错。
    """

    pieces: list[str] = []
    spans: list[tuple[int, int, int]] = []
    offset = 0
    for cue in sorted(cues, key=lambda item: item.id):
        text = " ".join(strip_non_speech(cue.source_text or "").split())
        if not text:
            continue
        if pieces:
            pieces.append(" ")
            offset += 1
        spans.append((offset, offset + len(text), cue.id))
        pieces.append(text)
        offset += len(text)
    return "".join(pieces), tuple(spans)


def _preceding_significant(stream: str, start: int) -> str:
    index = start - 1
    while index >= 0 and (stream[index].isspace() or stream[index] in _SKIPPABLE_BEFORE_TOKEN):
        index -= 1
    return stream[index] if index >= 0 else ""


def _tokenize(stream: str, spans: Sequence[tuple[int, int, int]]) -> tuple[_Token, ...]:
    starts = [span[0] for span in spans]
    tokens: list[_Token] = []
    for match in _TOKEN.finditer(stream):
        index = bisect_right(starts, match.start()) - 1
        cue_id = spans[index][2] if index >= 0 else 0
        previous = _preceding_significant(stream, match.start())
        tokens.append(
            _Token(
                surface=match.group(),
                start=match.start(),
                cue_id=cue_id,
                sentence_initial=previous == "" or previous in _SENTENCE_END,
            )
        )
    return tuple(tokens)


def _case_forms(tokens: Iterable[_Token]) -> dict[str, set[str]]:
    forms: dict[str, set[str]] = defaultdict(set)
    for token in tokens:
        forms[token.surface.casefold()].add(token.surface)
    return forms


def _only_uppercase_form(forms: dict[str, set[str]], surface: str) -> bool:
    """该词的 casefold 桶里是否没有任何首字母小写的变体。"""

    return not any(form[:1].islower() for form in forms[surface.casefold()])


def rule_d_partition(
    cues: Sequence[Any],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """规则 D 的纯机制：返回 ``(收, 弃)``。

    两边都只含「只出现大写形态且长度 ≥ 2」的词；收的那边至少有一次非句首出现，
    弃的那边次次都在句首。**刻意不掺 ``_PROPER_NAME_STOPWORDS``**：那张词表属于
    最终候选清单的过滤，掺进来会让这个函数的实测结论（fixture 上 6 收 6 弃）
    不再复核得出规则 D 自己的精度。
    """

    stream, spans = build_text_stream(cues)
    tokens = _tokenize(stream, spans)
    forms = _case_forms(tokens)
    mid_sentence: dict[str, bool] = {}
    for token in tokens:
        if len(token.surface) < 2 or not token.surface[:1].isupper():
            continue
        if not _only_uppercase_form(forms, token.surface):
            continue
        mid_sentence[token.surface] = mid_sentence.get(token.surface, False) or (
            not token.sentence_initial
        )
    collected = tuple(sorted(name for name, has_mid in mid_sentence.items() if has_mid))
    rejected = tuple(sorted(name for name, has_mid in mid_sentence.items() if not has_mid))
    return collected, rejected


def _shape_candidate(surface: str) -> bool:
    return bool(
        _SHAPE_INTERNAL_CAPS.match(surface)
        or _SHAPE_WITH_DIGIT.match(surface)
        or _SHAPE_WITH_HYPHEN.match(surface)
    )


def _capitalized_runs(tokens: Sequence[_Token]) -> list[tuple[str, tuple[int, ...]]]:
    """连续大写词串（≥2 词）。组串前先丢掉**领头**的句首词。

    不丢的话 ``The MX-5`` 会被收成一条短语候选（实测确实出现过）。只丢领头：串
    中间的词不可能是句首，那是真实的短语成分。
    """

    runs: list[tuple[str, tuple[int, ...]]] = []
    current: list[_Token] = []

    def flush() -> None:
        members = current
        while members and members[0].sentence_initial:
            members = members[1:]
        if len(members) >= 2:
            runs.append(
                (
                    " ".join(token.surface for token in members),
                    tuple(dict.fromkeys(token.cue_id for token in members)),
                )
            )

    previous_end = -1
    for token in tokens:
        contiguous = current and token.start == previous_end + 1
        if token.surface[:1].isupper() and (not current or contiguous):
            current.append(token)
        else:
            flush()
            current = [token] if token.surface[:1].isupper() else []
        previous_end = token.start + len(token.surface)
    flush()
    return runs


def _variant_key(text: str) -> str:
    return re.sub(r"[-\s]", "", text).casefold()


def _surface_variants(
    tokens: Sequence[_Token], runs: Sequence[tuple[str, tuple[int, ...]]]
) -> dict[str, set[str]]:
    """``_variant_key`` 桶 -> 流里真实出现过的字面形态集合。

    拼写不一致簇靠它反向查：候选先被收进来，再回头在全文里找它的大小写 / 连字符 /
    空格变体。只看单 token 与连续两词串，不做任意字符间插空的宽松匹配——那会把
    ``Auto`` 匹配成 ``A u t o``。
    """

    buckets: dict[str, set[str]] = defaultdict(set)
    for token in tokens:
        buckets[_variant_key(token.surface)].add(token.surface)
    for index in range(len(tokens) - 1):
        first, second = tokens[index], tokens[index + 1]
        if second.start == first.start + len(first.surface) + 1:
            pair = f"{first.surface} {second.surface}"
            buckets[_variant_key(pair)].add(pair)
    for text, _ in runs:
        buckets[_variant_key(text)].add(text)
    return buckets


def _title_tokens(metadata_title: str | None) -> frozenset[str]:
    if not metadata_title:
        return frozenset()
    cleaned = strip_non_speech(metadata_title)
    return frozenset(match.group().casefold() for match in _TOKEN.finditer(cleaned))


def _topic_hints(tokens: Sequence[_Token], limit: int) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for token in tokens:
        surface = token.surface
        if not surface[:1].islower() or len(surface) < _TOPIC_HINT_MIN_LENGTH:
            continue
        folded = surface.casefold()
        if folded in _TOPIC_STOPWORDS:
            continue
        counts[folded] += 1
    ranked = sorted(
        ((text, count) for text, count in counts.items() if count >= _TOPIC_HINT_MIN_COUNT),
        key=lambda item: (-item[1], item[0]),
    )
    return [{"text": text, "count": count} for text, count in ranked[:limit]]


def _degradation(
    *,
    source_language: str | None,
    stream: str,
    tokens: Sequence[_Token],
) -> dict[str, str] | None:
    if not language_has_case_signal(source_language):
        return {
            "reason": DEGRADED_NO_CASE_SIGNAL,
            "detail": (
                "source language "
                f"{normalize_language(source_language) or 'unknown'!r} "
                "has no reliable lowercase signal for mid-sentence common nouns"
            ),
        }
    if not any(character in _SENTENCE_END for character in stream):
        return {
            "reason": DEGRADED_NO_SENTENCE_BOUNDARY,
            "detail": "source cues contain no sentence-ending punctuation",
        }
    uppercase = sum(1 for token in tokens if token.surface[:1].isupper())
    if uppercase == 0 or uppercase == len(tokens):
        return {
            "reason": DEGRADED_NO_CASE_SIGNAL,
            "detail": "source cues are single-case; capitalization carries no information",
        }
    return None


def _candidate(
    *,
    source: str,
    kind: str,
    occurrence_count: int,
    confidence: str,
    cue_ids: Sequence[int],
    metadata: Sequence[str],
    variants: Sequence[str],
) -> dict[str, Any]:
    return {
        "source": source,
        "kind": kind,
        "occurrence_count": occurrence_count,
        "confidence": confidence,
        "risk": RISK_HIGH if variants else RISK_NONE,
        "variants": list(variants),
        "evidence": {
            "cue_ids": list(cue_ids[:_EVIDENCE_CUE_ID_LIMIT]),
            "metadata": list(metadata),
        },
    }


def extract_translation_context(
    cues: Sequence[Any],
    *,
    source_language: str | None,
    metadata_title: str | None = None,
    topic_hint_limit: int = _TOPIC_HINT_LIMIT,
) -> dict[str, Any]:
    """抽取翻译上下文候选包。永不抛出——缺信号时降级并说明原因。"""

    stream, spans = build_text_stream(cues)
    tokens = _tokenize(stream, spans)
    forms = _case_forms(tokens)
    degraded = _degradation(source_language=source_language, stream=stream, tokens=tokens)
    title_tokens = _title_tokens(metadata_title)
    runs = _capitalized_runs(tokens)
    variant_buckets = _surface_variants(tokens, runs)

    occurrences: Counter[str] = Counter(token.surface for token in tokens)
    cue_ids_by_surface: dict[str, list[int]] = defaultdict(list)
    for token in tokens:
        if token.cue_id not in cue_ids_by_surface[token.surface]:
            cue_ids_by_surface[token.surface].append(token.cue_id)

    def variants_of(text: str) -> list[str]:
        found = variant_buckets.get(_variant_key(text), set())
        return sorted(form for form in found if form != text)

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    def emit(
        source: str,
        kind: str,
        *,
        occurrence_count: int,
        confidence: str,
        cue_ids: Sequence[int],
    ) -> None:
        if source in seen:
            return
        seen.add(source)
        metadata = ["title"] if source.casefold() in title_tokens else []
        candidates.append(
            _candidate(
                source=source,
                kind=kind,
                occurrence_count=occurrence_count,
                # 平台 title 是作者自己写的专名，大小写可靠，比 ASR 正文强；命中
                # 即拉满置信度。
                confidence=CONFIDENCE_HIGH if metadata else confidence,
                cue_ids=cue_ids,
                metadata=metadata,
                variants=variants_of(source),
            )
        )

    # 判定顺序决定 kind 标签（emit 按 source 去重，先到先得）：专名 → 形态术语 →
    # 代码符号。`MX-5` / `CarPlay` 两种判据都命中，而它们是专名，先走规则 D 才
    # 标得对；降级时规则 D 整段不跑，它们退为 shaped_term，标签仍如实反映依据。
    collected: tuple[str, ...] = ()
    if degraded is None:
        collected, _ = rule_d_partition(cues)
        for surface in collected:
            if surface.casefold() in _PROPER_NAME_STOPWORDS:
                continue
            emit(
                surface,
                KIND_PROPER_NAME,
                occurrence_count=occurrences[surface],
                confidence=CONFIDENCE_HIGH,
                cue_ids=cue_ids_by_surface[surface],
            )
        for text, cue_ids in runs:
            members = text.split(" ")
            if any(member.casefold() in _PROPER_NAME_STOPWORDS for member in members):
                continue
            all_rule_d = all(
                len(member) >= 2
                and member[:1].isupper()
                and _only_uppercase_form(forms, member)
                and member in collected
                for member in members
            )
            emit(
                text,
                KIND_CAPITALIZED_RUN,
                occurrence_count=stream.count(text),
                confidence=CONFIDENCE_HIGH if all_rule_d else CONFIDENCE_MEDIUM,
                cue_ids=cue_ids,
            )

    for surface in sorted({token.surface for token in tokens}):
        if _shape_candidate(surface):
            emit(
                surface,
                KIND_SHAPED_TERM,
                occurrence_count=occurrences[surface],
                confidence=CONFIDENCE_HIGH,
                cue_ids=cue_ids_by_surface[surface],
            )
    starts = [span[0] for span in spans]
    for pattern in _CODE_PATTERNS:
        for match in pattern.finditer(stream):
            text = match.group(1).strip()
            if not text or text in seen:
                continue
            index = bisect_right(starts, match.start()) - 1
            cue_id = spans[index][2] if index >= 0 else 0
            emit(
                text,
                KIND_CODE_IDENTIFIER,
                # 数这个字面量自己出现几次，不是数这条 pattern 全流匹配了几次
                # （后者会把同一个 pattern 下的别的符号一起算进来）。
                occurrence_count=stream.count(text),
                confidence=CONFIDENCE_HIGH,
                cue_ids=[cue_id],
            )

    candidates.sort(key=lambda item: (-item["occurrence_count"], item["source"]))
    return {
        "version": CONTEXT_SCHEMA_VERSION,
        "extractor": "deterministic",
        "extractor_version": EXTRACTOR_VERSION,
        "source_language": normalize_language(source_language),
        "cue_count": len(spans),
        "extractor_degraded": degraded,
        "topic_hints": _topic_hints(tokens, topic_hint_limit),
        "candidates": candidates,
    }


def _covering_runs(report: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    """casefold 后的词 -> 覆盖它的多词候选 source（字母序）。

    **用全部 ``capitalized_run`` 候选，不论 confidence。** confidence 只决定「进不进
    骨架」，不决定「这个词是不是那个短语的组成部分」这个事实成不成立，两者不是一回
    事——统一过滤条件会把它改坏。实测踩过：``Claude Code`` 是 medium（它的
    ``all_rule_d`` 不成立），只按 high 算会让 ``Claude`` 的 ``_covered_by`` 漏掉它，
    而那恰是那份语料里用户最关心的术语。
    """

    covering: dict[str, set[str]] = defaultdict(set)
    for candidate in report.get("candidates", []):
        if candidate.get("kind") != KIND_CAPITALIZED_RUN:
            continue
        phrase = str(candidate.get("source") or "").strip()
        if not phrase:
            continue
        for word in phrase.split():
            # casefold 与骨架自身的去重同口径，免得 `Auto` 与 `auto` 一个命中一个不中。
            covering[word.casefold()].add(phrase)
    return {word: tuple(sorted(phrases)) for word, phrases in covering.items()}


def glossary_skeleton_terms(report: dict[str, Any]) -> list[dict[str, Any]]:
    """候选包 -> glossary 骨架条目，按 casefold 去重。

    两条硬性约束，都来自 ``xvt.translate`` 与 ``xvt.qa`` 的实际语义：

    - ``load_translation_policy`` 对 source 的重复判定走 casefold 并**直接抛**
      ``duplicate source term``，所以同时收到 ``Auto`` 与 ``auto`` 时必须去重，
      否则导出的文件根本加载不了。
    - 只导 ``confidence == "high"`` 且**非多词**的候选。多词恒等映射是强主张
      （模块 docstring 有理由），低置信条目留在产物里备查而不进骨架。
    - 多词专名拆出的单词照进骨架，只加 ``_covered_by``（覆盖它的多词 source，字母
      序）。**不排除**——理由与那个 ``Claude`` 反例见模块 docstring。没有被覆盖的
      条目不带这个键，而不是带个空数组。
    """

    covering = _covering_runs(report)
    terms: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in report.get("candidates", []):
        if candidate.get("confidence") != CONFIDENCE_HIGH:
            continue
        if candidate.get("kind") == KIND_CAPITALIZED_RUN:
            continue
        source = str(candidate.get("source") or "").strip()
        folded = source.casefold()
        if not source or folded in seen:
            continue
        seen.add(folded)
        entry: dict[str, Any] = {
            "source": source,
            # 专名最常见的处理就是保持原文，所以预填恒等映射：用户只改真正需要
            # 译的那几条。留空会被 load_translation_policy 的非空校验直接拒掉，
            # 等于交一份不能用的文件。
            "target": source,
            "_evidence": dict(candidate.get("evidence") or {}),
            "_kind": candidate.get("kind"),
        }
        covered_by = covering.get(folded)
        if covered_by:
            entry["_covered_by"] = list(covered_by)
        terms.append(entry)
    return terms

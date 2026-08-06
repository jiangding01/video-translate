from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from xvt.config import DEFAULT_BATCH_SIZE
from xvt.errors import InternalError, UserInputError
from xvt.models import Cue
from xvt.qa import character_limit, reading_speed_limit
from xvt.segment import BREAK_BEFORE, SENTENCE_END, SOFT_BREAK

TRANSLATION_STYLES = frozenset({"faithful", "natural", "concise"})
SEMANTIC_GROUPING_VERSION = 1
TRANSLATE_AND_POLISH_TASK_VERSION = 1
SEMANTIC_REVIEW_CATEGORIES = frozenset(
    {"meaning_error", "omission", "terminology", "awkward_join", "source_suspect"}
)
SEMANTIC_REPAIR_CATEGORIES = SEMANTIC_REVIEW_CATEGORIES - {"source_suspect"}
SEMANTIC_GROUP_MAX_CUES = 20
SEMANTIC_GROUP_MAX_DURATION_MS = 30_000
SEMANTIC_GROUP_PAUSE_MS = 650
SEMANTIC_SENTENCE_END = SENTENCE_END | frozenset("؟۔।॥։՞")
_TRAILING_CLOSERS = frozenset("\"'”’»）)]}」』】》〉〕〗〙〛〞〟＂＇")


@dataclass(frozen=True, slots=True)
class GlossaryTerm:
    source: str
    target: str


@dataclass(frozen=True, slots=True)
class TranslationPolicy:
    style: str = "faithful"
    terms: tuple[GlossaryTerm, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "style": self.style,
            "terms": [asdict(term) for term in self.terms],
        }


def translation_policy_from_dict(data: object) -> TranslationPolicy:
    if data is None:
        return TranslationPolicy()
    if not isinstance(data, dict):
        raise ValueError("translation policy must be an object")
    style = data.get("style", "faithful")
    if not isinstance(style, str) or style not in TRANSLATION_STYLES:
        raise ValueError("translation policy has an unsupported style")
    terms_data = data.get("terms", [])
    if not isinstance(terms_data, list):
        raise ValueError("translation policy terms must be an array")

    terms = []
    seen_sources = set()
    for item in terms_data:
        if not isinstance(item, dict):
            raise ValueError("each translation policy term must be an object")
        source = item.get("source")
        target = item.get("target")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("translation policy term source must be non-empty text")
        if not isinstance(target, str) or not target.strip():
            raise ValueError("translation policy term target must be non-empty text")
        normalized_source = source.strip().casefold()
        if normalized_source in seen_sources:
            raise ValueError(f"translation policy contains duplicate source term: {source.strip()}")
        seen_sources.add(normalized_source)
        terms.append(GlossaryTerm(source.strip(), target.strip()))
    return TranslationPolicy(
        style=style,
        terms=tuple(sorted(terms, key=lambda term: term.source.casefold())),
    )


def load_translation_policy(
    glossary_file: str | Path | None = None,
    *,
    style: str = "faithful",
) -> TranslationPolicy:
    if style not in TRANSLATION_STYLES:
        raise ValueError(f"unsupported translation style: {style}")
    if glossary_file is None:
        return TranslationPolicy(style=style)

    path = Path(glossary_file).expanduser()
    if not path.is_file():
        raise ValueError(f"glossary file does not exist: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError("glossary file must contain JSON") from error
    terms_data = data.get("terms") if isinstance(data, dict) else None
    if not isinstance(terms_data, list):
        raise ValueError("glossary file must contain a terms array")

    terms = []
    seen_sources = set()
    for item in terms_data:
        if not isinstance(item, dict):
            raise ValueError("each glossary term must be an object")
        source = item.get("source")
        target = item.get("target")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("glossary term source must be non-empty text")
        if not isinstance(target, str) or not target.strip():
            raise ValueError("glossary term target must be non-empty text")
        normalized_source = source.strip().casefold()
        if normalized_source in seen_sources:
            raise ValueError(f"glossary contains duplicate source term: {source.strip()}")
        seen_sources.add(normalized_source)
        terms.append(GlossaryTerm(source.strip(), target.strip()))

    return translation_policy_from_dict({"style": style, "terms": [asdict(term) for term in terms]})


@dataclass(frozen=True, slots=True)
class TranslationItem:
    id: int
    source_text: str
    duration_ms: int
    max_target_characters: int


@dataclass(frozen=True, slots=True)
class TranslationBatch:
    id: int
    source_language: str
    target_language: str
    items: tuple[TranslationItem, ...]
    context_before: tuple[TranslationItem, ...]
    context_after: tuple[TranslationItem, ...]
    policy: TranslationPolicy = TranslationPolicy()
    semantic_groups: tuple[tuple[int, ...], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PolishItem:
    id: int
    source_text: str
    draft_text: str
    duration_ms: int
    max_target_characters: int


@dataclass(frozen=True, slots=True)
class PolishBatch:
    id: int
    source_language: str
    target_language: str
    items: tuple[PolishItem, ...]
    context_before: tuple[PolishItem, ...]
    context_after: tuple[PolishItem, ...]
    policy: TranslationPolicy = TranslationPolicy()
    semantic_groups: tuple[tuple[int, ...], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SemanticReviewItem:
    id: int
    source_text: str
    candidate_text: str
    duration_ms: int
    max_target_characters: int


@dataclass(frozen=True, slots=True)
class SemanticSuggestedItem:
    id: int
    text: str


@dataclass(frozen=True, slots=True)
class SemanticIssue:
    category: str
    cue_ids: tuple[int, ...]
    severity: str
    message: str
    suggested_items: tuple[SemanticSuggestedItem, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SemanticReviewBatch:
    id: int
    source_language: str
    target_language: str
    items: tuple[SemanticReviewItem, ...]
    context_before: tuple[SemanticReviewItem, ...]
    context_after: tuple[SemanticReviewItem, ...]
    policy: TranslationPolicy = TranslationPolicy()
    semantic_groups: tuple[tuple[int, ...], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SemanticRepairBatch:
    id: int
    source_language: str
    target_language: str
    items: tuple[SemanticReviewItem, ...]
    context_before: tuple[SemanticReviewItem, ...]
    context_after: tuple[SemanticReviewItem, ...]
    issues: tuple[SemanticIssue, ...]
    policy: TranslationPolicy = TranslationPolicy()
    semantic_groups: tuple[tuple[int, ...], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def agent_translation_manifest_fingerprint(payload: dict[str, Any]) -> str:
    canonical = {key: value for key, value in payload.items() if key != "fingerprint"}
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()


def _ends_with_marker(text: str, markers: frozenset[str]) -> bool:
    normalized = text.rstrip()
    while normalized and normalized[-1] in _TRAILING_CLOSERS:
        normalized = normalized[:-1].rstrip()
    return bool(normalized) and normalized[-1] in markers


def _starts_with_connector(text: str) -> bool:
    words = "".join(
        character if character.isalnum() else " " for character in text.casefold()
    ).split()
    return bool(words) and words[0] in BREAK_BEFORE


def _group_duration_ms(cues: list[Cue]) -> int:
    return cues[-1].end_ms - cues[0].start_ms


def _forced_group_cut(cues: list[Cue]) -> int:
    boundaries = range(1, len(cues))
    for predicate in (
        lambda previous, _following: _ends_with_marker(previous.source_text, SEMANTIC_SENTENCE_END),
        lambda previous, _following: _ends_with_marker(previous.source_text, SOFT_BREAK),
        lambda _previous, following: _starts_with_connector(following.source_text),
    ):
        matches = [index for index in boundaries if predicate(cues[index - 1], cues[index])]
        if matches:
            return matches[-1]
    return max(
        boundaries,
        key=lambda index: (cues[index].start_ms - cues[index - 1].end_ms, index),
    )


def build_semantic_groups(
    cues: list[Cue],
    *,
    max_cues: int = SEMANTIC_GROUP_MAX_CUES,
    max_duration_ms: int = SEMANTIC_GROUP_MAX_DURATION_MS,
    pause_ms: int = SEMANTIC_GROUP_PAUSE_MS,
) -> tuple[tuple[int, ...], ...]:
    if max_cues < 1:
        raise ValueError("semantic group max_cues must be positive")
    if max_duration_ms < 1:
        raise ValueError("semantic group max_duration_ms must be positive")
    if pause_ms < 0:
        raise ValueError("semantic group pause_ms must not be negative")

    groups: list[tuple[int, ...]] = []
    pending: list[Cue] = []
    for index, cue in enumerate(cues):
        pending.append(cue)
        while len(pending) > max_cues or (
            len(pending) > 1 and _group_duration_ms(pending) > max_duration_ms
        ):
            cut = _forced_group_cut(pending)
            groups.append(tuple(item.id for item in pending[:cut]))
            pending = pending[cut:]

        following = cues[index + 1] if index + 1 < len(cues) else None
        sentence_ended = _ends_with_marker(cue.source_text, SEMANTIC_SENTENCE_END)
        paused = following is not None and following.start_ms - cue.end_ms >= pause_ms
        if pending and (following is None or sentence_ended or paused):
            groups.append(tuple(item.id for item in pending))
            pending = []
    return tuple(groups)


def build_translation_batches(
    cues: list[Cue],
    *,
    source_language: str,
    target_language: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    context_size: int = 1,
    policy: TranslationPolicy | None = None,
) -> list[TranslationBatch]:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if context_size < 0:
        raise ValueError("context_size must not be negative")
    speed_limit = reading_speed_limit(target_language)
    line_limit = character_limit(target_language)
    resolved_policy = policy or TranslationPolicy()
    items = [
        TranslationItem(
            cue.id,
            cue.source_text,
            cue.end_ms - cue.start_ms,
            max(1, min(line_limit, (cue.end_ms - cue.start_ms) * speed_limit // 1000)),
        )
        for cue in cues
    ]
    items_by_id = {item.id: item for item in items}
    if len(items_by_id) != len(items):
        raise InternalError("source cues contains duplicate cue ids")
    semantic_groups = build_semantic_groups(
        cues,
        max_cues=min(SEMANTIC_GROUP_MAX_CUES, batch_size),
    )
    packed_groups: list[tuple[int, int]] = []
    start = 0
    while start < len(semantic_groups):
        end = start
        item_count = 0
        while end < len(semantic_groups):
            group_size = len(semantic_groups[end])
            if item_count and item_count + group_size > batch_size:
                break
            if group_size > batch_size:
                raise InternalError("semantic group exceeds translation batch capacity")
            item_count += group_size
            end += 1
        packed_groups.append((start, end))
        start = end

    batches = []
    for group_start, group_end in packed_groups:
        batch_groups = semantic_groups[group_start:group_end]
        batch_items = tuple(items_by_id[cue_id] for group in batch_groups for cue_id in group)
        before_groups = semantic_groups[max(0, group_start - context_size) : group_start]
        after_groups = semantic_groups[group_end : group_end + context_size]
        context_before = tuple(items_by_id[cue_id] for group in before_groups for cue_id in group)
        context_after = tuple(items_by_id[cue_id] for group in after_groups for cue_id in group)
        batches.append(
            TranslationBatch(
                id=len(batches) + 1,
                source_language=source_language,
                target_language=target_language,
                items=batch_items,
                context_before=context_before,
                context_after=context_after,
                policy=TranslationPolicy(
                    style=resolved_policy.style,
                    terms=tuple(
                        term
                        for term in resolved_policy.terms
                        if any(
                            term.source.casefold() in item.source_text.casefold()
                            for item in batch_items
                        )
                    ),
                ),
                semantic_groups=batch_groups,
            )
        )
    return batches


def build_cue_retranslation_batches(
    cues: list[Cue],
    cue_ids: tuple[int, ...],
    *,
    source_language: str,
    target_language: str,
    policy: TranslationPolicy | None = None,
    context_size: int = 2,
) -> list[TranslationBatch]:
    if not cue_ids:
        raise UserInputError("at least one cue id is required")
    if len(set(cue_ids)) != len(cue_ids):
        raise UserInputError("cue ids must not contain duplicates")

    speed_limit = reading_speed_limit(target_language)
    line_limit = character_limit(target_language)
    resolved_policy = policy or TranslationPolicy()
    items = [
        TranslationItem(
            cue.id,
            cue.source_text,
            cue.end_ms - cue.start_ms,
            max(1, min(line_limit, (cue.end_ms - cue.start_ms) * speed_limit // 1000)),
        )
        for cue in cues
    ]
    positions = {item.id: index for index, item in enumerate(items)}
    if len(positions) != len(items):
        raise InternalError("source cues contains duplicate cue ids")
    missing = sorted(set(cue_ids) - positions.keys())
    if missing:
        raise UserInputError(f"source cues do not contain ids: {missing}")

    batches = []
    for batch_id, cue_id in enumerate(cue_ids, 1):
        position = positions[cue_id]
        item = items[position]
        batches.append(
            TranslationBatch(
                id=batch_id,
                source_language=source_language,
                target_language=target_language,
                items=(item,),
                context_before=tuple(items[max(0, position - context_size) : position]),
                context_after=tuple(items[position + 1 : position + 1 + context_size]),
                policy=TranslationPolicy(
                    style=resolved_policy.style,
                    terms=tuple(
                        term
                        for term in resolved_policy.terms
                        if term.source.casefold() in item.source_text.casefold()
                    ),
                ),
                semantic_groups=((item.id,),),
            )
        )
    return batches


def build_polish_batches(
    source_cues: list[Cue],
    draft_cues: list[Cue],
    *,
    source_language: str,
    target_language: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    context_size: int = 1,
    policy: TranslationPolicy | None = None,
) -> list[PolishBatch]:
    if len(source_cues) != len(draft_cues):
        raise ValueError("draft cues must match source cues exactly")
    for source, draft in zip(source_cues, draft_cues, strict=True):
        if (
            source.id != draft.id
            or source.start_ms != draft.start_ms
            or source.end_ms != draft.end_ms
            or source.source_text != draft.source_text
        ):
            raise ValueError("draft cues must preserve source ids, timeline, order, and text")
        if not draft.target_text or not draft.target_text.strip():
            raise ValueError(f"draft translation for cue {draft.id} is empty")
        if draft.target_language and draft.target_language != target_language:
            raise ValueError("draft cue target language does not match the polish target")

    translation_batches = build_translation_batches(
        source_cues,
        source_language=source_language,
        target_language=target_language,
        batch_size=batch_size,
        context_size=context_size,
        policy=policy,
    )
    drafts_by_id = {cue.id: cue.target_text.strip() for cue in draft_cues if cue.target_text}

    def polish_item(item: TranslationItem) -> PolishItem:
        return PolishItem(
            id=item.id,
            source_text=item.source_text,
            draft_text=drafts_by_id[item.id],
            duration_ms=item.duration_ms,
            max_target_characters=item.max_target_characters,
        )

    return [
        PolishBatch(
            id=batch.id,
            source_language=batch.source_language,
            target_language=batch.target_language,
            items=tuple(polish_item(item) for item in batch.items),
            context_before=tuple(polish_item(item) for item in batch.context_before),
            context_after=tuple(polish_item(item) for item in batch.context_after),
            policy=batch.policy,
            semantic_groups=batch.semantic_groups,
        )
        for batch in translation_batches
    ]


def build_semantic_review_batches(
    source_cues: list[Cue],
    candidate_cues: list[Cue],
    *,
    source_language: str,
    target_language: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    policy: TranslationPolicy | None = None,
) -> list[SemanticReviewBatch]:
    polish_batches = build_polish_batches(
        source_cues,
        candidate_cues,
        source_language=source_language,
        target_language=target_language,
        batch_size=batch_size,
        policy=policy,
    )

    def review_item(item: PolishItem) -> SemanticReviewItem:
        return SemanticReviewItem(
            id=item.id,
            source_text=item.source_text,
            candidate_text=item.draft_text,
            duration_ms=item.duration_ms,
            max_target_characters=item.max_target_characters,
        )

    return [
        SemanticReviewBatch(
            id=batch.id,
            source_language=batch.source_language,
            target_language=batch.target_language,
            items=tuple(review_item(item) for item in batch.items),
            context_before=tuple(review_item(item) for item in batch.context_before),
            context_after=tuple(review_item(item) for item in batch.context_after),
            policy=batch.policy,
            semantic_groups=batch.semantic_groups,
        )
        for batch in polish_batches
    ]


def _issue_touches_group(issue: SemanticIssue, group: tuple[int, ...]) -> bool:
    return bool(set(issue.cue_ids) & set(group))


def build_semantic_repair_batches(
    source_cues: list[Cue],
    candidate_cues: list[Cue],
    issues: tuple[SemanticIssue, ...],
    *,
    source_language: str,
    target_language: str,
    policy: TranslationPolicy | None = None,
) -> list[SemanticRepairBatch]:
    review_batches = build_semantic_review_batches(
        source_cues,
        candidate_cues,
        source_language=source_language,
        target_language=target_language,
        batch_size=SEMANTIC_GROUP_MAX_CUES,
        policy=policy,
    )
    items = {
        item.id: item
        for batch in review_batches
        for item in (*batch.items, *batch.context_before, *batch.context_after)
    }
    groups = build_semantic_groups(source_cues)
    repairable = tuple(issue for issue in issues if issue.category in SEMANTIC_REPAIR_CATEGORIES)
    selected_groups = [
        group for group in groups if any(_issue_touches_group(i, group) for i in repairable)
    ]
    group_positions = {group: index for index, group in enumerate(groups)}
    batches: list[SemanticRepairBatch] = []
    for group in selected_groups:
        position = group_positions[group]
        previous = groups[position - 1] if position > 0 else ()
        following = groups[position + 1] if position + 1 < len(groups) else ()
        batches.append(
            SemanticRepairBatch(
                id=len(batches) + 1,
                source_language=source_language,
                target_language=target_language,
                items=tuple(items[cue_id] for cue_id in group),
                context_before=tuple(items[cue_id] for cue_id in previous),
                context_after=tuple(items[cue_id] for cue_id in following),
                issues=tuple(issue for issue in repairable if _issue_touches_group(issue, group)),
                policy=policy or TranslationPolicy(),
                semantic_groups=(group,),
            )
        )
    return batches


def parse_semantic_review_response(
    batch: SemanticReviewBatch,
    response: list[dict[str, Any]],
) -> tuple[SemanticIssue, ...]:
    owned_ids = {item.id for item in batch.items}
    allowed_ids = owned_ids | {item.id for item in batch.context_after}
    issues: list[SemanticIssue] = []
    seen: set[tuple[str, tuple[int, ...]]] = set()
    for raw in response:
        if not isinstance(raw, dict):
            raise ValueError("semantic review issue must be an object")
        category = raw.get("category")
        cue_ids_raw = raw.get("cue_ids")
        severity = raw.get("severity")
        message = raw.get("message")
        suggestions_raw = raw.get("suggested_items")
        if category not in SEMANTIC_REVIEW_CATEGORIES:
            raise ValueError("semantic review issue has an unsupported category")
        if (
            not isinstance(cue_ids_raw, list)
            or not cue_ids_raw
            or any(not isinstance(value, int) or isinstance(value, bool) for value in cue_ids_raw)
            or len(set(cue_ids_raw)) != len(cue_ids_raw)
        ):
            raise ValueError("semantic review issue cue_ids must be unique integers")
        cue_ids = tuple(sorted(cue_ids_raw))
        if not set(cue_ids) <= allowed_ids or min(cue_ids) not in owned_ids:
            raise ValueError("semantic review issue references cues outside its owned batch")
        if severity != "review":
            raise ValueError("semantic review issue severity must be review")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("semantic review issue message must be non-empty text")
        if not isinstance(suggestions_raw, list):
            raise ValueError("semantic review issue suggested_items must be an array")
        suggestions: list[SemanticSuggestedItem] = []
        suggested_ids: set[int] = set()
        for item in suggestions_raw:
            cue_id = item.get("id") if isinstance(item, dict) else None
            text = item.get("text") if isinstance(item, dict) else None
            if (
                not isinstance(cue_id, int)
                or isinstance(cue_id, bool)
                or cue_id in suggested_ids
                or cue_id not in cue_ids
                or not isinstance(text, str)
                or not text.strip()
            ):
                raise ValueError("semantic review issue contains invalid suggested_items")
            suggested_ids.add(cue_id)
            suggestions.append(SemanticSuggestedItem(cue_id, text.strip()))
        key = (category, cue_ids)
        if key in seen:
            raise ValueError("semantic review response contains a duplicate issue")
        seen.add(key)
        issues.append(
            SemanticIssue(
                category=category,
                cue_ids=cue_ids,
                severity=severity,
                message=message.strip(),
                suggested_items=tuple(suggestions),
            )
        )
    return tuple(issues)


def parse_semantic_repair_response(
    batch: SemanticRepairBatch,
    response: list[dict[str, Any]],
) -> dict[int, str]:
    expected_ids = {item.id for item in batch.items}
    repaired: dict[int, str] = {}
    for item in response:
        cue_id = item.get("id") if isinstance(item, dict) else None
        text = item.get("text") if isinstance(item, dict) else None
        if not isinstance(cue_id, int) or isinstance(cue_id, bool) or cue_id in repaired:
            raise ValueError("semantic repair response contains an invalid or duplicate cue id")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"semantic repair for cue {cue_id} is empty")
        repaired[cue_id] = text.strip()
    if set(repaired) != expected_ids:
        raise ValueError("semantic repair response must cover its complete semantic group")
    return repaired


def apply_semantic_repairs(
    candidate_cues: list[Cue],
    repaired: dict[int, str],
    expected_ids: set[int],
) -> list[Cue]:
    if set(repaired) != expected_ids:
        raise ValueError("semantic repairs must cover all affected semantic groups exactly once")
    return [
        replace(cue, target_text=repaired.get(cue.id, cue.target_text)) for cue in candidate_cues
    ]


def parse_translation_response(
    batch: TranslationBatch,
    response: list[dict[str, Any]],
) -> dict[int, str]:
    expected_ids = {item.id for item in batch.items}
    translations = {}
    for item in response:
        cue_id = item.get("id")
        text = item.get("text")
        if not isinstance(cue_id, int) or cue_id in translations:
            raise ValueError("translation response contains an invalid or duplicate cue id")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"translation for cue {cue_id} is empty")
        translations[cue_id] = text.strip()

    actual_ids = set(translations)
    if actual_ids != expected_ids:
        missing = sorted(expected_ids - actual_ids)
        extra = sorted(actual_ids - expected_ids)
        raise ValueError(
            f"translation cue ids do not match batch: missing={missing}, extra={extra}"
        )
    return translations


def parse_polish_response(
    batch: PolishBatch,
    response: list[dict[str, Any]],
) -> dict[int, str]:
    expected_ids = {item.id for item in batch.items}
    polished = {}
    for item in response:
        cue_id = item.get("id")
        text = item.get("text")
        if not isinstance(cue_id, int) or isinstance(cue_id, bool) or cue_id in polished:
            raise ValueError("polish response contains an invalid or duplicate cue id")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"polished translation for cue {cue_id} is empty")
        polished[cue_id] = text.strip()
    if set(polished) != expected_ids:
        missing = sorted(expected_ids - polished.keys())
        extra = sorted(polished.keys() - expected_ids)
        raise ValueError(f"polish cue ids do not match batch: missing={missing}, extra={extra}")
    return polished


def apply_translations(
    cues: list[Cue],
    translations: dict[int, str],
    *,
    target_language: str,
) -> list[Cue]:
    expected_ids = {cue.id for cue in cues}
    if set(translations) != expected_ids:
        raise ValueError("translations must cover every cue exactly once")
    return [
        replace(
            cue,
            target_text=translations[cue.id].strip(),
            target_language=target_language,
        )
        for cue in cues
    ]


def apply_polish_translations(
    draft_cues: list[Cue],
    polished: dict[int, str],
) -> list[Cue]:
    if set(polished) != {cue.id for cue in draft_cues}:
        raise ValueError("polished translations must cover every cue exactly once")
    return [replace(cue, target_text=polished[cue.id].strip()) for cue in draft_cues]


def map_subtitle_translations(
    source_cues: list[Cue],
    translated_cues: list[Cue],
    *,
    minimum_overlap: float = 0.5,
) -> tuple[dict[int, str], dict[str, Any]]:
    if not source_cues or not translated_cues:
        raise ValueError("source and translated subtitles must contain cues")
    if len(source_cues) == len(translated_cues):
        translations = {
            source.id: translated.target_text or ""
            for source, translated in zip(source_cues, translated_cues, strict=True)
        }
        return translations, {
            "method": "strict_order",
            "confidence": 1.0,
            "source_cues": len(source_cues),
            "translated_cues": len(translated_cues),
            "assignments": [
                {"source_id": source.id, "translated_ids": [translated.id]}
                for source, translated in zip(source_cues, translated_cues, strict=True)
            ],
        }

    assignments: dict[int, list[Cue]] = {cue.id: [] for cue in source_cues}
    overlap_scores = []
    for translated in translated_cues:
        best_source = None
        best_overlap = 0
        for source in source_cues:
            overlap = max(
                0,
                min(source.end_ms, translated.end_ms) - max(source.start_ms, translated.start_ms),
            )
            if overlap > best_overlap:
                best_source = source
                best_overlap = overlap
        duration = translated.end_ms - translated.start_ms
        overlap_ratio = best_overlap / duration
        if best_source is None or overlap_ratio < minimum_overlap:
            raise ValueError(
                f"translated cue {translated.id} has insufficient source timeline overlap"
            )
        assignments[best_source.id].append(translated)
        overlap_scores.append(overlap_ratio)

    missing = [cue_id for cue_id, cues in assignments.items() if not cues]
    if missing:
        raise ValueError(f"translated subtitles do not cover source cues: {missing}")

    translations = {
        cue_id: "\n".join(cue.target_text or "" for cue in cues)
        for cue_id, cues in assignments.items()
    }
    return translations, {
        "method": "timeline_overlap",
        "confidence": sum(overlap_scores) / len(overlap_scores),
        "source_cues": len(source_cues),
        "translated_cues": len(translated_cues),
        "assignments": [
            {"source_id": cue_id, "translated_ids": [cue.id for cue in cues]}
            for cue_id, cues in assignments.items()
        ],
    }

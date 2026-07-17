from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from xvt.models import Cue
from xvt.qa import character_limit, reading_speed_limit

TRANSLATION_STYLES = frozenset({"faithful", "natural", "concise"})


@dataclass(frozen=True, slots=True)
class GlossaryTerm:
    source: str
    target: str


@dataclass(frozen=True, slots=True)
class TranslationPolicy:
    style: str = "faithful"
    terms: tuple[GlossaryTerm, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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

    return TranslationPolicy(
        style=style,
        terms=tuple(sorted(terms, key=lambda term: term.source.casefold())),
    )


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_translation_batches(
    cues: list[Cue],
    *,
    source_language: str,
    target_language: str,
    batch_size: int = 40,
    context_size: int = 2,
    policy: TranslationPolicy | None = None,
) -> list[TranslationBatch]:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
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
    batches = []
    for start in range(0, len(items), batch_size):
        end = min(len(items), start + batch_size)
        batch_items = tuple(items[start:end])
        batches.append(
            TranslationBatch(
                id=len(batches) + 1,
                source_language=source_language,
                target_language=target_language,
                items=batch_items,
                context_before=tuple(items[max(0, start - context_size) : start]),
                context_after=tuple(items[end : end + context_size]),
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
            )
        )
    return batches


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

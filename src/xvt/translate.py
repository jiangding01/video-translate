from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

from xvt.models import Cue
from xvt.qa import character_limit, reading_speed_limit


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_translation_batches(
    cues: list[Cue],
    *,
    source_language: str,
    target_language: str,
    batch_size: int = 40,
    context_size: int = 2,
) -> list[TranslationBatch]:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    speed_limit = reading_speed_limit(target_language)
    line_limit = character_limit(target_language)
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
        batches.append(
            TranslationBatch(
                id=len(batches) + 1,
                source_language=source_language,
                target_language=target_language,
                items=tuple(items[start:end]),
                context_before=tuple(items[max(0, start - context_size) : start]),
                context_after=tuple(items[end : end + context_size]),
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

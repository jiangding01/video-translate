from __future__ import annotations

from pathlib import Path

import pysubs2

from xvt.models import Cue

SUPPORTED_SUBTITLE_EXTENSIONS = frozenset({".srt", ".vtt", ".ass", ".ssa"})


def normalize_display_text(text: str) -> str:
    return " ".join(text.split())


def load_subtitles(
    path: str | Path,
    *,
    role: str = "source",
    source_language: str | None = None,
    target_language: str | None = None,
) -> list[Cue]:
    subtitle_path = Path(path).expanduser()
    if not subtitle_path.is_file():
        raise ValueError(f"subtitle file does not exist: {subtitle_path}")
    if subtitle_path.suffix.lower() not in SUPPORTED_SUBTITLE_EXTENSIONS:
        raise ValueError(f"unsupported subtitle format: {subtitle_path.suffix}")
    if role not in {"source", "translated"}:
        raise ValueError(f"unsupported subtitle role: {role}")

    subtitles = pysubs2.load(str(subtitle_path), encoding="utf-8")
    cues = []
    for event in subtitles:
        if event.is_comment or not event.plaintext.strip():
            continue
        if event.end <= event.start:
            raise ValueError(f"invalid subtitle timestamp: {event.start} --> {event.end}")
        text = "\n".join(line.strip() for line in event.plaintext.splitlines() if line.strip())
        cue = Cue(
            id=len(cues) + 1,
            start_ms=event.start,
            end_ms=event.end,
            source_text=text if role == "source" else "",
            target_text=text if role == "translated" else None,
            source_language=source_language,
            target_language=target_language,
            timing_confidence=1.0,
            provenance=("user_source_subtitle" if role == "source" else "user_translated_subtitle"),
        )
        cues.append(cue)
    return cues


def _cue_text(cue: Cue, mode: str, target_first: bool) -> str:
    if mode == "source":
        return cue.source_text
    if not cue.target_text:
        raise ValueError(f"cue {cue.id} has no translated text")
    if mode == "translated":
        return cue.target_text
    if mode == "bilingual":
        lines = (
            (normalize_display_text(cue.target_text), normalize_display_text(cue.source_text))
            if target_first
            else (
                normalize_display_text(cue.source_text),
                normalize_display_text(cue.target_text),
            )
        )
        return r"\N".join(lines)
    raise ValueError(f"unsupported subtitle mode: {mode}")


def save_subtitles(
    cues: list[Cue],
    path: str | Path,
    *,
    mode: str = "source",
    target_first: bool = True,
) -> Path:
    subtitle_path = Path(path).expanduser()
    if subtitle_path.suffix.lower() not in SUPPORTED_SUBTITLE_EXTENSIONS:
        raise ValueError(f"unsupported subtitle format: {subtitle_path.suffix}")

    subtitles = pysubs2.SSAFile()
    for cue in cues:
        if cue.end_ms <= cue.start_ms:
            raise ValueError(f"cue {cue.id} has an invalid timestamp")
        subtitles.events.append(
            pysubs2.SSAEvent(
                start=cue.start_ms,
                end=cue.end_ms,
                text=_cue_text(cue, mode, target_first),
            )
        )

    subtitle_path.parent.mkdir(parents=True, exist_ok=True)
    subtitles.save(str(subtitle_path), encoding="utf-8")
    return subtitle_path

from __future__ import annotations

import importlib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from xvt.models import Transcript, Word

DEFAULT_MODEL = "large-v3-turbo"
DEFAULT_MLX_MODEL = "mlx-community/whisper-large-v3-turbo"


class TranscriptionError(RuntimeError):
    pass


def words_from_mlx_segments(segments: Iterable[dict[str, Any]]) -> tuple[Word, ...]:
    words = []
    for segment in segments:
        for item in segment.get("words") or []:
            if item.get("start") is None or item.get("end") is None:
                continue
            words.append(
                Word(
                    text=str(item.get("word") or ""),
                    start_ms=round(float(item["start"]) * 1000),
                    end_ms=round(float(item["end"]) * 1000),
                    probability=(
                        float(item["probability"]) if item.get("probability") is not None else None
                    ),
                )
            )
    return tuple(words)


def words_from_faster_segments(segments: Iterable[Any]) -> tuple[Word, ...]:
    words = []
    for segment in segments:
        for item in segment.words or []:
            words.append(
                Word(
                    text=str(item.word or ""),
                    start_ms=round(float(item.start) * 1000),
                    end_ms=round(float(item.end) * 1000),
                    probability=(float(item.probability) if item.probability is not None else None),
                )
            )
    return tuple(words)


def transcribe_mlx(path: Path, language: str | None = None) -> Transcript:
    try:
        mlx_whisper = importlib.import_module("mlx_whisper")
    except ImportError as error:
        raise TranscriptionError("mlx-whisper is not installed") from error

    options: dict[str, Any] = {
        "path_or_hf_repo": DEFAULT_MLX_MODEL,
        "word_timestamps": True,
        "condition_on_previous_text": False,
        "hallucination_silence_threshold": 1.0,
    }
    if language:
        options["language"] = language

    result = mlx_whisper.transcribe(str(path), **options)
    words = words_from_mlx_segments(result.get("segments") or [])
    if not words:
        raise TranscriptionError("MLX Whisper returned no word timestamps")
    return Transcript(
        language=str(result.get("language") or language or "unknown"),
        engine="mlx",
        words=words,
    )


def transcribe_faster(
    path: Path,
    language: str | None = None,
    model_name: str = DEFAULT_MODEL,
) -> Transcript:
    try:
        faster_whisper = importlib.import_module("faster_whisper")
    except ImportError as error:
        raise TranscriptionError("faster-whisper is not installed") from error

    model = faster_whisper.WhisperModel(model_name, device="cpu", compute_type="int8")
    options: dict[str, Any] = {"word_timestamps": True}
    if language:
        options["language"] = language
    segments, info = model.transcribe(str(path), **options)
    words = words_from_faster_segments(segments)
    if not words:
        raise TranscriptionError("faster-whisper returned no word timestamps")
    return Transcript(
        language=str(info.language or language or "unknown"),
        language_probability=float(info.language_probability),
        engine="faster",
        words=words,
    )


def transcribe_audio(
    path: str | Path,
    *,
    engine: str = "mlx",
    language: str | None = None,
    model_name: str = DEFAULT_MODEL,
) -> Transcript:
    audio_path = Path(path).expanduser()
    if not audio_path.is_file():
        raise ValueError(f"audio file does not exist: {audio_path}")
    audio_path = audio_path.resolve()

    if engine == "mlx":
        try:
            return transcribe_mlx(audio_path, language)
        except TranscriptionError as error:
            if "not installed" not in str(error):
                raise
            return transcribe_faster(audio_path, language, model_name)
    if engine == "faster":
        try:
            return transcribe_faster(audio_path, language, model_name)
        except TranscriptionError as error:
            if "not installed" not in str(error):
                raise
            return transcribe_mlx(audio_path, language)
    raise ValueError(f"unsupported transcription engine: {engine}")

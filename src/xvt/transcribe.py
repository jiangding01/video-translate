from __future__ import annotations

import importlib
import threading
from collections.abc import Iterable
from pathlib import Path
from time import monotonic
from typing import Any

from xvt.events import EventSink, create_event
from xvt.models import Transcript, Word

DEFAULT_MODEL = "large-v3-turbo"
DEFAULT_MLX_MODEL = "mlx-community/whisper-large-v3-turbo"


class TranscriptionError(RuntimeError):
    pass


def _emit_progress(
    event_sink: EventSink | None,
    *,
    engine: str,
    processed_ms: int | None,
    total_ms: int | None,
    elapsed_seconds: float,
    eta_seconds: float | None,
    determinate: bool,
) -> None:
    if event_sink is None:
        return
    percent = None
    if determinate and processed_ms is not None and total_ms and total_ms > 0:
        percent = round(min(processed_ms / total_ms * 100, 100), 2)
    event_sink(
        create_event(
            "transcription_progress",
            engine=engine,
            determinate=determinate,
            processed_ms=processed_ms,
            total_ms=total_ms,
            percent=percent,
            elapsed_seconds=round(elapsed_seconds, 2),
            eta_seconds=round(eta_seconds, 2) if eta_seconds is not None else None,
        )
    )


def _start_mlx_heartbeat(
    event_sink: EventSink | None,
    total_ms: int | None,
) -> tuple[float, threading.Event | None, threading.Thread | None]:
    started_at = monotonic()
    _emit_progress(
        event_sink,
        engine="mlx",
        processed_ms=0 if total_ms is not None else None,
        total_ms=total_ms,
        elapsed_seconds=0,
        eta_seconds=None,
        determinate=False,
    )
    if event_sink is None:
        return started_at, None, None

    stop = threading.Event()

    def heartbeat() -> None:
        while not stop.wait(10):
            _emit_progress(
                event_sink,
                engine="mlx",
                processed_ms=None,
                total_ms=total_ms,
                elapsed_seconds=monotonic() - started_at,
                eta_seconds=None,
                determinate=False,
            )

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    return started_at, stop, thread


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


def transcribe_mlx(
    path: Path,
    language: str | None = None,
    *,
    event_sink: EventSink | None = None,
    total_ms: int | None = None,
) -> Transcript:
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

    started_at, stop, thread = _start_mlx_heartbeat(event_sink, total_ms)
    try:
        result = mlx_whisper.transcribe(str(path), **options)
    finally:
        if stop is not None:
            stop.set()
        if thread is not None:
            thread.join()
    words = words_from_mlx_segments(result.get("segments") or [])
    if not words:
        raise TranscriptionError("MLX Whisper returned no word timestamps")
    _emit_progress(
        event_sink,
        engine="mlx",
        processed_ms=total_ms,
        total_ms=total_ms,
        elapsed_seconds=monotonic() - started_at,
        eta_seconds=0,
        determinate=True,
    )
    return Transcript(
        language=str(result.get("language") or language or "unknown"),
        engine="mlx",
        words=words,
    )


def transcribe_faster(
    path: Path,
    language: str | None = None,
    model_name: str = DEFAULT_MODEL,
    *,
    event_sink: EventSink | None = None,
    total_ms: int | None = None,
) -> Transcript:
    try:
        faster_whisper = importlib.import_module("faster_whisper")
    except ImportError as error:
        raise TranscriptionError("faster-whisper is not installed") from error

    started_at = monotonic()
    model = faster_whisper.WhisperModel(model_name, device="cpu", compute_type="int8")
    options: dict[str, Any] = {"word_timestamps": True}
    if language:
        options["language"] = language
    segments, info = model.transcribe(str(path), **options)
    reported_total_ms = round(float(info.duration) * 1000) if info.duration else total_ms
    _emit_progress(
        event_sink,
        engine="faster",
        processed_ms=0,
        total_ms=reported_total_ms,
        elapsed_seconds=monotonic() - started_at,
        eta_seconds=None,
        determinate=reported_total_ms is not None,
    )
    completed_segments = []
    processed_ms = 0
    for segment in segments:
        completed_segments.append(segment)
        processed_ms = max(processed_ms, round(float(segment.end) * 1000))
        elapsed_seconds = monotonic() - started_at
        eta_seconds = None
        if processed_ms > 0 and reported_total_ms and elapsed_seconds > 0:
            remaining_ratio = max(reported_total_ms - processed_ms, 0) / processed_ms
            eta_seconds = elapsed_seconds * remaining_ratio
        displayed_processed_ms = processed_ms
        if reported_total_ms is not None:
            displayed_processed_ms = min(processed_ms, max(reported_total_ms - 1, 0))
        _emit_progress(
            event_sink,
            engine="faster",
            processed_ms=displayed_processed_ms,
            total_ms=reported_total_ms,
            elapsed_seconds=elapsed_seconds,
            eta_seconds=eta_seconds,
            determinate=reported_total_ms is not None,
        )
    words = words_from_faster_segments(completed_segments)
    if not words:
        raise TranscriptionError("faster-whisper returned no word timestamps")
    _emit_progress(
        event_sink,
        engine="faster",
        processed_ms=reported_total_ms,
        total_ms=reported_total_ms,
        elapsed_seconds=monotonic() - started_at,
        eta_seconds=0,
        determinate=reported_total_ms is not None,
    )
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
    event_sink: EventSink | None = None,
    total_ms: int | None = None,
) -> Transcript:
    audio_path = Path(path).expanduser()
    if not audio_path.is_file():
        raise ValueError(f"audio file does not exist: {audio_path}")
    audio_path = audio_path.resolve()

    if engine == "mlx":
        try:
            return transcribe_mlx(
                audio_path,
                language,
                event_sink=event_sink,
                total_ms=total_ms,
            )
        except TranscriptionError as error:
            if "not installed" not in str(error):
                raise
            return transcribe_faster(
                audio_path,
                language,
                model_name,
                event_sink=event_sink,
                total_ms=total_ms,
            )
    if engine == "faster":
        try:
            return transcribe_faster(
                audio_path,
                language,
                model_name,
                event_sink=event_sink,
                total_ms=total_ms,
            )
        except TranscriptionError as error:
            if "not installed" not in str(error):
                raise
            return transcribe_mlx(
                audio_path,
                language,
                event_sink=event_sink,
                total_ms=total_ms,
            )
    raise ValueError(f"unsupported transcription engine: {engine}")

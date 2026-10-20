"""``run_transcription`` 的输入类型分派：远程 URL / 纯字幕 / 本地媒体。

只做参数组合校验与三条腿之间的路由，不含任何媒体或字幕算法。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from xvt.events import EventSink
from xvt.models import Job
from xvt.pipeline.common import is_subtitle_input
from xvt.pipeline.transcription.local import run_local_transcription, run_subtitle_transcription
from xvt.pipeline.transcription.remote import run_remote_transcription
from xvt.source_selection import SourceStrategy


def run_transcription(
    input_value: str,
    output_root: str | Path,
    *,
    engine: str = "mlx",
    language: str | None = None,
    subtitles: str | Path | None = None,
    source_strategy: str = "auto",
    align_subtitles: bool = False,
    minimum_alignment_confidence: float = 0.55,
    allow_low_confidence_alignment: bool = False,
    segmentation_preset: str = "balanced",
    cookies_from_browser: str | None = None,
    proxy: str | None = None,
    youtube_subtitles: bool = False,
    subtitle_language: str | None = None,
    allow_auto_subtitles: bool = False,
    workflow_config: dict[str, Any] | None = None,
    event_sink: EventSink | None = None,
    emit_job_completed: bool = True,
) -> Job:
    try:
        strategy = SourceStrategy(source_strategy)
    except ValueError as error:
        raise ValueError("source strategy must be auto, subtitles, or asr") from error
    if strategy is SourceStrategy.ASR and align_subtitles:
        raise ValueError("--source-strategy asr cannot be combined with --align-subtitles")
    parsed = urlparse(input_value)
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        return run_remote_transcription(
            input_value,
            output_root,
            engine=engine,
            language=language,
            subtitles=subtitles,
            source_strategy=source_strategy,
            align_subtitles=align_subtitles,
            minimum_alignment_confidence=minimum_alignment_confidence,
            allow_low_confidence_alignment=allow_low_confidence_alignment,
            segmentation_preset=segmentation_preset,
            cookies_from_browser=cookies_from_browser,
            proxy=proxy,
            youtube_subtitles=youtube_subtitles,
            subtitle_language=subtitle_language,
            allow_auto_subtitles=allow_auto_subtitles,
            workflow_config=workflow_config,
            event_sink=event_sink,
            emit_job_completed=emit_job_completed,
        )
    if youtube_subtitles:
        raise ValueError("--youtube-subtitles requires a remote URL input")
    if is_subtitle_input(input_value):
        if subtitles:
            raise ValueError("subtitle input cannot be combined with --subtitles")
        if align_subtitles:
            raise ValueError("--align-subtitles requires media input")
        if source_strategy == SourceStrategy.ASR:
            raise ValueError("--source-strategy asr requires media input")
        return run_subtitle_transcription(
            input_value,
            output_root,
            language=language,
            source_strategy=source_strategy,
            workflow_config=workflow_config,
            event_sink=event_sink,
            emit_job_completed=emit_job_completed,
        )
    return run_local_transcription(
        input_value,
        output_root,
        engine=engine,
        language=language,
        subtitles=subtitles,
        source_strategy=source_strategy,
        align_subtitles=align_subtitles,
        minimum_alignment_confidence=minimum_alignment_confidence,
        allow_low_confidence_alignment=allow_low_confidence_alignment,
        segmentation_preset=segmentation_preset,
        workflow_config=workflow_config,
        event_sink=event_sink,
        emit_job_completed=emit_job_completed,
    )

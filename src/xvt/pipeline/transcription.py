"""转写主链：素材获取、音频规范化、Whisper 转写、分段、对齐与转写恢复。

覆盖本地媒体、纯字幕输入与远程（YouTube）三条获取路径，以及 `resume`
使用的规范化音频 / 词级转写 / 源 cue checkpoint 恢复逻辑。
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from xvt.align import align_source_cues
from xvt.config import (
    DEFAULT_ENGINE,
    DEFAULT_MIN_ALIGNMENT_CONFIDENCE,
    DEFAULT_SEGMENTATION_PRESET,
)
from xvt.download import (
    RemoteMediaInfo,
    download_remote_media,
    download_remote_subtitles,
    inspect_remote,
    select_subtitle_language,
)
from xvt.events import EventSink, EventType, create_event, job_event_fields
from xvt.job_store import create_job, get_job_directory, get_job_file, load_job
from xvt.media import MediaInfo, probe_media
from xvt.models import Cue, Job, StageName, StageStatus, Transcript, Word
from xvt.pipeline.common import (
    _add_artifact,
    _error_category,
    _load_cues,
    _media_info_from_remote_metadata,
    _set_stage,
    _write_json,
    is_subtitle_input,
)
from xvt.pipeline.review import _complete_qa_stage
from xvt.process import run_command
from xvt.qa import check_subtitles
from xvt.recovery import valid_artifact_path
from xvt.segment import segment_transcript, segmentation_config_for_preset
from xvt.stages import (
    SUBTITLE_INPUT_SKIPS,
    TRAILING_TRANSCRIPTION_SKIPS,
    media_transcription_skips,
)
from xvt.subtitles import SUPPORTED_SUBTITLE_EXTENSIONS, load_subtitles, save_subtitles
from xvt.transcribe import transcribe_audio


def _process_acquired_media(
    job: Job,
    source: Path | None,
    media_info: Any,
    *,
    engine: str,
    language: str | None,
    subtitles: str | Path | None,
    align_subtitles: bool,
    minimum_alignment_confidence: float,
    allow_low_confidence_alignment: bool,
    event_sink: EventSink | None,
    emit_job_completed: bool = True,
) -> Job:
    job_directory = get_job_directory(job)
    artifacts_directory = job_directory / "artifacts"
    artifacts_directory.mkdir(parents=True, exist_ok=True)
    for stage in (
        *media_transcription_skips(
            has_source_subtitles=bool(subtitles),
            align_subtitles=align_subtitles,
        ),
        *TRAILING_TRANSCRIPTION_SKIPS,
    ):
        _set_stage(job, stage, StageStatus.SKIPPED, event_sink)
    current_stage = StageName.IMPORT_SUBTITLES
    try:
        imported_cues = None
        if subtitles:
            _set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
            subtitle_path = Path(subtitles).expanduser()
            if not subtitle_path.is_file():
                raise ValueError(f"subtitle file does not exist: {subtitle_path}")
            imported_path = artifacts_directory / f"input-subtitles{subtitle_path.suffix.lower()}"
            if subtitle_path.resolve() != imported_path.resolve():
                shutil.copy2(subtitle_path, imported_path)
            imported_cues = load_subtitles(
                imported_path,
                role="source",
                source_language=language,
            )
            if not imported_cues:
                raise ValueError("subtitle file contains no usable cues")
            _add_artifact(job, "input_subtitles", imported_path, event_sink)
            _set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)

        transcript = None
        if imported_cues is None or align_subtitles:
            if source is None:
                raise RuntimeError("audio acquisition is required for transcription or alignment")
            current_stage = StageName.NORMALIZE_AUDIO
            _set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
            audio_path = artifacts_directory / "audio.wav"
            run_command(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(source),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    str(audio_path),
                ],
                event_sink=event_sink,
            )
            if not audio_path.is_file():
                raise RuntimeError("FFmpeg completed without producing normalized audio")
            _add_artifact(job, "normalized_audio", audio_path, event_sink)
            _set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)

            current_stage = StageName.TRANSCRIBE
            _set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
            transcript = transcribe_audio(
                audio_path,
                engine=engine,
                language=language,
                event_sink=event_sink,
                total_ms=media_info.duration_ms,
            )
            words_path = artifacts_directory / "words.json"
            _write_json(words_path, asdict(transcript))
            _add_artifact(job, "word_transcript", words_path, event_sink)
            _set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)

        if imported_cues is not None and align_subtitles:
            current_stage = StageName.ALIGN
            _set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
            alignment = align_source_cues(imported_cues, transcript)
            cues = list(alignment.cues)
            alignment_path = artifacts_directory / "alignment.json"
            _write_json(alignment_path, asdict(alignment))
            _add_artifact(job, "subtitle_alignment", alignment_path, event_sink)
            if (
                not allow_low_confidence_alignment
                and alignment.confidence < minimum_alignment_confidence
            ):
                raise ValueError(
                    "subtitle alignment confidence is below the required threshold "
                    f"({alignment.confidence:.3f} < {minimum_alignment_confidence:.3f})"
                )
            _set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)
        elif imported_cues is not None:
            cues = imported_cues
        else:
            current_stage = StageName.SEGMENT
            _set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
            cues = segment_transcript(
                transcript,
                segmentation_config_for_preset(
                    str(job.config.get("segmentation_preset", DEFAULT_SEGMENTATION_PRESET))
                ),
                media_duration_ms=media_info.duration_ms,
            )
            if not cues:
                raise RuntimeError("transcription produced no subtitle cues")
            _set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)

        cues_path = artifacts_directory / "source-cues.json"
        _write_json(cues_path, [asdict(cue) for cue in cues])
        _add_artifact(job, "source_cues", cues_path, event_sink)

        current_stage = StageName.SUBTITLE_QA
        _set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
        report = check_subtitles(
            cues,
            media_duration_ms=media_info.duration_ms,
            video_width=media_info.width,
            video_height=media_info.height,
        )
        qa_path = artifacts_directory / "qa.json"
        _write_json(qa_path, asdict(report))
        _add_artifact(job, "subtitle_qa", qa_path, event_sink)
        _complete_qa_stage(job, report, event_sink)

        current_stage = StageName.RENDER
        _set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
        subtitle_path = save_subtitles(cues, artifacts_directory / "source.srt")
        _add_artifact(job, "source_srt", subtitle_path, event_sink)
        _set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)
    except Exception as error:
        _set_stage(
            job,
            current_stage,
            StageStatus.FAILED,
            event_sink,
            error=str(error),
            error_category=_error_category(error),
        )
        raise

    if event_sink and emit_job_completed:
        event_sink(
            create_event(
                EventType.JOB_COMPLETED,
                **job_event_fields(job.id, job_directory / "job.json"),
            )
        )
    return job


def run_local_transcription(
    input_path: str | Path,
    output_root: str | Path,
    *,
    engine: str = "mlx",
    language: str | None = None,
    subtitles: str | Path | None = None,
    align_subtitles: bool = False,
    minimum_alignment_confidence: float = 0.55,
    allow_low_confidence_alignment: bool = False,
    segmentation_preset: str = "balanced",
    workflow_config: dict[str, Any] | None = None,
    event_sink: EventSink | None = None,
    emit_job_completed: bool = True,
) -> Job:
    source = Path(input_path).expanduser()
    if not source.is_file():
        raise ValueError(f"input file does not exist: {source}")
    source = source.resolve()
    job = create_job(
        str(source),
        output_root,
        {
            "engine": engine,
            "source_language": language or "auto",
            "subtitles": str(Path(subtitles).expanduser().resolve()) if subtitles else None,
            "align_subtitles": align_subtitles,
            "minimum_alignment_confidence": minimum_alignment_confidence,
            "allow_low_confidence_alignment": allow_low_confidence_alignment,
            "segmentation_preset": segmentation_preset,
            **({"workflow": dict(workflow_config)} if workflow_config else {}),
        },
    )
    if event_sink:
        event_sink(
            create_event(
                EventType.JOB_STARTED,
                **job_event_fields(job.id, get_job_file(job)),
                input=str(source),
            )
        )

    current_stage = StageName.INSPECT
    try:
        _set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
        media_info = probe_media(source)
        media_path = get_job_directory(job) / "artifacts" / "media.json"
        media_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(media_path, asdict(media_info))
        _add_artifact(job, "media_info", media_path, event_sink)
        _set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)
        _set_stage(job, StageName.ACQUIRE, StageStatus.SKIPPED, event_sink)
    except Exception as error:
        _set_stage(
            job,
            current_stage,
            StageStatus.FAILED,
            event_sink,
            error=str(error),
            error_category=_error_category(error),
        )
        raise
    return _process_acquired_media(
        job,
        source,
        media_info,
        engine=engine,
        language=language,
        subtitles=subtitles,
        align_subtitles=align_subtitles,
        minimum_alignment_confidence=minimum_alignment_confidence,
        allow_low_confidence_alignment=allow_low_confidence_alignment,
        event_sink=event_sink,
        emit_job_completed=emit_job_completed,
    )


def _finish_subtitle_job(
    job: Job,
    cues: list[Cue],
    *,
    event_sink: EventSink | None,
) -> Job:
    artifacts_directory = get_job_directory(job) / "artifacts"
    cues_path = artifacts_directory / "source-cues.json"
    _write_json(cues_path, [asdict(cue) for cue in cues])
    _add_artifact(job, "source_cues", cues_path, event_sink)
    if job.stages[StageName.TRANSLATE].status is StageStatus.PENDING:
        _set_stage(job, StageName.TRANSLATE, StageStatus.SKIPPED, event_sink)

    current_stage = StageName.SUBTITLE_QA
    try:
        _set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
        report = check_subtitles(cues)
        qa_path = artifacts_directory / "qa.json"
        _write_json(qa_path, asdict(report))
        _add_artifact(job, "subtitle_qa", qa_path, event_sink)
        _complete_qa_stage(job, report, event_sink)

        current_stage = StageName.RENDER
        _set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
        subtitle_path = save_subtitles(cues, artifacts_directory / "source.srt")
        _add_artifact(job, "source_srt", subtitle_path, event_sink)
        _set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)
        for stage in (StageName.BURN, StageName.MEDIA_QA):
            _set_stage(job, stage, StageStatus.SKIPPED, event_sink)
    except Exception as error:
        if job.stages[current_stage].status is not StageStatus.FAILED:
            _set_stage(
                job,
                current_stage,
                StageStatus.FAILED,
                event_sink,
                error=str(error),
                error_category=_error_category(error),
            )
        raise
    return job


def run_subtitle_transcription(
    subtitle_file: str | Path,
    output_root: str | Path,
    *,
    language: str | None = None,
    workflow_config: dict[str, Any] | None = None,
    event_sink: EventSink | None = None,
    emit_job_completed: bool = True,
) -> Job:
    source = Path(subtitle_file).expanduser()
    if not source.is_file():
        raise ValueError(f"subtitle file does not exist: {source}")
    if source.suffix.lower() not in SUPPORTED_SUBTITLE_EXTENSIONS:
        raise ValueError(f"unsupported subtitle format: {source.suffix}")
    source = source.resolve()
    job = create_job(
        str(source),
        output_root,
        {
            "input_kind": "subtitle",
            "source_language": language or "auto",
            "subtitles": str(source),
            **({"workflow": dict(workflow_config)} if workflow_config else {}),
        },
    )
    if event_sink:
        event_sink(
            create_event(
                EventType.JOB_STARTED,
                **job_event_fields(job.id, get_job_file(job)),
                input=str(source),
            )
        )

    try:
        for stage in SUBTITLE_INPUT_SKIPS:
            _set_stage(job, stage, StageStatus.SKIPPED, event_sink)
        _set_stage(job, StageName.IMPORT_SUBTITLES, StageStatus.RUNNING, event_sink)
        artifacts_directory = get_job_directory(job) / "artifacts"
        artifacts_directory.mkdir(parents=True, exist_ok=True)
        imported_path = artifacts_directory / f"input-subtitles{source.suffix.lower()}"
        shutil.copy2(source, imported_path)
        cues = load_subtitles(
            imported_path,
            role="source",
            source_language=language,
        )
        if not cues:
            raise ValueError("subtitle file contains no usable cues")
        _add_artifact(job, "input_subtitles", imported_path, event_sink)
        _set_stage(job, StageName.IMPORT_SUBTITLES, StageStatus.SUCCEEDED, event_sink)
    except Exception as error:
        _set_stage(
            job,
            StageName.IMPORT_SUBTITLES,
            StageStatus.FAILED,
            event_sink,
            error=str(error),
            error_category=_error_category(error),
        )
        raise

    job = _finish_subtitle_job(job, cues, event_sink=event_sink)

    if event_sink and emit_job_completed:
        event_sink(
            create_event(EventType.JOB_COMPLETED, **job_event_fields(job.id, get_job_file(job)))
        )
    return job


def run_remote_transcription(
    url: str,
    output_root: str | Path,
    *,
    engine: str = "mlx",
    language: str | None = None,
    subtitles: str | Path | None = None,
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
    job = create_job(
        url,
        output_root,
        {
            "engine": engine,
            "source_language": language or "auto",
            "subtitles": str(Path(subtitles).expanduser().resolve()) if subtitles else None,
            "align_subtitles": align_subtitles,
            "minimum_alignment_confidence": minimum_alignment_confidence,
            "allow_low_confidence_alignment": allow_low_confidence_alignment,
            "segmentation_preset": segmentation_preset,
            "cookies_from_browser": cookies_from_browser,
            "proxy_enabled": bool(proxy),
            "youtube_subtitles": youtube_subtitles,
            "subtitle_language": subtitle_language,
            "allow_auto_subtitles": allow_auto_subtitles,
            **({"workflow": dict(workflow_config)} if workflow_config else {}),
        },
    )
    if event_sink:
        event_sink(
            create_event(
                EventType.JOB_STARTED,
                **job_event_fields(job.id, get_job_file(job)),
                input=url,
            )
        )

    try:
        source, media_info, selected_subtitles = _acquire_remote_job(
            job,
            cookies_from_browser=cookies_from_browser,
            proxy=proxy,
            event_sink=event_sink,
        )
    except Exception as error:
        current_stage = (
            StageName.INSPECT
            if job.stages[StageName.INSPECT].status is StageStatus.RUNNING
            else StageName.ACQUIRE
        )
        _set_stage(
            job,
            current_stage,
            StageStatus.FAILED,
            event_sink,
            error=str(error),
            error_category=_error_category(error),
        )
        raise
    return _process_acquired_media(
        job,
        source,
        media_info,
        engine=engine,
        language=language,
        subtitles=selected_subtitles,
        align_subtitles=align_subtitles,
        minimum_alignment_confidence=minimum_alignment_confidence,
        allow_low_confidence_alignment=allow_low_confidence_alignment,
        event_sink=event_sink,
        emit_job_completed=emit_job_completed,
    )


def _load_remote_info(path: Path) -> RemoteMediaInfo:
    data = json.loads(path.read_text(encoding="utf-8"))
    return RemoteMediaInfo(
        id=str(data["id"]),
        title=str(data["title"]),
        duration_ms=int(data["duration_ms"]),
        webpage_url=str(data["webpage_url"]),
        subtitle_languages=tuple(data.get("subtitle_languages", ())),
        automatic_subtitle_languages=tuple(data.get("automatic_subtitle_languages", ())),
    )


def _acquire_remote_job(
    job: Job,
    *,
    cookies_from_browser: str | None,
    proxy: str | None,
    event_sink: EventSink | None,
) -> tuple[Path | None, MediaInfo, str | Path | None]:
    url = job.input
    remote_path = valid_artifact_path(job, "remote_info")
    if remote_path is None:
        _set_stage(job, StageName.INSPECT, StageStatus.RUNNING, event_sink)
        remote_info = inspect_remote(
            url,
            cookies_from_browser=cookies_from_browser,
            proxy=proxy,
        )
        remote_path = get_job_directory(job) / "artifacts" / "remote.json"
        remote_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(remote_path, asdict(remote_info))
        _add_artifact(job, "remote_info", remote_path, event_sink)
    else:
        remote_info = _load_remote_info(remote_path)
    _set_stage(job, StageName.INSPECT, StageStatus.SUCCEEDED, event_sink)

    selected_subtitles = valid_artifact_path(job, "input_subtitles") or job.config.get("subtitles")
    downloaded_subtitles = valid_artifact_path(job, "downloaded_subtitles")
    acquired_subtitles = downloaded_subtitles is not None
    if downloaded_subtitles is not None:
        selected_subtitles = downloaded_subtitles
    elif job.config.get("youtube_subtitles"):
        manual_language = select_subtitle_language(
            str(job.config.get("subtitle_language") or ""),
            remote_info.subtitle_languages,
        )
        automatic_language = None
        if manual_language is None and job.config.get("allow_auto_subtitles"):
            automatic_language = select_subtitle_language(
                str(job.config.get("subtitle_language") or ""),
                remote_info.automatic_subtitle_languages,
            )
        selected_language = manual_language or automatic_language
        if selected_language:
            _set_stage(job, StageName.ACQUIRE, StageStatus.RUNNING, event_sink)
            selected_subtitles = download_remote_subtitles(
                url,
                get_job_directory(job) / "input" / "subtitles",
                language=selected_language,
                automatic=manual_language is None,
                cookies_from_browser=cookies_from_browser,
                proxy=proxy,
            )
            acquired_subtitles = True
            _add_artifact(job, "downloaded_subtitles", selected_subtitles, event_sink)
            if event_sink:
                event_sink(
                    create_event(
                        EventType.REMOTE_SUBTITLES_SELECTED,
                        **job_event_fields(job.id, get_job_file(job)),
                        language=selected_language,
                        automatic=manual_language is None,
                    )
                )

    workflow = job.config.get("workflow")
    download_for_burn = bool(isinstance(workflow, dict) and workflow.get("burn"))
    needs_audio = not selected_subtitles or bool(job.config.get("align_subtitles"))
    needs_audio = needs_audio or remote_info.duration_ms <= 0
    source = valid_artifact_path(job, "source_video" if download_for_burn else "downloaded_media")
    if source is None and download_for_burn:
        source = valid_artifact_path(job, "downloaded_media")
    if needs_audio or download_for_burn:
        _set_stage(job, StageName.ACQUIRE, StageStatus.RUNNING, event_sink)
        if source is None:
            source = download_remote_media(
                url,
                get_job_directory(job) / "input",
                audio_only=not download_for_burn,
                cookies_from_browser=cookies_from_browser,
                proxy=proxy,
                event_sink=event_sink,
            )
            _add_artifact(job, "downloaded_media", source, event_sink)
        media_info = probe_media(source)
        if download_for_burn and valid_artifact_path(job, "source_video") is None:
            _add_artifact(job, "source_video", source, event_sink)
        _set_stage(job, StageName.ACQUIRE, StageStatus.SUCCEEDED, event_sink)
    else:
        source = None
        media_info = _media_info_from_remote_metadata(url, remote_info.duration_ms)
        status = StageStatus.SUCCEEDED if acquired_subtitles else StageStatus.SKIPPED
        _set_stage(job, StageName.ACQUIRE, status, event_sink)
    media_path = get_job_directory(job) / "artifacts" / "media.json"
    _write_json(media_path, asdict(media_info))
    _add_artifact(job, "media_info", media_path, event_sink)
    return source, media_info, selected_subtitles


def resume_remote_transcription(
    job_file: str | Path,
    *,
    cookies_from_browser: str | None = None,
    proxy: str | None = None,
    event_sink: EventSink | None = None,
) -> Job:
    job = load_job(job_file)
    parsed = urlparse(job.input)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("job input is not a remote URL")
    try:
        source, media_info, subtitles = _acquire_remote_job(
            job,
            cookies_from_browser=cookies_from_browser or job.config.get("cookies_from_browser"),
            proxy=proxy,
            event_sink=event_sink,
        )
    except Exception as error:
        current_stage = (
            StageName.INSPECT
            if job.stages[StageName.INSPECT].status is StageStatus.RUNNING
            else StageName.ACQUIRE
        )
        _set_stage(
            job,
            current_stage,
            StageStatus.FAILED,
            event_sink,
            error=str(error),
            error_category=_error_category(error),
        )
        raise
    return _process_acquired_media(
        job,
        source,
        media_info,
        engine=str(job.config.get("engine", DEFAULT_ENGINE)),
        language=(
            None
            if job.config.get("source_language") == "auto"
            else str(job.config.get("source_language"))
        ),
        subtitles=subtitles,
        align_subtitles=bool(job.config.get("align_subtitles")),
        minimum_alignment_confidence=float(
            job.config.get("minimum_alignment_confidence", DEFAULT_MIN_ALIGNMENT_CONFIDENCE)
        ),
        allow_low_confidence_alignment=bool(
            job.config.get("allow_low_confidence_alignment", False)
        ),
        event_sink=event_sink,
        emit_job_completed=False,
    )


def run_transcription(
    input_value: str,
    output_root: str | Path,
    *,
    engine: str = "mlx",
    language: str | None = None,
    subtitles: str | Path | None = None,
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
    parsed = urlparse(input_value)
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        return run_remote_transcription(
            input_value,
            output_root,
            engine=engine,
            language=language,
            subtitles=subtitles,
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
        return run_subtitle_transcription(
            input_value,
            output_root,
            language=language,
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
        align_subtitles=align_subtitles,
        minimum_alignment_confidence=minimum_alignment_confidence,
        allow_low_confidence_alignment=allow_low_confidence_alignment,
        segmentation_preset=segmentation_preset,
        workflow_config=workflow_config,
        event_sink=event_sink,
        emit_job_completed=emit_job_completed,
    )


def _load_transcript(path: Path) -> Transcript:
    data = json.loads(path.read_text(encoding="utf-8"))
    return Transcript(
        language=data["language"],
        engine=data["engine"],
        words=tuple(Word(**word) for word in data["words"]),
        language_probability=data.get("language_probability"),
    )


def _finish_resumed_source(
    job: Job,
    cues: list[Cue],
    media_info: MediaInfo,
    event_sink: EventSink | None,
) -> Job:
    artifacts_directory = get_job_directory(job) / "artifacts"
    cues_path = artifacts_directory / "source-cues.json"
    _write_json(cues_path, [asdict(cue) for cue in cues])
    _add_artifact(job, "source_cues", cues_path, event_sink)

    _set_stage(job, StageName.SUBTITLE_QA, StageStatus.RUNNING, event_sink)
    report = check_subtitles(
        cues,
        media_duration_ms=media_info.duration_ms,
        video_width=media_info.width,
        video_height=media_info.height,
    )
    qa_path = artifacts_directory / "qa.json"
    _write_json(qa_path, asdict(report))
    _add_artifact(job, "subtitle_qa", qa_path, event_sink)
    _complete_qa_stage(job, report, event_sink)

    _set_stage(job, StageName.RENDER, StageStatus.RUNNING, event_sink)
    subtitle_path = save_subtitles(cues, artifacts_directory / "source.srt")
    _add_artifact(job, "source_srt", subtitle_path, event_sink)
    _set_stage(job, StageName.RENDER, StageStatus.SUCCEEDED, event_sink)
    if event_sink:
        event_sink(
            create_event(EventType.JOB_RESUMED, **job_event_fields(job.id, get_job_file(job)))
        )
    return job


def resume_job(job_file: str | Path, *, event_sink: EventSink | None = None) -> Job:
    job = load_job(job_file)
    if job.config.get("input_kind") == "subtitle":
        cues_path = valid_artifact_path(job, "source_cues")
        if cues_path:
            resumed = _finish_subtitle_job(job, _load_cues(cues_path), event_sink=event_sink)
            if event_sink:
                event_sink(
                    create_event(
                        EventType.JOB_RESUMED, **job_event_fields(job.id, get_job_file(job))
                    )
                )
            return resumed
        subtitle_path = valid_artifact_path(job, "input_subtitles")
        if subtitle_path is None:
            raise ValueError("subtitle job has no valid input_subtitles checkpoint")
        language = job.config.get("source_language")
        cues = load_subtitles(
            subtitle_path,
            role="source",
            source_language=None if language == "auto" else str(language),
        )
        if not cues:
            raise ValueError("subtitle file contains no usable cues")
        resumed = _finish_subtitle_job(job, cues, event_sink=event_sink)
        if event_sink:
            event_sink(
                create_event(EventType.JOB_RESUMED, **job_event_fields(job.id, get_job_file(job)))
            )
        return resumed

    media_path = valid_artifact_path(job, "media_info")
    if media_path is None:
        raise ValueError("job has no valid media_info checkpoint; run the job again")
    media_info = MediaInfo(**json.loads(media_path.read_text(encoding="utf-8")))

    cues_path = valid_artifact_path(job, "source_cues")
    if cues_path:
        return _finish_resumed_source(job, _load_cues(cues_path), media_info, event_sink)

    transcript_path = valid_artifact_path(job, "word_transcript")
    if transcript_path:
        transcript = _load_transcript(transcript_path)
    else:
        audio_path = valid_artifact_path(job, "normalized_audio")
        if audio_path is None:
            raise ValueError("job has no valid resumable checkpoint; run the job again")
        _set_stage(job, StageName.TRANSCRIBE, StageStatus.RUNNING, event_sink)
        language = job.config.get("source_language")
        transcript = transcribe_audio(
            audio_path,
            engine=job.config.get("engine", DEFAULT_ENGINE),
            language=None if language == "auto" else language,
            event_sink=event_sink,
            total_ms=media_info.duration_ms,
        )
        transcript_path = get_job_directory(job) / "artifacts" / "words.json"
        _write_json(transcript_path, asdict(transcript))
        _add_artifact(job, "word_transcript", transcript_path, event_sink)
        _set_stage(job, StageName.TRANSCRIBE, StageStatus.SUCCEEDED, event_sink)

    if job.config.get("align_subtitles"):
        subtitle_path = valid_artifact_path(job, "input_subtitles")
        if subtitle_path is None:
            raise ValueError("job has no valid input_subtitles checkpoint")
        imported_cues = load_subtitles(
            subtitle_path,
            role="source",
            source_language=job.config.get("source_language"),
        )
        _set_stage(job, StageName.ALIGN, StageStatus.RUNNING, event_sink)
        alignment = align_source_cues(imported_cues, transcript)
        alignment_path = get_job_directory(job) / "artifacts" / "alignment.json"
        _write_json(alignment_path, asdict(alignment))
        _add_artifact(job, "subtitle_alignment", alignment_path, event_sink)
        minimum_confidence = float(
            job.config.get("minimum_alignment_confidence", DEFAULT_MIN_ALIGNMENT_CONFIDENCE)
        )
        if (
            not job.config.get("allow_low_confidence_alignment", False)
            and alignment.confidence < minimum_confidence
        ):
            _set_stage(
                job,
                StageName.ALIGN,
                StageStatus.FAILED,
                event_sink,
                error="subtitle alignment confidence is below the required threshold",
            )
            raise ValueError("subtitle alignment confidence is below the required threshold")
        _set_stage(job, StageName.ALIGN, StageStatus.SUCCEEDED, event_sink)
        cues = list(alignment.cues)
    else:
        _set_stage(job, StageName.SEGMENT, StageStatus.RUNNING, event_sink)
        try:
            cues = segment_transcript(
                transcript,
                segmentation_config_for_preset(
                    str(job.config.get("segmentation_preset", DEFAULT_SEGMENTATION_PRESET))
                ),
                media_duration_ms=media_info.duration_ms,
            )
        except ValueError as error:
            _set_stage(
                job,
                StageName.SEGMENT,
                StageStatus.FAILED,
                event_sink,
                error=str(error),
                error_category=_error_category(error),
            )
            raise
        if not cues:
            _set_stage(
                job,
                StageName.SEGMENT,
                StageStatus.FAILED,
                event_sink,
                error="transcription produced no subtitle cues",
            )
            raise RuntimeError("transcription produced no subtitle cues")
        _set_stage(job, StageName.SEGMENT, StageStatus.SUCCEEDED, event_sink)
    return _finish_resumed_source(job, cues, media_info, event_sink)

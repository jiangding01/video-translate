from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import asdict, dataclass
from math import ceil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from xvt.align import align_source_cues
from xvt.burn import burn_subtitles, recommended_video_bitrate
from xvt.download import (
    RemoteMediaInfo,
    download_remote_media,
    download_remote_subtitles,
    inspect_remote,
    select_subtitle_language,
)
from xvt.events import EventSink, create_event
from xvt.job_store import create_job, get_job_directory, get_job_file, load_job, save_job, utc_now
from xvt.media import MediaInfo, probe_media
from xvt.models import Artifact, Cue, Job, StageName, StageStatus, Transcript, Word
from xvt.process import run_command
from xvt.qa import (
    check_subtitles,
    display_width,
    display_width_review_limit,
    target_overflow_cue_ids,
)
from xvt.recovery import valid_artifact_path
from xvt.render import AssStyleOptions, save_bilingual_ass
from xvt.segment import segment_transcript, split_cue_by_parts
from xvt.subtitles import load_subtitles, normalize_display_text, save_subtitles
from xvt.transcribe import transcribe_audio
from xvt.translate import (
    TranslationPolicy,
    apply_translations,
    build_translation_batches,
    load_translation_policy,
    map_subtitle_translations,
)
from xvt.translation_provider import (
    TRANSLATION_PROMPT_VERSION,
    TranslationProviderError,
    resolve_provider,
    translate_batch,
)


@dataclass(frozen=True, slots=True)
class TranslationRunResult:
    mode: str
    paths: tuple[Path, ...]
    fallback_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ResegmentationResult:
    changed: bool
    source_cues_path: Path | None = None
    report_path: Path | None = None


def inspect_input(input_value: str) -> dict[str, Any]:
    parsed = urlparse(input_value)
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        return {"kind": "remote", **asdict(inspect_remote(input_value))}
    return {"kind": "local", **asdict(probe_media(input_value))}


def _media_info_from_remote_metadata(url: str, duration_ms: int) -> MediaInfo:
    return MediaInfo(
        path=url,
        duration_ms=duration_ms,
        size_bytes=0,
        format_name="remote",
        width=None,
        height=None,
        frame_rate=None,
        video_codec=None,
        audio_codec=None,
        has_video=False,
        has_audio=False,
    )


def _set_stage(
    job: Job,
    stage: StageName,
    status: StageStatus,
    event_sink: EventSink | None,
    *,
    error: str | None = None,
) -> None:
    record = job.stages[stage]
    now = utc_now()
    record.status = status
    record.error = error
    if status == StageStatus.RUNNING:
        record.started_at = now
        record.completed_at = None
        event_type = "stage_started"
    elif status == StageStatus.AWAITING_INPUT:
        event_type = "stage_waiting"
    else:
        record.completed_at = now
        event_type = "stage_failed" if status == StageStatus.FAILED else "stage_completed"
    save_job(job)
    if event_sink:
        event_sink(create_event(event_type, job_id=job.id, stage=stage.value, status=status.value))


def interrupt_running_stage(
    job_file: str | Path,
    stage: str,
    *,
    event_sink: EventSink | None = None,
) -> bool:
    try:
        stage_name = StageName(stage)
        job = load_job(job_file)
        if job.stages[stage_name].status is not StageStatus.RUNNING:
            return False
        _set_stage(
            job,
            stage_name,
            StageStatus.FAILED,
            event_sink,
            error="interrupted by user",
        )
    except (KeyError, OSError, RuntimeError, ValueError):
        return False
    return True


def _write_json(path: Path, data: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _add_artifact(job: Job, kind: str, path: Path, event_sink: EventSink | None) -> None:
    artifact = Artifact(kind=kind, path=str(path), checksum=_checksum(path))
    job.artifacts = [item for item in job.artifacts if item.kind != kind]
    job.artifacts.append(artifact)
    save_job(job)
    if event_sink:
        event_sink(create_event("artifact_created", job_id=job.id, kind=kind, path=str(path)))


def _complete_qa_stage(
    job: Job,
    report: Any,
    event_sink: EventSink | None,
) -> None:
    if report.status in {"failed", "needs_review"}:
        message = f"subtitle QA {report.status}; inspect the QA artifact"
        _set_stage(
            job,
            StageName.SUBTITLE_QA,
            StageStatus.FAILED,
            event_sink,
            error=message,
        )
        raise RuntimeError(message)
    _set_stage(job, StageName.SUBTITLE_QA, StageStatus.SUCCEEDED, event_sink)


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
        else:
            _set_stage(job, current_stage, StageStatus.SKIPPED, event_sink)

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
        else:
            _set_stage(job, StageName.NORMALIZE_AUDIO, StageStatus.SKIPPED, event_sink)
            _set_stage(job, StageName.TRANSCRIBE, StageStatus.SKIPPED, event_sink)

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
            _set_stage(job, StageName.SEGMENT, StageStatus.SKIPPED, event_sink)
        elif imported_cues is not None:
            cues = imported_cues
            _set_stage(job, StageName.ALIGN, StageStatus.SKIPPED, event_sink)
            _set_stage(job, StageName.SEGMENT, StageStatus.SKIPPED, event_sink)
        else:
            _set_stage(job, StageName.ALIGN, StageStatus.SKIPPED, event_sink)
            current_stage = StageName.SEGMENT
            _set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
            cues = segment_transcript(transcript, media_duration_ms=media_info.duration_ms)
            if not cues:
                raise RuntimeError("transcription produced no subtitle cues")
            _set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)

        cues_path = artifacts_directory / "source-cues.json"
        _write_json(cues_path, [asdict(cue) for cue in cues])
        _add_artifact(job, "source_cues", cues_path, event_sink)
        _set_stage(job, StageName.TRANSLATE, StageStatus.SKIPPED, event_sink)

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
        _set_stage(job, StageName.BURN, StageStatus.SKIPPED, event_sink)
        _set_stage(job, StageName.MEDIA_QA, StageStatus.SKIPPED, event_sink)
    except Exception as error:
        _set_stage(job, current_stage, StageStatus.FAILED, event_sink, error=str(error))
        raise

    if event_sink and emit_job_completed:
        event_sink(
            create_event("job_completed", job_id=job.id, job_file=str(job_directory / "job.json"))
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
            **({"workflow": dict(workflow_config)} if workflow_config else {}),
        },
    )
    if event_sink:
        event_sink(
            create_event(
                "job_started",
                job_id=job.id,
                job_file=str(get_job_file(job)),
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
        _set_stage(job, current_stage, StageStatus.FAILED, event_sink, error=str(error))
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
                "job_started",
                job_id=job.id,
                job_file=str(get_job_file(job)),
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
        _set_stage(job, current_stage, StageStatus.FAILED, event_sink, error=str(error))
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
                        "remote_subtitles_selected",
                        job_id=job.id,
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
        _set_stage(job, current_stage, StageStatus.FAILED, event_sink, error=str(error))
        raise
    return _process_acquired_media(
        job,
        source,
        media_info,
        engine=str(job.config.get("engine", "mlx")),
        language=(
            None
            if job.config.get("source_language") == "auto"
            else str(job.config.get("source_language"))
        ),
        subtitles=subtitles,
        align_subtitles=bool(job.config.get("align_subtitles")),
        minimum_alignment_confidence=float(job.config.get("minimum_alignment_confidence", 0.55)),
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
    return run_local_transcription(
        input_value,
        output_root,
        engine=engine,
        language=language,
        subtitles=subtitles,
        align_subtitles=align_subtitles,
        minimum_alignment_confidence=minimum_alignment_confidence,
        allow_low_confidence_alignment=allow_low_confidence_alignment,
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
        event_sink(create_event("job_resumed", job_id=job.id, job_file=str(get_job_file(job))))
    return job


def resume_job(job_file: str | Path, *, event_sink: EventSink | None = None) -> Job:
    job = load_job(job_file)
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
            engine=job.config.get("engine", "mlx"),
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
        minimum_confidence = float(job.config.get("minimum_alignment_confidence", 0.55))
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
        cues = segment_transcript(transcript, media_duration_ms=media_info.duration_ms)
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


def _artifact_path(job: Job, kind: str) -> Path:
    artifact = next((item for item in job.artifacts if item.kind == kind), None)
    if artifact is None:
        raise ValueError(f"job has no {kind} artifact")
    return Path(artifact.path)


def _load_cues(path: Path) -> list[Cue]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("cue artifact must contain a JSON array")
    cues = []
    for item in data:
        words = [Word(**word) for word in item.get("words", [])]
        cues.append(Cue(**{**item, "words": words}))
    return cues


def resegment_translation(
    job_file: str | Path,
    *,
    event_sink: EventSink | None = None,
) -> ResegmentationResult:
    job = load_job(job_file)
    workflow = job.config.get("workflow")
    if isinstance(workflow, dict) and workflow.get("translation_subtitles"):
        raise ValueError(
            "cannot re-segment a workflow using an imported translation subtitle file; "
            "provide a new translation for the re-segmented source cues"
        )

    source_path = valid_artifact_path(job, "source_cues")
    translated_path = valid_artifact_path(job, "translated_cues")
    media_path = valid_artifact_path(job, "media_info")
    if source_path is None or translated_path is None or media_path is None:
        raise ValueError(
            "re-segmentation requires valid source_cues, translated_cues, and media_info"
        )

    source_cues = _load_cues(source_path)
    translated_cues = _load_cues(translated_path)
    source_ids = {cue.id for cue in source_cues}
    if len(source_ids) != len(source_cues):
        raise ValueError("source_cues contains duplicate cue ids")
    translated_by_id = {cue.id: cue for cue in translated_cues}
    if len(translated_by_id) != len(translated_cues):
        raise ValueError("translated_cues contains duplicate cue ids")
    media_data = json.loads(media_path.read_text(encoding="utf-8"))
    overflow_ids = set(
        target_overflow_cue_ids(
            translated_cues,
            video_width=media_data.get("width"),
            video_height=media_data.get("height"),
        )
    )
    if not overflow_ids:
        return ResegmentationResult(False)
    if missing_source_ids := overflow_ids - source_ids:
        raise ValueError(
            f"translated_cues has no matching source cue ids: {sorted(missing_source_ids)}"
        )

    resegmented: list[Cue] = []
    report_items = []
    for source_cue in source_cues:
        if source_cue.id not in overflow_ids:
            resegmented.append(source_cue)
            continue
        translated_cue = translated_by_id.get(source_cue.id)
        if translated_cue is None or not translated_cue.target_text:
            raise ValueError(f"cue {source_cue.id} has no translated text to re-segment")
        if source_cue.provenance not in {"asr", "asr_resegmented"} or not source_cue.words:
            raise ValueError(
                f"cue {source_cue.id} has no ASR word timestamps; "
                "imported subtitles are not re-segmented"
            )
        target_language = translated_cue.target_language
        hard_limit = display_width_review_limit(
            target_language,
            video_width=media_data.get("width"),
            video_height=media_data.get("height"),
        )
        target_width = display_width(normalize_display_text(translated_cue.target_text))
        part_count = ceil(target_width / hard_limit)
        split_cues = split_cue_by_parts(source_cue, part_count)
        report_items.append(
            {
                "source_cue_id": source_cue.id,
                "new_cue_count": len(split_cues),
                "target_width": target_width,
                "hard_display_limit": hard_limit,
            }
        )
        resegmented.extend(split_cues)

    for cue_id, cue in enumerate(resegmented, 1):
        cue.id = cue_id

    artifacts_directory = get_job_directory(job) / "artifacts"
    source_output = artifacts_directory / "source-cues.json"
    _write_json(source_output, [asdict(cue) for cue in resegmented])
    source_srt_output = save_subtitles(resegmented, artifacts_directory / "source.srt")
    report_output = artifacts_directory / "translation-resegmentation.json"
    _write_json(
        report_output,
        {
            "source_cue_count": len(source_cues),
            "resegmented_cue_count": len(resegmented),
            "items": report_items,
        },
    )
    invalidated_kinds = {
        "input_translation_subtitles",
        "translation_mapping",
        "translation_batches",
        "translation_progress",
        "translation_response",
        "translated_cues",
        "translated_subtitle_qa",
        "translated_srt",
        "bilingual_srt",
        "bilingual_ass",
        "burned_video",
        "media_qa",
    }
    replacement_artifacts = [
        Artifact("source_cues", str(source_output), _checksum(source_output)),
        Artifact("source_srt", str(source_srt_output), _checksum(source_srt_output)),
        Artifact("translation_resegmentation", str(report_output), _checksum(report_output)),
    ]
    replacement_kinds = {artifact.kind for artifact in replacement_artifacts}
    invalidated = [artifact for artifact in job.artifacts if artifact.kind in invalidated_kinds]
    job.artifacts = [
        artifact
        for artifact in job.artifacts
        if artifact.kind not in invalidated_kinds | replacement_kinds
    ]
    job.artifacts.extend(replacement_artifacts)
    if isinstance(workflow, dict):
        workflow.pop("burn_fingerprint", None)
    for stage in (
        StageName.TRANSLATE,
        StageName.SUBTITLE_QA,
        StageName.RENDER,
        StageName.BURN,
        StageName.MEDIA_QA,
    ):
        record = job.stages[stage]
        record.status = StageStatus.PENDING
        record.started_at = None
        record.completed_at = None
        record.error = None
    save_job(job)
    if event_sink:
        for artifact in replacement_artifacts:
            event_sink(
                create_event(
                    "artifact_created",
                    job_id=job.id,
                    kind=artifact.kind,
                    path=artifact.path,
                )
            )
        for artifact in invalidated:
            event_sink(
                create_event(
                    "artifact_invalidated",
                    job_id=job.id,
                    kind=artifact.kind,
                    path=artifact.path,
                )
            )
        event_sink(
            create_event(
                "translation_resegmented",
                job_id=job.id,
                source_cue_count=len(source_cues),
                resegmented_cue_count=len(resegmented),
                report_path=str(report_output),
            )
        )
    return ResegmentationResult(True, source_output, report_output)


def _render_translated_outputs(
    job: Job,
    translated: list[Cue],
    media_data: dict[str, Any],
    event_sink: EventSink | None,
) -> tuple[Path, ...]:
    artifacts_directory = get_job_directory(job) / "artifacts"
    translated_path = save_subtitles(
        translated,
        artifacts_directory / "translated.srt",
        mode="translated",
    )
    bilingual_path = save_subtitles(
        translated,
        artifacts_directory / "bilingual.srt",
        mode="bilingual",
    )
    _add_artifact(job, "translated_srt", translated_path, event_sink)
    _add_artifact(job, "bilingual_srt", bilingual_path, event_sink)
    paths = [translated_path, bilingual_path]

    width = int(media_data.get("width") or 0)
    height = int(media_data.get("height") or 0)
    if width > 0 and height > 0:
        ass_path = save_bilingual_ass(
            translated,
            artifacts_directory / "bilingual.ass",
            video_width=width,
            video_height=height,
        )
        _add_artifact(job, "bilingual_ass", ass_path, event_sink)
        paths.append(ass_path)
    return tuple(paths)


def prepare_agent_translation(
    job_file: str | Path,
    *,
    source_language: str,
    target_language: str,
    batch_size: int = 40,
    policy: TranslationPolicy | None = None,
    glossary: str | Path | None = None,
    translation_style: str = "faithful",
    event_sink: EventSink | None = None,
) -> Path:
    job = load_job(job_file)
    resolved_policy = policy or load_translation_policy(glossary, style=translation_style)
    _set_stage(job, StageName.TRANSLATE, StageStatus.RUNNING, event_sink)
    try:
        cues = _load_cues(_artifact_path(job, "source_cues"))
        batches = build_translation_batches(
            cues,
            source_language=source_language,
            target_language=target_language,
            batch_size=batch_size,
            policy=resolved_policy,
        )
        output_path = get_job_directory(job) / "artifacts" / "translation-batches.json"
        _write_json(
            output_path,
            {
                "job_id": job.id,
                "source_language": source_language,
                "target_language": target_language,
                "policy": resolved_policy.to_dict(),
                "batches": [batch.to_dict() for batch in batches],
            },
        )
        _add_artifact(job, "translation_batches", output_path, event_sink)
        job.config["target_language"] = target_language
        job.config["translation_policy"] = resolved_policy.to_dict()
        _set_stage(job, StageName.TRANSLATE, StageStatus.AWAITING_INPUT, event_sink)
        return output_path
    except Exception as error:
        _set_stage(job, StageName.TRANSLATE, StageStatus.FAILED, event_sink, error=str(error))
        raise


def run_translation(
    job_file: str | Path,
    *,
    target_language: str,
    provider: str = "auto",
    base_url: str | None = None,
    model: str | None = None,
    api_key_env: str = "XVT_TRANSLATION_API_KEY",
    batch_size: int = 40,
    max_retries: int = 2,
    timeout_seconds: float = 60,
    policy: TranslationPolicy | None = None,
    glossary: str | Path | None = None,
    translation_style: str = "faithful",
    event_sink: EventSink | None = None,
) -> TranslationRunResult:
    if max_retries < 0:
        raise ValueError("max_retries must not be negative")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    job = load_job(job_file)
    resolved_policy = policy or load_translation_policy(glossary, style=translation_style)
    cues = _load_cues(_artifact_path(job, "source_cues"))
    source_language = next(
        (cue.source_language for cue in cues if cue.source_language),
        job.config.get("source_language", "auto"),
    )
    mode, provider_config, fallback_reason = resolve_provider(
        provider,
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        timeout_seconds=timeout_seconds,
    )
    batches_path = prepare_agent_translation(
        job_file,
        source_language=source_language,
        target_language=target_language,
        batch_size=batch_size,
        policy=resolved_policy,
        event_sink=event_sink,
    )
    if mode == "agent":
        if fallback_reason and event_sink:
            event_sink(
                create_event(
                    "translation_fallback",
                    job_id=job.id,
                    mode="agent",
                    reason=fallback_reason,
                    path=str(batches_path),
                )
            )
        return TranslationRunResult("agent", (batches_path,), fallback_reason)

    if provider_config is None:
        raise RuntimeError("translation provider configuration was not resolved")
    job = load_job(job_file)
    batches = build_translation_batches(
        cues,
        source_language=source_language,
        target_language=target_language,
        batch_size=batch_size,
        policy=resolved_policy,
    )
    source_cues_checksum = _checksum(_artifact_path(job, "source_cues"))
    fingerprint_payload = {
        "source_cues_checksum": source_cues_checksum,
        "source_language": source_language,
        "target_language": target_language,
        "provider": mode,
        "base_url": provider_config.base_url,
        "model": provider_config.model,
        "batch_size": batch_size,
        "prompt_version": TRANSLATION_PROMPT_VERSION,
        "translation_policy": resolved_policy.to_dict(),
    }
    translation_fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    _set_stage(job, StageName.TRANSLATE, StageStatus.RUNNING, event_sink)
    if event_sink:
        event_sink(
            create_event(
                "translation_provider_selected",
                job_id=job.id,
                provider=mode,
                model=provider_config.model,
            )
        )

    progress_path = valid_artifact_path(job, "translation_progress")
    translations: dict[int, str] = {}
    completed_batches: set[int] = set()
    if progress_path:
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        if progress.get("fingerprint") == translation_fingerprint:
            completed_batches = {
                int(batch_id) for batch_id in progress.get("completed_batches", [])
            }
            translations = {
                int(item["id"]): str(item["text"])
                for item in progress.get("items", [])
                if isinstance(item, dict) and isinstance(item.get("id"), int)
            }
        elif event_sink:
            event_sink(
                create_event(
                    "translation_progress_reset",
                    job_id=job.id,
                    reason="translation fingerprint changed",
                )
            )

    artifacts_directory = get_job_directory(job) / "artifacts"
    progress_output = artifacts_directory / "translation-progress.json"
    try:
        for batch in batches:
            batch_ids = {item.id for item in batch.items}
            if batch.id in completed_batches and batch_ids <= translations.keys():
                continue
            if event_sink:
                event_sink(create_event("translation_batch_started", job_id=job.id, batch=batch.id))
            for attempt in range(max_retries + 1):
                try:
                    if attempt == 0:
                        translated_batch = translate_batch(batch, provider_config)
                    else:
                        translated_batch = translate_batch(
                            batch,
                            provider_config,
                            correction=(
                                "The previous response was invalid. Return every requested id "
                                "exactly once with non-empty translated text."
                            ),
                        )
                    break
                except TranslationProviderError as error:
                    if not error.retryable or attempt >= max_retries:
                        raise
                    delay = (
                        error.retry_after if error.retry_after is not None else min(2**attempt, 30)
                    )
                    if event_sink:
                        event_sink(
                            create_event(
                                "translation_batch_retrying",
                                job_id=job.id,
                                batch=batch.id,
                                attempt=attempt + 2,
                                delay_seconds=delay,
                                reason="provider temporarily unavailable",
                            )
                        )
                    time.sleep(delay)
                except ValueError:
                    if attempt >= max_retries:
                        raise
                    delay = min(0.5 * (2**attempt), 4)
                    if event_sink:
                        event_sink(
                            create_event(
                                "translation_batch_retrying",
                                job_id=job.id,
                                batch=batch.id,
                                attempt=attempt + 2,
                                delay_seconds=delay,
                                reason="invalid provider response",
                            )
                        )
                    time.sleep(delay)
            translations.update(translated_batch)
            completed_batches.add(batch.id)
            _write_json(
                progress_output,
                {
                    "fingerprint": translation_fingerprint,
                    "completed_batches": sorted(completed_batches),
                    "items": [
                        {"id": cue_id, "text": text}
                        for cue_id, text in sorted(translations.items())
                    ],
                },
            )
            _add_artifact(job, "translation_progress", progress_output, event_sink)
            if event_sink:
                event_sink(
                    create_event("translation_batch_completed", job_id=job.id, batch=batch.id)
                )
    except Exception as error:
        _set_stage(job, StageName.TRANSLATE, StageStatus.FAILED, event_sink, error=str(error))
        raise

    response_path = artifacts_directory / "translation-response.json"
    _write_json(
        response_path,
        {"items": [{"id": cue_id, "text": text} for cue_id, text in sorted(translations.items())]},
    )
    _add_artifact(job, "translation_response", response_path, event_sink)
    rendered = apply_agent_translation(
        job_file,
        response_path,
        target_language=target_language,
        event_sink=event_sink,
    )
    return TranslationRunResult(mode, (response_path, *rendered))


def apply_agent_translation(
    job_file: str | Path,
    response_file: str | Path,
    *,
    target_language: str,
    event_sink: EventSink | None = None,
) -> tuple[Path, ...]:
    job = load_job(job_file)
    current_stage = StageName.TRANSLATE
    _set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
    try:
        cues = _load_cues(_artifact_path(job, "source_cues"))
        response = json.loads(Path(response_file).read_text(encoding="utf-8"))
        items = response.get("items") if isinstance(response, dict) else None
        if not isinstance(items, list):
            raise ValueError("translation response must contain an items array")
        translations = {}
        for item in items:
            cue_id = item.get("id") if isinstance(item, dict) else None
            text = item.get("text") if isinstance(item, dict) else None
            if not isinstance(cue_id, int) or cue_id in translations:
                raise ValueError("translation response contains an invalid or duplicate cue id")
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"translation for cue {cue_id} is empty")
            translations[cue_id] = text.strip()
        translated = apply_translations(cues, translations, target_language=target_language)

        artifacts_directory = get_job_directory(job) / "artifacts"
        translated_cues_path = artifacts_directory / "translated-cues.json"
        _write_json(translated_cues_path, [asdict(cue) for cue in translated])
        _add_artifact(job, "translated_cues", translated_cues_path, event_sink)
        _set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)

        current_stage = StageName.SUBTITLE_QA
        _set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
        media_data = json.loads(_artifact_path(job, "media_info").read_text(encoding="utf-8"))
        report = check_subtitles(
            translated,
            media_duration_ms=int(media_data["duration_ms"]),
            video_width=media_data.get("width"),
            video_height=media_data.get("height"),
            require_translation=True,
        )
        qa_path = artifacts_directory / "qa-translated.json"
        _write_json(qa_path, asdict(report))
        _add_artifact(job, "translated_subtitle_qa", qa_path, event_sink)
        _complete_qa_stage(job, report, event_sink)

        current_stage = StageName.RENDER
        _set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
        paths = _render_translated_outputs(job, translated, media_data, event_sink)
        _set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)
        return paths
    except Exception as error:
        _set_stage(job, current_stage, StageStatus.FAILED, event_sink, error=str(error))
        raise


def apply_subtitle_translation(
    job_file: str | Path,
    subtitle_file: str | Path,
    *,
    target_language: str,
    event_sink: EventSink | None = None,
) -> tuple[Path, ...]:
    job = load_job(job_file)
    current_stage = StageName.TRANSLATE
    _set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
    try:
        source_cues = _load_cues(_artifact_path(job, "source_cues"))
        subtitle_path = Path(subtitle_file).expanduser()
        translated_cues = load_subtitles(
            subtitle_path,
            role="translated",
            target_language=target_language,
        )
        translations, mapping_report = map_subtitle_translations(source_cues, translated_cues)
        translated = apply_translations(
            source_cues,
            translations,
            target_language=target_language,
        )
        artifacts_directory = get_job_directory(job) / "artifacts"
        imported_path = artifacts_directory / f"input-translation{subtitle_path.suffix.lower()}"
        shutil.copy2(subtitle_path, imported_path)
        _add_artifact(job, "input_translation_subtitles", imported_path, event_sink)
        mapping_path = artifacts_directory / "translation-mapping.json"
        _write_json(mapping_path, mapping_report)
        _add_artifact(job, "translation_mapping", mapping_path, event_sink)

        translated_cues_path = artifacts_directory / "translated-cues.json"
        _write_json(translated_cues_path, [asdict(cue) for cue in translated])
        _add_artifact(job, "translated_cues", translated_cues_path, event_sink)
        job.config["target_language"] = target_language
        _set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)

        current_stage = StageName.SUBTITLE_QA
        _set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
        media_data = json.loads(_artifact_path(job, "media_info").read_text(encoding="utf-8"))
        report = check_subtitles(
            translated,
            media_duration_ms=int(media_data["duration_ms"]),
            video_width=media_data.get("width"),
            video_height=media_data.get("height"),
            require_translation=True,
        )
        qa_path = artifacts_directory / "qa-translated.json"
        _write_json(qa_path, asdict(report))
        _add_artifact(job, "translated_subtitle_qa", qa_path, event_sink)
        _complete_qa_stage(job, report, event_sink)

        current_stage = StageName.RENDER
        _set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
        paths = _render_translated_outputs(job, translated, media_data, event_sink)
        _set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)
        return paths
    except Exception as error:
        _set_stage(job, current_stage, StageStatus.FAILED, event_sink, error=str(error))
        raise


def burn_translated_job(
    job_file: str | Path,
    *,
    media: str | Path | None = None,
    output: str | Path | None = None,
    hardware_encoding: bool = True,
    cookies_from_browser: str | None = None,
    proxy: str | None = None,
    target_font: str | None = None,
    source_font: str | None = None,
    event_sink: EventSink | None = None,
) -> Path:
    job = load_job(job_file)
    current_stage = StageName.RENDER
    try:
        if media:
            media_path = Path(media).expanduser()
        else:
            parsed = urlparse(job.input)
            if parsed.scheme in {"http", "https"} and parsed.hostname:
                media_path = valid_artifact_path(job, "source_video")
                if media_path is None:
                    media_path = download_remote_media(
                        job.input,
                        get_job_directory(job) / "input" / "video",
                        cookies_from_browser=cookies_from_browser,
                        proxy=proxy,
                        event_sink=event_sink,
                    )
                    _add_artifact(job, "source_video", media_path, event_sink)
            else:
                media_path = Path(job.input).expanduser()
        if not media_path.is_file():
            raise ValueError(f"video file does not exist: {media_path}")
        media_path = media_path.resolve()
        media_info = probe_media(media_path)
        if not media_info.has_video or not media_info.width or not media_info.height:
            raise ValueError("burn-in requires a video stream with known dimensions")

        translated = _load_cues(_artifact_path(job, "translated_cues"))
        _set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
        ass_path = save_bilingual_ass(
            translated,
            get_job_directory(job) / "artifacts" / "bilingual.ass",
            video_width=media_info.width,
            video_height=media_info.height,
            options=AssStyleOptions(target_font=target_font, source_font=source_font),
        )
        _add_artifact(job, "bilingual_ass", ass_path, event_sink)
        _set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)

        current_stage = StageName.BURN
        _set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
        output_path = (
            Path(output).expanduser()
            if output
            else get_job_directory(job) / "artifacts" / "burned.mp4"
        )
        burned_path = burn_subtitles(
            media_path,
            ass_path,
            output_path,
            hardware_encoding=hardware_encoding,
            video_bitrate=recommended_video_bitrate(media_info.height),
            duration_ms=media_info.duration_ms,
            event_sink=event_sink,
        )
        _add_artifact(job, "burned_video", burned_path, event_sink)
        _set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)

        current_stage = StageName.MEDIA_QA
        _set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
        burned_info = probe_media(burned_path)
        duration_delta_ms = abs(burned_info.duration_ms - media_info.duration_ms)
        media_report = {
            "status": "pass",
            "video_codec": burned_info.video_codec,
            "audio_codec": burned_info.audio_codec,
            "duration_delta_ms": duration_delta_ms,
            "issues": [],
        }
        if not burned_info.has_video:
            media_report["issues"].append("output has no video stream")
        if media_info.has_audio and not burned_info.has_audio:
            media_report["issues"].append("output has no audio stream")
        elif burned_info.has_audio and burned_info.audio_codec != "aac":
            media_report["issues"].append("output audio codec is not AAC")
        if (burned_info.width, burned_info.height) != (media_info.width, media_info.height):
            media_report["issues"].append("output dimensions differ from source video")
        if duration_delta_ms > 500:
            media_report["issues"].append("output duration differs by more than 500ms")
        if media_report["issues"]:
            media_report["status"] = "failed"
        report_path = get_job_directory(job) / "artifacts" / "media-qa.json"
        _write_json(report_path, media_report)
        _add_artifact(job, "media_qa", report_path, event_sink)
        if media_report["status"] == "failed":
            raise RuntimeError("burned video failed media QA")
        _set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)
        return burned_path
    except Exception as error:
        _set_stage(job, current_stage, StageStatus.FAILED, event_sink, error=str(error))
        raise

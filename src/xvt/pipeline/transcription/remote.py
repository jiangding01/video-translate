"""远程获取腿：远程元信息探测、字幕与媒体下载，以及下载后的转写。

``_acquire_remote_job`` 负责 ``inspect_remote`` → 字幕候选 → 媒体下载这段
可恢复的获取过程；获取完成后交给本地腿的 ``_process_adaptive_media``。
``_process_acquired_media`` 是**冻结的 legacy 路径**，只有 config 里缺
``source_strategy`` 的旧 Job 经 ``resume_remote_transcription`` 可达，刻意
不与现代路径同步（见 AGENTS.md 与 docs/REVIEW_FINDINGS_2026_10_17.md 的 O8）。
"""

from __future__ import annotations

import shutil
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from xvt.align import align_source_cues
from xvt.config import DEFAULT_ENGINE, DEFAULT_MIN_ALIGNMENT_CONFIDENCE
from xvt.download import (
    download_remote_media,
    download_remote_subtitles,
    inspect_remote,
    select_subtitle_language,
)
from xvt.errors import DependencyError
from xvt.events import EventSink, EventType, create_event, job_event_fields, redact_text
from xvt.job_store import create_job, get_job_directory, get_job_file, load_job
from xvt.media import MediaInfo, probe_media
from xvt.models import Job, StageName, StageStatus
from xvt.pipeline.common import (
    add_artifact,
    error_category,
    media_info_from_remote_metadata,
    record_media_download_selection,
    set_stage,
    write_json,
)
from xvt.pipeline.transcription.local import (
    _normalize_audio,
    _process_adaptive_media,
    _record_effective_engine,
)
from xvt.pipeline.transcription.shared import (
    AcquiredSubtitleCandidate,
    _load_remote_info,
    _record_candidate_qa,
    _render_source_srt,
    _run_source_subtitle_qa,
    _segmentation_config,
)
from xvt.recovery import valid_artifact_path
from xvt.segment import segment_transcript
from xvt.source_selection import (
    SourceCandidateEvaluation,
    SourceCandidateKind,
    SourceSelectionAction,
    SourceStrategy,
    evaluate_source_candidate,
    invalid_source_candidate,
    plan_source_selection,
)
from xvt.stages import TRAILING_TRANSCRIPTION_SKIPS, media_transcription_skips
from xvt.subtitles import load_subtitles
from xvt.transcribe import transcribe_audio
from xvt.workflow_config import get_workflow_config


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
    """冻结的 legacy 路径：仅由 resume_remote_transcription 在 config 缺 source_strategy 时到达。

    缺该键的只有 v0.1.x 创建的媒体 Job；现代 Job 的创建点都会写键并走
    _process_adaptive_media。本函数刻意保持 legacy 语义：对齐后没有 QA error 门、
    没有回落 ASR、不登记 source_selection / aligned_subtitle_qa；error 级 issue
    由下游 _run_source_subtitle_qa 的 fail-closed 兜底。这是裁定后的刻意状态。
    """
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
        set_stage(job, stage, StageStatus.SKIPPED, event_sink)
    current_stage = StageName.IMPORT_SUBTITLES
    try:
        imported_cues = None
        if subtitles:
            set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
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
                # 报错要指向用户传入的字幕文件，不是 Job 内部副本。
                display_path=subtitle_path,
            )
            if not imported_cues:
                raise ValueError("subtitle file contains no usable cues")
            add_artifact(job, "input_subtitles", imported_path, event_sink)
            set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)

        transcript = None
        if imported_cues is None or align_subtitles:
            if source is None:
                raise RuntimeError("audio acquisition is required for transcription or alignment")
            current_stage = StageName.NORMALIZE_AUDIO
            set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
            audio_path = artifacts_directory / "audio.wav"
            _normalize_audio(job, source, audio_path, event_sink)
            set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)

            current_stage = StageName.TRANSCRIBE
            set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
            transcript = transcribe_audio(
                audio_path,
                engine=engine,
                language=language,
                event_sink=event_sink,
                total_ms=media_info.duration_ms,
            )
            _record_effective_engine(job, engine, transcript)
            words_path = artifacts_directory / "words.json"
            write_json(words_path, asdict(transcript))
            add_artifact(job, "word_transcript", words_path, event_sink)
            set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)

        if imported_cues is not None and align_subtitles:
            current_stage = StageName.ALIGN
            set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
            alignment = align_source_cues(imported_cues, transcript)
            cues = list(alignment.cues)
            alignment_path = artifacts_directory / "alignment.json"
            write_json(alignment_path, asdict(alignment))
            add_artifact(job, "subtitle_alignment", alignment_path, event_sink)
            if (
                not allow_low_confidence_alignment
                and alignment.confidence < minimum_alignment_confidence
            ):
                raise ValueError(
                    "subtitle alignment confidence is below the required threshold "
                    f"({alignment.confidence:.3f} < {minimum_alignment_confidence:.3f})"
                )
            set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)
        elif imported_cues is not None:
            cues = imported_cues
        else:
            current_stage = StageName.SEGMENT
            set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
            cues = segment_transcript(
                transcript,
                _segmentation_config(job),
                media_duration_ms=media_info.duration_ms,
            )
            if not cues:
                raise RuntimeError("transcription produced no subtitle cues")
            set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)

        cues_path = artifacts_directory / "source-cues.json"
        write_json(cues_path, [asdict(cue) for cue in cues])
        add_artifact(job, "source_cues", cues_path, event_sink)

        current_stage = StageName.SUBTITLE_QA
        if not _run_source_subtitle_qa(
            job, cues, artifacts_directory, media_info=media_info, event_sink=event_sink
        ):
            return job

        current_stage = StageName.RENDER
        _render_source_srt(job, cues, artifacts_directory, event_sink)
    except Exception as error:
        if job.stages[current_stage].status is not StageStatus.FAILED:
            set_stage(
                job,
                current_stage,
                StageStatus.FAILED,
                event_sink,
                error=str(error),
                error_category=error_category(error),
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


def run_remote_transcription(
    url: str,
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
    job = create_job(
        url,
        output_root,
        {
            "engine": engine,
            "source_language": language or "auto",
            "subtitles": str(Path(subtitles).expanduser().resolve()) if subtitles else None,
            "source_strategy": source_strategy,
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
        source, media_info, candidate = _acquire_remote_job(
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
        set_stage(
            job,
            current_stage,
            StageStatus.FAILED,
            event_sink,
            error=str(error),
            error_category=error_category(error),
        )
        raise
    # 同 run_local_transcription：Job 由本函数创建，`source_strategy` 必然已写入 config，
    # 旧 Job 只会从 resume_remote_transcription 进入，那条路径保留键缺失的判定。
    return _process_adaptive_media(
        job,
        source,
        media_info,
        engine=engine,
        language=language,
        candidate=candidate,
        align_subtitles=align_subtitles,
        minimum_alignment_confidence=minimum_alignment_confidence,
        allow_low_confidence_alignment=allow_low_confidence_alignment,
        event_sink=event_sink,
        emit_job_completed=emit_job_completed,
    )


def _acquire_remote_job(
    job: Job,
    *,
    cookies_from_browser: str | None,
    proxy: str | None,
    event_sink: EventSink | None,
) -> tuple[Path | None, MediaInfo, AcquiredSubtitleCandidate | None]:
    url = job.input
    remote_path = valid_artifact_path(job, "remote_info")
    if remote_path is None:
        set_stage(job, StageName.INSPECT, StageStatus.RUNNING, event_sink)
        remote_info = inspect_remote(
            url,
            cookies_from_browser=cookies_from_browser,
            proxy=proxy,
        )
        remote_path = get_job_directory(job) / "artifacts" / "remote.json"
        remote_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(remote_path, asdict(remote_info))
        add_artifact(job, "remote_info", remote_path, event_sink)
    else:
        remote_info = _load_remote_info(remote_path)
    if (
        job.config.get("youtube_subtitles")
        and cookies_from_browser
        and not job.config.get("subtitles")
        and valid_artifact_path(job, "input_subtitles") is None
        and valid_artifact_path(job, "downloaded_subtitles") is None
        and urlparse(url).hostname
        in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
        and select_subtitle_language(
            str(job.config.get("subtitle_language") or ""), remote_info.subtitle_languages
        )
        is None
    ):
        set_stage(job, StageName.INSPECT, StageStatus.RUNNING, event_sink)
        try:
            anonymous_info = inspect_remote(url, proxy=proxy, anonymous=True)
        except Exception as error:
            raise DependencyError(
                "人工平台字幕缺失后的匿名补查失败；无法确认字幕是否不存在。"
                f" 请重试同一 Job。原因：{redact_text(str(error))}"
            ) from error
        if not remote_info.id or anonymous_info.id != remote_info.id:
            raise ValueError("匿名字幕补查返回了不同的视频，拒绝采用")
        if (
            select_subtitle_language(
                str(job.config.get("subtitle_language") or ""), anonymous_info.subtitle_languages
            )
            is not None
        ):
            remote_info = replace(
                remote_info,
                subtitle_languages=anonymous_info.subtitle_languages,
                subtitles_anonymous=True,
            )
            write_json(remote_path, asdict(remote_info))
            add_artifact(job, "remote_info", remote_path, event_sink)
    set_stage(job, StageName.INSPECT, StageStatus.SUCCEEDED, event_sink)

    candidate: AcquiredSubtitleCandidate | None = None
    selected_subtitles = valid_artifact_path(job, "input_subtitles") or job.config.get("subtitles")
    if selected_subtitles:
        candidate = AcquiredSubtitleCandidate(
            Path(selected_subtitles),
            SourceCandidateKind.USER,
        )
    downloaded_subtitles = valid_artifact_path(job, "downloaded_subtitles")
    acquired_subtitles = downloaded_subtitles is not None
    if downloaded_subtitles is not None:
        selected_subtitles = downloaded_subtitles
        manual_language = select_subtitle_language(
            str(job.config.get("subtitle_language") or ""),
            remote_info.subtitle_languages,
        )
        candidate = AcquiredSubtitleCandidate(
            downloaded_subtitles,
            (
                SourceCandidateKind.MANUAL
                if manual_language is not None
                else SourceCandidateKind.AUTOMATIC
            ),
        )
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
            set_stage(
                job,
                StageName.ACQUIRE,
                StageStatus.RUNNING,
                event_sink,
                extras={"detail": "subtitles"},
            )
            selected_subtitles = download_remote_subtitles(
                url,
                get_job_directory(job) / "input" / "subtitles",
                language=selected_language,
                automatic=manual_language is None,
                cookies_from_browser=cookies_from_browser,
                proxy=proxy,
                **({"anonymous": True} if remote_info.subtitles_anonymous else {}),
            )
            acquired_subtitles = True
            add_artifact(job, "downloaded_subtitles", selected_subtitles, event_sink)
            candidate = AcquiredSubtitleCandidate(
                Path(selected_subtitles),
                (
                    SourceCandidateKind.MANUAL
                    if manual_language is not None
                    else SourceCandidateKind.AUTOMATIC
                ),
            )
            if event_sink:
                event_sink(
                    create_event(
                        EventType.REMOTE_SUBTITLES_SELECTED,
                        **job_event_fields(job.id, get_job_file(job)),
                        language=selected_language,
                        automatic=manual_language is None,
                        anonymous=remote_info.subtitles_anonymous,
                    )
                )

    preflight_evaluation: SourceCandidateEvaluation | None = None
    if "source_strategy" in job.config and candidate:
        provisional_media_info = media_info_from_remote_metadata(url, remote_info.duration_ms)
        media_path = get_job_directory(job) / "artifacts" / "media.json"
        write_json(media_path, asdict(provisional_media_info))
        add_artifact(job, "media_info", media_path, event_sink)
        try:
            candidate_cues = load_subtitles(
                candidate.path,
                role="source",
                source_language=(
                    None
                    if job.config.get("source_language") == "auto"
                    else str(job.config.get("source_language"))
                ),
            )
        except Exception as error:
            preflight_evaluation = invalid_source_candidate(candidate.kind, str(error))
        else:
            preflight_evaluation = (
                evaluate_source_candidate(
                    candidate_cues,
                    kind=candidate.kind,
                    media_duration_ms=remote_info.duration_ms,
                )
                if candidate_cues
                else invalid_source_candidate(
                    candidate.kind,
                    "subtitle file contains no usable cues",
                )
            )
        _record_candidate_qa(job, preflight_evaluation, event_sink)

    workflow = get_workflow_config(job)
    download_for_burn = bool(workflow is not None and workflow.burn)
    if "source_strategy" not in job.config:
        needs_audio = not selected_subtitles or bool(job.config.get("align_subtitles"))
    else:
        strategy = SourceStrategy(str(job.config["source_strategy"]))
        has_translation_goal = bool(
            workflow is not None and (workflow.target_language is not None or workflow.burn)
        )
        candidate_has_errors = bool(preflight_evaluation and preflight_evaluation.has_errors)
        plan = plan_source_selection(
            strategy,
            candidate_kind=(candidate.kind if candidate else SourceCandidateKind.NONE),
            candidate_has_errors=candidate_has_errors,
            has_translation_goal=has_translation_goal,
        )
        needs_audio = bool(job.config.get("align_subtitles")) or plan.action in {
            SourceSelectionAction.ASR,
            SourceSelectionAction.ALIGN,
        }
    needs_audio = needs_audio or remote_info.duration_ms <= 0
    source = valid_artifact_path(job, "source_video" if download_for_burn else "downloaded_media")
    if source is None and download_for_burn:
        source = valid_artifact_path(job, "downloaded_media")
    if needs_audio or download_for_burn:
        set_stage(
            job,
            StageName.ACQUIRE,
            StageStatus.RUNNING,
            event_sink,
            extras={"detail": "media"},
        )
        if source is None:
            source = download_remote_media(
                url,
                get_job_directory(job) / "input",
                audio_only=not download_for_burn,
                cookies_from_browser=cookies_from_browser,
                proxy=proxy,
                event_sink=event_sink,
            )
            add_artifact(job, "downloaded_media", source, event_sink)
            record_media_download_selection(job, source, event_sink)
        media_info = probe_media(source)
        if download_for_burn and valid_artifact_path(job, "source_video") is None:
            add_artifact(job, "source_video", source, event_sink)
        set_stage(job, StageName.ACQUIRE, StageStatus.SUCCEEDED, event_sink)
    else:
        source = None
        media_info = media_info_from_remote_metadata(url, remote_info.duration_ms)
        status = StageStatus.SUCCEEDED if acquired_subtitles else StageStatus.SKIPPED
        set_stage(job, StageName.ACQUIRE, status, event_sink)
    media_path = get_job_directory(job) / "artifacts" / "media.json"
    write_json(media_path, asdict(media_info))
    add_artifact(job, "media_info", media_path, event_sink)
    return source, media_info, candidate


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
        source, media_info, candidate = _acquire_remote_job(
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
        set_stage(
            job,
            current_stage,
            StageStatus.FAILED,
            event_sink,
            error=str(error),
            error_category=error_category(error),
        )
        raise
    if "source_strategy" in job.config:
        return _process_adaptive_media(
            job,
            source,
            media_info,
            engine=str(job.config.get("engine", DEFAULT_ENGINE)),
            language=(
                None
                if job.config.get("source_language") == "auto"
                else str(job.config.get("source_language"))
            ),
            candidate=candidate,
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
        subtitles=candidate.path if candidate else None,
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

"""转写主链：素材获取、音频规范化、Whisper 转写、分段、对齐与转写恢复。

覆盖本地媒体、纯字幕输入与远程（YouTube）三条获取路径，以及 `resume`
使用的规范化音频 / 词级转写 / 源 cue checkpoint 恢复逻辑。
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
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
    _artifact_checksum_for,
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
from xvt.source_selection import (
    SourceCandidateEvaluation,
    SourceCandidateKind,
    SourceSelectionAction,
    SourceStrategy,
    evaluate_source_candidate,
    invalid_source_candidate,
    plan_source_selection,
)
from xvt.stages import (
    SUBTITLE_INPUT_SKIPS,
    TRAILING_TRANSCRIPTION_SKIPS,
    media_transcription_skips,
)
from xvt.subtitles import SUPPORTED_SUBTITLE_EXTENSIONS, load_subtitles, save_subtitles
from xvt.transcribe import transcribe_audio
from xvt.workflow_config import get_workflow_config


@dataclass(frozen=True, slots=True)
class AcquiredSubtitleCandidate:
    path: Path
    kind: SourceCandidateKind


def _candidate_from_job(job: Job) -> AcquiredSubtitleCandidate | None:
    downloaded_path = valid_artifact_path(job, "downloaded_subtitles")
    if downloaded_path is not None:
        remote_path = valid_artifact_path(job, "remote_info")
        if remote_path is None:
            return None
        remote_info = _load_remote_info(remote_path)
        manual_language = select_subtitle_language(
            str(job.config.get("subtitle_language") or ""),
            remote_info.subtitle_languages,
        )
        return AcquiredSubtitleCandidate(
            downloaded_path,
            (
                SourceCandidateKind.MANUAL
                if manual_language is not None
                else SourceCandidateKind.AUTOMATIC
            ),
        )

    subtitle_path = valid_artifact_path(job, "input_subtitles")
    if subtitle_path is None and job.config.get("subtitles"):
        subtitle_path = Path(str(job.config["subtitles"]))
    if subtitle_path is None:
        return None
    return AcquiredSubtitleCandidate(subtitle_path, SourceCandidateKind.USER)


def _record_candidate_qa(
    job: Job,
    evaluation: SourceCandidateEvaluation,
    event_sink: EventSink | None,
) -> Path:
    path = get_job_directory(job) / "artifacts" / "source-candidate-qa.json"
    _write_json(
        path,
        {
            "candidate_kind": evaluation.kind.value,
            "report": asdict(evaluation.report),
            "repairs": [asdict(repair) for repair in evaluation.repairs],
        },
    )
    _add_artifact(job, "source_candidate_qa", path, event_sink)
    return path


def _record_source_selection(
    job: Job,
    *,
    strategy: SourceStrategy,
    candidate_kind: SourceCandidateKind,
    selected_kind: str,
    reason: str,
    reference_allowed: bool,
    alignment_confidence: float | None,
    event_sink: EventSink | None,
) -> Path:
    source_checksum = _artifact_checksum_for(job, "source_cues")
    if source_checksum is None:
        raise RuntimeError("source selection requires a registered source_cues artifact")
    path = get_job_directory(job) / "artifacts" / "source-selection.json"
    _write_json(
        path,
        {
            "strategy": strategy.value,
            "candidate_kind": candidate_kind.value,
            "selected_kind": selected_kind,
            "reason": reason,
            "reference_allowed": reference_allowed,
            "alignment_confidence": alignment_confidence,
            "source_cues_checksum": source_checksum,
        },
    )
    _add_artifact(job, "source_selection", path, event_sink)
    if event_sink:
        event_sink(
            create_event(
                EventType.SOURCE_SELECTED,
                **job_event_fields(job.id, get_job_file(job)),
                strategy=strategy.value,
                candidate_kind=candidate_kind.value,
                selected_kind=selected_kind,
                reason=reason,
                report_path=str(path),
            )
        )
    return path


def _recover_source_selection(
    job: Job,
    cues: list[Cue],
    event_sink: EventSink | None,
) -> None:
    candidate = _candidate_from_job(job)
    candidate_kind = candidate.kind if candidate else SourceCandidateKind.NONE

    provenances = {cue.provenance for cue in cues}
    if "aligned" in provenances:
        selected_kind = "aligned_subtitles"
    elif provenances & {"asr", "asr_resegmented"}:
        selected_kind = "asr"
    else:
        selected_kind = "subtitles"

    reference_allowed = False
    candidate_qa_path = valid_artifact_path(job, "source_candidate_qa")
    if selected_kind == "asr" and candidate_qa_path is not None:
        candidate_qa = json.loads(candidate_qa_path.read_text(encoding="utf-8"))
        issues = candidate_qa.get("report", {}).get("issues", [])
        reference_allowed = not any(issue.get("severity") == "error" for issue in issues)

    alignment_confidence = None
    alignment_path = valid_artifact_path(job, "subtitle_alignment")
    if alignment_path is not None:
        alignment_data = json.loads(alignment_path.read_text(encoding="utf-8"))
        alignment_confidence = float(alignment_data["confidence"])

    _record_source_selection(
        job,
        strategy=SourceStrategy(str(job.config["source_strategy"])),
        candidate_kind=candidate_kind,
        selected_kind=selected_kind,
        reason="selection_artifact_recovered",
        reference_allowed=reference_allowed,
        alignment_confidence=alignment_confidence,
        event_sink=event_sink,
    )


def _source_selection_matches(job: Job) -> bool:
    path = valid_artifact_path(job, "source_selection")
    source_checksum = _artifact_checksum_for(job, "source_cues")
    if path is None or source_checksum is None:
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict) or data.get("source_cues_checksum") != source_checksum:
        return False
    return (
        data.get("candidate_kind") == SourceCandidateKind.NONE
        or job.config.get("input_kind") == "subtitle"
        or valid_artifact_path(job, "source_candidate_qa") is not None
    )


def _normalize_audio_and_transcribe(
    job: Job,
    source: Path | None,
    media_info: Any,
    *,
    engine: str,
    language: str | None,
    event_sink: EventSink | None,
) -> Transcript:
    if source is None:
        raise RuntimeError("audio acquisition is required for transcription or alignment")
    artifacts_directory = get_job_directory(job) / "artifacts"
    audio_path = valid_artifact_path(job, "normalized_audio")
    if audio_path is None:
        _set_stage(job, StageName.NORMALIZE_AUDIO, StageStatus.RUNNING, event_sink)
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
    _set_stage(job, StageName.NORMALIZE_AUDIO, StageStatus.SUCCEEDED, event_sink)

    transcript_path = valid_artifact_path(job, "word_transcript")
    if transcript_path is not None:
        transcript = _load_transcript(transcript_path)
    else:
        _set_stage(job, StageName.TRANSCRIBE, StageStatus.RUNNING, event_sink)
        transcript = transcribe_audio(
            audio_path,
            engine=engine,
            language=language,
            event_sink=event_sink,
            total_ms=media_info.duration_ms,
        )
        transcript_path = artifacts_directory / "words.json"
        _write_json(transcript_path, asdict(transcript))
        _add_artifact(job, "word_transcript", transcript_path, event_sink)
    _set_stage(job, StageName.TRANSCRIBE, StageStatus.SUCCEEDED, event_sink)
    return transcript


def _process_adaptive_media(
    job: Job,
    source: Path | None,
    media_info: Any,
    *,
    engine: str,
    language: str | None,
    candidate: AcquiredSubtitleCandidate | None,
    align_subtitles: bool,
    minimum_alignment_confidence: float,
    allow_low_confidence_alignment: bool,
    event_sink: EventSink | None,
    emit_job_completed: bool,
) -> Job:
    strategy = SourceStrategy(str(job.config["source_strategy"]))
    job_directory = get_job_directory(job)
    artifacts_directory = job_directory / "artifacts"
    artifacts_directory.mkdir(parents=True, exist_ok=True)
    for stage in TRAILING_TRANSCRIPTION_SKIPS:
        _set_stage(job, stage, StageStatus.SKIPPED, event_sink)

    current_stage = StageName.IMPORT_SUBTITLES
    candidate_kind = candidate.kind if candidate else SourceCandidateKind.NONE
    imported_cues: list[Cue] | None = None
    evaluation: SourceCandidateEvaluation | None = None
    try:
        if candidate is None:
            _set_stage(job, StageName.IMPORT_SUBTITLES, StageStatus.SKIPPED, event_sink)
        else:
            _set_stage(job, StageName.IMPORT_SUBTITLES, StageStatus.RUNNING, event_sink)
            if not candidate.path.is_file():
                raise ValueError(f"subtitle file does not exist: {candidate.path}")
            imported_path = artifacts_directory / f"input-subtitles{candidate.path.suffix.lower()}"
            if candidate.path.resolve() != imported_path.resolve():
                shutil.copy2(candidate.path, imported_path)
            _add_artifact(job, "input_subtitles", imported_path, event_sink)
            _set_stage(job, StageName.IMPORT_SUBTITLES, StageStatus.SUCCEEDED, event_sink)
            try:
                imported_cues = load_subtitles(
                    imported_path,
                    role="source",
                    source_language=language,
                )
            except Exception as error:
                evaluation = invalid_source_candidate(candidate_kind, str(error))
                imported_cues = None
            else:
                if imported_cues:
                    evaluation = evaluate_source_candidate(
                        imported_cues,
                        kind=candidate_kind,
                        media_duration_ms=media_info.duration_ms,
                        video_width=media_info.width,
                        video_height=media_info.height,
                    )
                    imported_cues = list(evaluation.cues)
                else:
                    evaluation = invalid_source_candidate(
                        candidate_kind,
                        "subtitle file contains no usable cues",
                    )
            _record_candidate_qa(job, evaluation, event_sink)
            if (
                candidate_kind is SourceCandidateKind.USER
                and strategy is not SourceStrategy.ASR
                and evaluation.has_errors
            ):
                raise ValueError(evaluation.report.issues[0].message)

        workflow = get_workflow_config(job)
        has_translation_goal = bool(
            workflow is not None and (workflow.target_language is not None or workflow.burn)
        )
        plan = plan_source_selection(
            strategy,
            candidate_kind=candidate_kind,
            candidate_has_errors=bool(evaluation and evaluation.has_errors),
            has_translation_goal=has_translation_goal,
        )
        if align_subtitles:
            plan = plan.__class__(SourceSelectionAction.ALIGN, "manual_subtitles_aligned", False)
        elif plan.action is SourceSelectionAction.ALIGN and imported_cues is None:
            plan = plan.__class__(SourceSelectionAction.ASR, "candidate_qa_failed", False)
        if plan.action is SourceSelectionAction.FAIL:
            raise ValueError("requested source subtitles are not available")
        if plan.action is SourceSelectionAction.SUBTITLES and imported_cues is None:
            raise ValueError("subtitle candidate contains no usable cues")

        transcript: Transcript | None = None
        if plan.action in {SourceSelectionAction.ASR, SourceSelectionAction.ALIGN}:
            transcript = _normalize_audio_and_transcribe(
                job,
                source,
                media_info,
                engine=engine,
                language=language,
                event_sink=event_sink,
            )
        else:
            for stage in (StageName.NORMALIZE_AUDIO, StageName.TRANSCRIBE):
                _set_stage(job, stage, StageStatus.SKIPPED, event_sink)

        alignment_confidence: float | None = None
        selected_kind = "subtitles"
        selection_reason = plan.reason
        reference_allowed = plan.reference_allowed
        if plan.action is SourceSelectionAction.ALIGN:
            if imported_cues is None or transcript is None:
                raise RuntimeError("subtitle alignment requires subtitle cues and a transcript")
            current_stage = StageName.ALIGN
            _set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
            alignment = align_source_cues(imported_cues, transcript)
            alignment_confidence = alignment.confidence
            alignment_path = artifacts_directory / "alignment.json"
            _write_json(alignment_path, asdict(alignment))
            _add_artifact(job, "subtitle_alignment", alignment_path, event_sink)
            aligned_cues = list(alignment.cues)
            aligned_report = check_subtitles(
                aligned_cues,
                media_duration_ms=media_info.duration_ms,
                video_width=media_info.width,
                video_height=media_info.height,
            )
            alignment_accepted = alignment.confidence >= minimum_alignment_confidence and not any(
                issue.severity == "error" for issue in aligned_report.issues
            )
            if align_subtitles:
                if (
                    not allow_low_confidence_alignment
                    and alignment.confidence < minimum_alignment_confidence
                ):
                    raise ValueError(
                        "subtitle alignment confidence is below the required threshold "
                        f"({alignment.confidence:.3f} < {minimum_alignment_confidence:.3f})"
                    )
                cues = aligned_cues
                selected_kind = "aligned_subtitles"
                selection_reason = "manual_subtitles_aligned"
            elif alignment_accepted:
                cues = aligned_cues
                selected_kind = "aligned_subtitles"
                selection_reason = "manual_subtitles_aligned"
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
                selected_kind = "asr"
                reference_allowed = False
                selection_reason = (
                    "alignment_below_threshold"
                    if alignment.confidence < minimum_alignment_confidence
                    else "alignment_post_qa_failed"
                )
            _set_stage(job, StageName.ALIGN, StageStatus.SUCCEEDED, event_sink)
            if selected_kind == "aligned_subtitles":
                _set_stage(job, StageName.SEGMENT, StageStatus.SKIPPED, event_sink)
        elif plan.action is SourceSelectionAction.ASR:
            _set_stage(job, StageName.ALIGN, StageStatus.SKIPPED, event_sink)
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
            selected_kind = "asr"
        else:
            if imported_cues is None:
                raise RuntimeError("subtitle selection requires imported subtitle cues")
            cues = imported_cues
            _set_stage(job, StageName.ALIGN, StageStatus.SKIPPED, event_sink)
            _set_stage(job, StageName.SEGMENT, StageStatus.SKIPPED, event_sink)

        cues_path = artifacts_directory / "source-cues.json"
        _write_json(cues_path, [asdict(cue) for cue in cues])
        _add_artifact(job, "source_cues", cues_path, event_sink)
        _record_source_selection(
            job,
            strategy=strategy,
            candidate_kind=candidate_kind,
            selected_kind=selected_kind,
            reason=selection_reason,
            reference_allowed=reference_allowed,
            alignment_confidence=alignment_confidence,
            event_sink=event_sink,
        )

        current_stage = StageName.SUBTITLE_QA
        _set_stage(
            job,
            current_stage,
            StageStatus.RUNNING,
            event_sink,
            extras={"detail": "source"},
        )
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
        _set_stage(
            job,
            current_stage,
            StageStatus.RUNNING,
            event_sink,
            extras={"detail": "source"},
        )
        subtitle_path = save_subtitles(cues, artifacts_directory / "source.srt")
        _add_artifact(job, "source_srt", subtitle_path, event_sink)
        _set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)
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

    if event_sink and emit_job_completed:
        event_sink(
            create_event(
                EventType.JOB_COMPLETED,
                **job_event_fields(job.id, job_directory / "job.json"),
            )
        )
    return job


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
        _set_stage(
            job,
            current_stage,
            StageStatus.RUNNING,
            event_sink,
            extras={"detail": "source"},
        )
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
        _set_stage(
            job,
            current_stage,
            StageStatus.RUNNING,
            event_sink,
            extras={"detail": "source"},
        )
        subtitle_path = save_subtitles(cues, artifacts_directory / "source.srt")
        _add_artifact(job, "source_srt", subtitle_path, event_sink)
        _set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)
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
    source_strategy: str = "auto",
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
            "source_strategy": source_strategy,
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
    if "source_strategy" in job.config:
        candidate = (
            AcquiredSubtitleCandidate(Path(subtitles).expanduser(), SourceCandidateKind.USER)
            if subtitles
            else None
        )
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
    record_source_selection: bool = False,
) -> Job:
    artifacts_directory = get_job_directory(job) / "artifacts"
    cues_path = artifacts_directory / "source-cues.json"
    _write_json(cues_path, [asdict(cue) for cue in cues])
    _add_artifact(job, "source_cues", cues_path, event_sink)
    if record_source_selection:
        _record_source_selection(
            job,
            strategy=SourceStrategy(str(job.config["source_strategy"])),
            candidate_kind=SourceCandidateKind.USER,
            selected_kind="subtitles",
            reason="user_subtitles_preserved",
            reference_allowed=False,
            alignment_confidence=None,
            event_sink=event_sink,
        )
    if job.stages[StageName.TRANSLATE].status is StageStatus.PENDING:
        _set_stage(job, StageName.TRANSLATE, StageStatus.SKIPPED, event_sink)

    current_stage = StageName.SUBTITLE_QA
    try:
        _set_stage(
            job,
            current_stage,
            StageStatus.RUNNING,
            event_sink,
            extras={"detail": "source"},
        )
        report = check_subtitles(cues)
        qa_path = artifacts_directory / "qa.json"
        _write_json(qa_path, asdict(report))
        _add_artifact(job, "subtitle_qa", qa_path, event_sink)
        _complete_qa_stage(job, report, event_sink)

        current_stage = StageName.RENDER
        _set_stage(
            job,
            current_stage,
            StageStatus.RUNNING,
            event_sink,
            extras={"detail": "source"},
        )
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
    source_strategy: str = "auto",
    workflow_config: dict[str, Any] | None = None,
    event_sink: EventSink | None = None,
    emit_job_completed: bool = True,
) -> Job:
    if source_strategy == SourceStrategy.ASR:
        raise ValueError("--source-strategy asr requires media input")
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
            "source_strategy": source_strategy,
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

    job = _finish_subtitle_job(
        job,
        cues,
        event_sink=event_sink,
        record_source_selection=True,
    )

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
        _set_stage(
            job,
            current_stage,
            StageStatus.FAILED,
            event_sink,
            error=str(error),
            error_category=_error_category(error),
        )
        raise
    if "source_strategy" in job.config:
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
    return _process_acquired_media(
        job,
        source,
        media_info,
        engine=engine,
        language=language,
        subtitles=candidate.path if candidate else None,
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
) -> tuple[Path | None, MediaInfo, AcquiredSubtitleCandidate | None]:
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
            _set_stage(
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
            )
            acquired_subtitles = True
            _add_artifact(job, "downloaded_subtitles", selected_subtitles, event_sink)
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
                    )
                )

    preflight_evaluation: SourceCandidateEvaluation | None = None
    if "source_strategy" in job.config and candidate:
        provisional_media_info = _media_info_from_remote_metadata(url, remote_info.duration_ms)
        media_path = get_job_directory(job) / "artifacts" / "media.json"
        _write_json(media_path, asdict(provisional_media_info))
        _add_artifact(job, "media_info", media_path, event_sink)
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
        _set_stage(
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
        _set_stage(
            job,
            current_stage,
            StageStatus.FAILED,
            event_sink,
            error=str(error),
            error_category=_error_category(error),
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
    if "source_strategy" in job.config and not _source_selection_matches(job):
        candidate = _candidate_from_job(job)
        if candidate is not None and valid_artifact_path(job, "source_candidate_qa") is None:
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
                evaluation = invalid_source_candidate(candidate.kind, str(error))
            else:
                evaluation = (
                    evaluate_source_candidate(
                        candidate_cues,
                        kind=candidate.kind,
                        media_duration_ms=media_info.duration_ms,
                        video_width=media_info.width,
                        video_height=media_info.height,
                    )
                    if candidate_cues
                    else invalid_source_candidate(
                        candidate.kind,
                        "subtitle file contains no usable cues",
                    )
                )
            _record_candidate_qa(job, evaluation, event_sink)
        _recover_source_selection(job, cues, event_sink)

    _set_stage(
        job,
        StageName.SUBTITLE_QA,
        StageStatus.RUNNING,
        event_sink,
        extras={"detail": "source"},
    )
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

    _set_stage(
        job,
        StageName.RENDER,
        StageStatus.RUNNING,
        event_sink,
        extras={"detail": "source"},
    )
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
            resumed = _finish_subtitle_job(
                job,
                _load_cues(cues_path),
                event_sink=event_sink,
                record_source_selection=(
                    "source_strategy" in job.config and not _source_selection_matches(job)
                ),
            )
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
        resumed = _finish_subtitle_job(
            job,
            cues,
            event_sink=event_sink,
            record_source_selection="source_strategy" in job.config,
        )
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

    if "source_strategy" in job.config:
        candidate = _candidate_from_job(job)
        return _process_adaptive_media(
            job,
            Path(job.input),
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

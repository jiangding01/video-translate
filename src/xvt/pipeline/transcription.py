"""转写主链：素材获取、音频规范化、Whisper 转写、分段、对齐与转写恢复。

覆盖本地媒体、纯字幕输入与远程（YouTube）三条获取路径，以及 `resume`
使用的规范化音频 / 词级转写 / 源 cue checkpoint 恢复逻辑。
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from xvt.align import align_source_cues
from xvt.artifacts import ArtifactKind, downstream_of
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
from xvt.errors import DependencyError
from xvt.events import EventSink, EventType, create_event, job_event_fields, redact_text
from xvt.job_store import create_job, get_job_directory, get_job_file, load_job, save_job, utc_now
from xvt.media import MediaInfo, probe_media
from xvt.models import Cue, Job, StageName, StageStatus, Transcript, Word
from xvt.pipeline.common import (
    add_artifact,
    artifact_checksum_for,
    error_category,
    is_subtitle_input,
    load_cues,
    media_info_from_remote_metadata,
    record_media_download_selection,
    set_stage,
    write_json,
)
from xvt.pipeline.review import complete_qa_stage
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
from xvt.workflow_config import clear_burn_fingerprint, get_workflow_config


@dataclass(frozen=True, slots=True)
class AcquiredSubtitleCandidate:
    path: Path
    kind: SourceCandidateKind


@dataclass(frozen=True, slots=True)
class RebuildSourceResult:
    source_cues_path: Path
    source_srt_path: Path
    qa_path: Path
    selection_path: Path


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
    write_json(
        path,
        {
            "candidate_kind": evaluation.kind.value,
            "report": asdict(evaluation.report),
            "repairs": [asdict(repair) for repair in evaluation.repairs],
        },
    )
    add_artifact(job, "source_candidate_qa", path, event_sink)
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
    source_checksum = artifact_checksum_for(job, "source_cues")
    if source_checksum is None:
        raise RuntimeError("source selection requires a registered source_cues artifact")
    path = get_job_directory(job) / "artifacts" / "source-selection.json"
    write_json(
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
    add_artifact(job, "source_selection", path, event_sink)
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


def source_selection_matches(job: Job) -> bool:
    """源选择登记是否仍与当前 source_cues 一致：源腿满足度的**唯一**实现。

    公有化原因（F5）：``workflow.py`` 曾逐字复制一份 ``_source_selection_satisfied``，
    只差一个 helper 名。给 ``selected_kind`` 加新分支时只改一边，``continue`` 与
    ``resume`` 对「源是否满足」的判定就会分叉。现在 workflow 经 ``xvt.pipeline``
    引用本函数，跨模块使用的私有名按 R1.4 的先例公有化。
    """

    path = valid_artifact_path(job, "source_selection")
    source_checksum = artifact_checksum_for(job, "source_cues")
    if path is None or source_checksum is None:
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict) or data.get("source_cues_checksum") != source_checksum:
        return False
    if data.get("selected_kind") == "human_reviewed":
        revision_path = valid_artifact_path(job, "source_review_revision")
        if revision_path is None:
            return False
        try:
            revision = json.loads(revision_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        return (
            isinstance(revision, dict)
            and revision.get("reviewed_source_cues_checksum") == source_checksum
        )
    return (
        data.get("candidate_kind") == SourceCandidateKind.NONE
        or job.config.get("input_kind") == "subtitle"
        or valid_artifact_path(job, "source_candidate_qa") is not None
    )


def _normalize_audio(
    job: Job,
    source: Path | str,
    audio_path: Path,
    event_sink: EventSink | None,
) -> None:
    """把源媒体转成 16 kHz 单声道 PCM WAV 并登记 normalized_audio artifact。

    NORMALIZE_AUDIO 的 stage 迁移与 checkpoint 复用判断都留在调用方：
    两个调用点的失败归属分别挂在 StageName.NORMALIZE_AUDIO 与局部 current_stage 上，
    把 set_stage 挪进来会改变异常路径的 stage 归属。
    """
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
    add_artifact(job, "normalized_audio", audio_path, event_sink)


def _confirm_reused_stage(
    job: Job,
    stage: StageName,
    event_sink: EventSink | None,
) -> None:
    """checkpoint 有效但 stage 还没记成功时补记一次成功。

    已经是 SUCCEEDED 时**什么都不做**：复用 checkpoint 没有做任何工作，刷新
    ``completed_at`` 会让 ``started_at`` → ``completed_at`` 跨过一段什么都没干的时间，
    stage 时间戳就再也不能用来算真实耗时。补记时同时清掉上一次中断留下的 ``started_at``，
    与 ``rebuild_source`` 对同类 stage 的处理同口径。
    """

    record = job.stages[stage]
    if record.status is StageStatus.SUCCEEDED:
        return
    record.started_at = None
    set_stage(job, stage, StageStatus.SUCCEEDED, event_sink)


def _record_effective_engine(
    job: Job,
    requested_engine: str,
    transcript: Transcript,
) -> None:
    """把实际生效的 ASR 引擎写回 ``job.config["engine"]``。

    ``transcribe_audio`` 在只装了一种后端时会回退到另一种引擎，产物里的 ``engine``
    因此可能与用户请求的不一致。``job.config["engine"]`` 的唯一用途是「下次要用哪个引擎」
    （`resume` / `rebuild-source` 都从它取值），所以这里直接改写它而不另加字段——
    另加「实际引擎」字段会立刻产生两份口径互相漂移。用户原本请求的引擎留在 JSONL 的
    ``transcription_fallback`` 事件里，那条记录是持久的。
    """

    if transcript.engine == requested_engine:
        return
    job.config["engine"] = transcript.engine
    save_job(job)


def _normalize_audio_and_transcribe(
    job: Job,
    source: Path | None,
    media_info: Any,
    *,
    engine: str,
    language: str | None,
    event_sink: EventSink | None,
    on_stage: Callable[[StageName], None] | None = None,
) -> Transcript:
    """复用 checkpoint 或执行规范化音频 + 词级转写。

    ``on_stage`` 是**失败归属的回传通道**：本函数自己迁移 NORMALIZE_AUDIO / TRANSCRIBE，
    但不碰调用方的 ``current_stage``。调用方的 ``except`` 用 ``current_stage`` 决定把失败
    记到哪个 stage，不回传就会把转写失败记到调用前的那个 stage（历史上是 IMPORT_SUBTITLES，
    而那次运行根本没导入字幕），同时让 TRANSCRIBE 永远停在 running。
    """

    def enter(stage: StageName) -> None:
        if on_stage is not None:
            on_stage(stage)

    artifacts_directory = get_job_directory(job) / "artifacts"
    transcript_path = valid_artifact_path(job, "word_transcript")
    if transcript_path is not None:
        # 这条早退不碰任何 stage：`rebuild_source` 会在复用 words 时刻意把
        # NORMALIZE_AUDIO 记成 SKIPPED，补记成 SUCCEEDED 会抹掉那条真状态。
        # 只回传失败归属，让 _load_transcript 的读盘失败记在 TRANSCRIBE 上。
        enter(StageName.TRANSCRIBE)
        return _load_transcript(transcript_path)

    if source is None and valid_artifact_path(job, "normalized_audio") is None:
        raise RuntimeError("audio acquisition is required for transcription or alignment")
    audio_path = valid_artifact_path(job, "normalized_audio")
    enter(StageName.NORMALIZE_AUDIO)
    if audio_path is None:
        set_stage(job, StageName.NORMALIZE_AUDIO, StageStatus.RUNNING, event_sink)
        audio_path = artifacts_directory / "audio.wav"
        _normalize_audio(job, source, audio_path, event_sink)
        set_stage(job, StageName.NORMALIZE_AUDIO, StageStatus.SUCCEEDED, event_sink)
    else:
        _confirm_reused_stage(job, StageName.NORMALIZE_AUDIO, event_sink)

    enter(StageName.TRANSCRIBE)
    set_stage(job, StageName.TRANSCRIBE, StageStatus.RUNNING, event_sink)
    transcript = transcribe_audio(
        audio_path,
        engine=engine,
        language=language,
        event_sink=event_sink,
        total_ms=media_info.duration_ms,
    )
    _record_effective_engine(job, engine, transcript)
    transcript_path = artifacts_directory / "words.json"
    write_json(transcript_path, asdict(transcript))
    add_artifact(job, "word_transcript", transcript_path, event_sink)
    set_stage(job, StageName.TRANSCRIBE, StageStatus.SUCCEEDED, event_sink)
    return transcript


def _run_source_subtitle_qa(
    job: Job,
    cues: list[Cue],
    artifacts_directory: Path,
    *,
    media_info: MediaInfo | None,
    event_sink: EventSink | None,
) -> bool:
    """跑源字幕 QA、写 qa.json、登记 subtitle_qa artifact，返回 QA 是否放行。

    只覆盖 SUBTITLE_QA 一段。调用方仍要在调用前把自己的 current_stage 置为
    StageName.SUBTITLE_QA —— 失败归属留在调用方，渲染段的失败才不会被记到 QA 上。
    """
    set_stage(
        job,
        StageName.SUBTITLE_QA,
        StageStatus.RUNNING,
        event_sink,
        extras={"detail": "source"},
    )
    report = check_subtitles(
        cues,
        media_duration_ms=media_info.duration_ms if media_info is not None else None,
        video_width=media_info.width if media_info is not None else None,
        video_height=media_info.height if media_info is not None else None,
    )
    qa_path = artifacts_directory / "qa.json"
    write_json(qa_path, asdict(report))
    add_artifact(job, "subtitle_qa", qa_path, event_sink)
    return complete_qa_stage(job, report, event_sink)


def _render_source_srt(
    job: Job,
    cues: list[Cue],
    artifacts_directory: Path,
    event_sink: EventSink | None,
) -> None:
    """写 source.srt、登记 source_srt artifact，并把 RENDER 记为成功。"""
    set_stage(
        job,
        StageName.RENDER,
        StageStatus.RUNNING,
        event_sink,
        extras={"detail": "source"},
    )
    subtitle_path = save_subtitles(cues, artifacts_directory / "source.srt")
    add_artifact(job, "source_srt", subtitle_path, event_sink)
    set_stage(job, StageName.RENDER, StageStatus.SUCCEEDED, event_sink)


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
        set_stage(job, stage, StageStatus.SKIPPED, event_sink)

    current_stage = StageName.IMPORT_SUBTITLES
    candidate_kind = candidate.kind if candidate else SourceCandidateKind.NONE
    imported_cues: list[Cue] | None = None
    evaluation: SourceCandidateEvaluation | None = None

    def enter_stage(stage: StageName) -> None:
        """跟随 ``_normalize_audio_and_transcribe`` 内部的 stage 迁移推进失败归属。"""

        nonlocal current_stage
        current_stage = stage

    try:
        if candidate is None:
            set_stage(job, StageName.IMPORT_SUBTITLES, StageStatus.SKIPPED, event_sink)
        else:
            set_stage(job, StageName.IMPORT_SUBTITLES, StageStatus.RUNNING, event_sink)
            if not candidate.path.is_file():
                raise ValueError(f"subtitle file does not exist: {candidate.path}")
            imported_path = artifacts_directory / f"input-subtitles{candidate.path.suffix.lower()}"
            if candidate.path.resolve() != imported_path.resolve():
                shutil.copy2(candidate.path, imported_path)
            add_artifact(job, "input_subtitles", imported_path, event_sink)
            set_stage(job, StageName.IMPORT_SUBTITLES, StageStatus.SUCCEEDED, event_sink)
            try:
                imported_cues = load_subtitles(
                    imported_path,
                    role="source",
                    source_language=language,
                    # 报错要指向用户/下载得到的原始文件，不是 Job 内部副本。
                    display_path=candidate.path,
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
                on_stage=enter_stage,
            )
        else:
            for stage in (StageName.NORMALIZE_AUDIO, StageName.TRANSCRIBE):
                set_stage(job, stage, StageStatus.SKIPPED, event_sink)

        alignment_confidence: float | None = None
        selected_kind = "subtitles"
        selection_reason = plan.reason
        reference_allowed = plan.reference_allowed
        if plan.action is SourceSelectionAction.ALIGN:
            if imported_cues is None or transcript is None:
                raise RuntimeError("subtitle alignment requires subtitle cues and a transcript")
            current_stage = StageName.ALIGN
            set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
            alignment = align_source_cues(imported_cues, transcript)
            alignment_confidence = alignment.confidence
            alignment_path = artifacts_directory / "alignment.json"
            write_json(alignment_path, asdict(alignment))
            add_artifact(job, "subtitle_alignment", alignment_path, event_sink)
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
                # 强制对齐同样受对齐后 QA error 门约束（置信度门保持既有语义，
                # --allow-low-confidence-alignment 仍可放行低置信但 QA 干净的对齐）。
                if any(issue.severity == "error" for issue in aligned_report.issues):
                    # `--align-subtitles` 必须与 `--subtitles` 同用，去掉该 flag 后
                    # 源选择走 SourceSelectionAction.SUBTITLES（user_subtitles_preserved），
                    # 保留的是用户原时间轴，不是 ASR 分段；要用 ASR 时间轴须显式
                    # `--source-strategy asr`（该策略不能与 `--align-subtitles` 同用）。
                    raise ValueError(
                        "aligned subtitles failed subtitle QA with error-level issues; "
                        "rerun without --align-subtitles to keep the source subtitle "
                        "timeline as-is, use --source-strategy asr to build the timeline "
                        "from speech instead, or fix the source subtitles"
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
                set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
                cues = segment_transcript(
                    transcript,
                    segmentation_config_for_preset(
                        str(job.config.get("segmentation_preset", DEFAULT_SEGMENTATION_PRESET))
                    ),
                    media_duration_ms=media_info.duration_ms,
                )
                if not cues:
                    raise RuntimeError("transcription produced no subtitle cues")
                set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)
                selected_kind = "asr"
                reference_allowed = False
                selection_reason = (
                    "alignment_below_threshold"
                    if alignment.confidence < minimum_alignment_confidence
                    else "alignment_post_qa_failed"
                )
            set_stage(job, StageName.ALIGN, StageStatus.SUCCEEDED, event_sink)
            if selected_kind == "aligned_subtitles":
                set_stage(job, StageName.SEGMENT, StageStatus.SKIPPED, event_sink)
        elif plan.action is SourceSelectionAction.ASR:
            set_stage(job, StageName.ALIGN, StageStatus.SKIPPED, event_sink)
            current_stage = StageName.SEGMENT
            set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
            cues = segment_transcript(
                transcript,
                segmentation_config_for_preset(
                    str(job.config.get("segmentation_preset", DEFAULT_SEGMENTATION_PRESET))
                ),
                media_duration_ms=media_info.duration_ms,
            )
            if not cues:
                raise RuntimeError("transcription produced no subtitle cues")
            set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)
            selected_kind = "asr"
        else:
            if imported_cues is None:
                raise RuntimeError("subtitle selection requires imported subtitle cues")
            cues = imported_cues
            set_stage(job, StageName.ALIGN, StageStatus.SKIPPED, event_sink)
            set_stage(job, StageName.SEGMENT, StageStatus.SKIPPED, event_sink)

        cues_path = artifacts_directory / "source-cues.json"
        write_json(cues_path, [asdict(cue) for cue in cues])
        add_artifact(job, "source_cues", cues_path, event_sink)
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
                segmentation_config_for_preset(
                    str(job.config.get("segmentation_preset", DEFAULT_SEGMENTATION_PRESET))
                ),
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
        set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
        media_info = probe_media(source)
        media_path = get_job_directory(job) / "artifacts" / "media.json"
        media_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(media_path, asdict(media_info))
        add_artifact(job, "media_info", media_path, event_sink)
        set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)
        set_stage(job, StageName.ACQUIRE, StageStatus.SKIPPED, event_sink)
    except Exception as error:
        set_stage(
            job,
            current_stage,
            StageStatus.FAILED,
            event_sink,
            error=str(error),
            error_category=error_category(error),
        )
        raise
    # 本函数自己用上面的字面量 config 创建 Job，`source_strategy` 必然在 `job.config` 里，
    # 因此不存在走 `_process_acquired_media` 旧路径的首跑分支（旧 Job 只经
    # `resume_remote_transcription` 进入，那里仍保留键缺失的判定）。
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


def _finish_subtitle_job(
    job: Job,
    cues: list[Cue],
    *,
    event_sink: EventSink | None,
    record_source_selection: bool = False,
) -> Job:
    artifacts_directory = get_job_directory(job) / "artifacts"
    cues_path = artifacts_directory / "source-cues.json"
    write_json(cues_path, [asdict(cue) for cue in cues])
    add_artifact(job, "source_cues", cues_path, event_sink)
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
        set_stage(job, StageName.TRANSLATE, StageStatus.SKIPPED, event_sink)

    current_stage = StageName.SUBTITLE_QA
    try:
        if not _run_source_subtitle_qa(
            job, cues, artifacts_directory, media_info=None, event_sink=event_sink
        ):
            return job

        current_stage = StageName.RENDER
        _render_source_srt(job, cues, artifacts_directory, event_sink)
        for stage in (StageName.BURN, StageName.MEDIA_QA):
            set_stage(job, stage, StageStatus.SKIPPED, event_sink)
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
            set_stage(job, stage, StageStatus.SKIPPED, event_sink)
        set_stage(job, StageName.IMPORT_SUBTITLES, StageStatus.RUNNING, event_sink)
        artifacts_directory = get_job_directory(job) / "artifacts"
        artifacts_directory.mkdir(parents=True, exist_ok=True)
        imported_path = artifacts_directory / f"input-subtitles{source.suffix.lower()}"
        shutil.copy2(source, imported_path)
        cues = load_subtitles(
            imported_path,
            role="source",
            source_language=language,
            # 报错要指向用户传入的字幕文件，不是 Job 内部副本。
            display_path=source,
        )
        if not cues:
            raise ValueError("subtitle file contains no usable cues")
        add_artifact(job, "input_subtitles", imported_path, event_sink)
        set_stage(job, StageName.IMPORT_SUBTITLES, StageStatus.SUCCEEDED, event_sink)
    except Exception as error:
        set_stage(
            job,
            StageName.IMPORT_SUBTITLES,
            StageStatus.FAILED,
            event_sink,
            error=str(error),
            error_category=error_category(error),
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


def _load_remote_info(path: Path) -> RemoteMediaInfo:
    data = json.loads(path.read_text(encoding="utf-8"))
    return RemoteMediaInfo(
        id=str(data["id"]),
        title=str(data["title"]),
        duration_ms=int(data["duration_ms"]),
        webpage_url=str(data["webpage_url"]),
        subtitle_languages=tuple(data.get("subtitle_languages", ())),
        automatic_subtitle_languages=tuple(data.get("automatic_subtitle_languages", ())),
        subtitles_anonymous=data.get("subtitles_anonymous", False),
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
    """登记恢复出的 source cues，补齐源选择登记，再跑源字幕 QA 与源 SRT 渲染。

    QA 段与渲染段的失败归属由局部 ``current_stage`` + 一个 ``except`` 承担，写法与
    同文件的姐妹终结函数 ``_finish_subtitle_job`` 同构：这里调用的
    ``_run_source_subtitle_qa`` / ``_render_source_srt`` 各自只覆盖一个 stage，
    调用方自己就知道当前在哪一段，不需要 ``_normalize_audio_and_transcribe`` 那套
    ``on_stage`` 回传——那个回传存在的唯一理由是被调函数在内部跨了两个 stage
    （NORMALIZE_AUDIO → TRANSCRIBE），调用方无法得知它停在哪一个。

    刻意**不**在 ``except`` 里「扫 stages 找状态为 RUNNING 的那个」：恢复路径上可能
    留着上一次中断遗留的 RUNNING，那样会把本次失败错挂到它身上。

    ``except`` 里的 FAILED 判断不可省：``_run_source_subtitle_qa`` 在 QA 报告整体
    ``failed`` 时经 ``complete_qa_stage`` 自己把 SUBTITLE_QA 记成 FAILED 并抛
    ``RuntimeError``，无条件改写会把那条具体的 QA 原因换成通用异常文本。而 QA 不放行
    并返回 False（``needs_review`` → AWAITING_INPUT）是正常业务分支，``complete_qa_stage``
    已自行记账，这里按原样早退、不当异常处理。

    source cues 落盘与源选择补登记刻意留在 ``try`` **之外**：``resume`` 到达这里时
    SEGMENT / ALIGN 都已记成功，这段写盘不属于任何正在跑的 stage，硬塞给 SUBTITLE_QA
    就是另一种错归属。它失败时不会留下 RUNNING 的 stage，异常照常上抛由 CLI 记账。
    """

    artifacts_directory = get_job_directory(job) / "artifacts"
    cues_path = artifacts_directory / "source-cues.json"
    write_json(cues_path, [asdict(cue) for cue in cues])
    add_artifact(job, "source_cues", cues_path, event_sink)
    if "source_strategy" in job.config and not source_selection_matches(job):
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

    current_stage = StageName.SUBTITLE_QA
    try:
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
                load_cues(cues_path),
                event_sink=event_sink,
                record_source_selection=(
                    "source_strategy" in job.config and not source_selection_matches(job)
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
        return _finish_resumed_source(job, load_cues(cues_path), media_info, event_sink)

    if "source_strategy" in job.config:
        # 远程 Job 的 job.input 是 URL，Path(URL) 会把 "https://host" 折叠成
        # "https:/host" 并被当作本地文件传给 ffmpeg。优先复用已下载的本地媒体，
        # 否则委派 resume_remote_transcription 重新获取。
        parsed_input = urlparse(job.input)
        if parsed_input.scheme in {"http", "https"} and parsed_input.hostname:
            local_media = valid_artifact_path(job, "source_video") or valid_artifact_path(
                job, "downloaded_media"
            )
            if local_media is None:
                return resume_remote_transcription(job_file, event_sink=event_sink)
            adaptive_source: Path | None = local_media
        else:
            adaptive_source = Path(job.input)
        candidate = _candidate_from_job(job)
        return _process_adaptive_media(
            job,
            adaptive_source,
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
        set_stage(job, StageName.TRANSCRIBE, StageStatus.RUNNING, event_sink)
        language = job.config.get("source_language")
        requested_engine = str(job.config.get("engine", DEFAULT_ENGINE))
        transcript = transcribe_audio(
            audio_path,
            engine=requested_engine,
            language=None if language == "auto" else language,
            event_sink=event_sink,
            total_ms=media_info.duration_ms,
        )
        _record_effective_engine(job, requested_engine, transcript)
        transcript_path = get_job_directory(job) / "artifacts" / "words.json"
        write_json(transcript_path, asdict(transcript))
        add_artifact(job, "word_transcript", transcript_path, event_sink)
        set_stage(job, StageName.TRANSCRIBE, StageStatus.SUCCEEDED, event_sink)

    if job.config.get("align_subtitles"):
        subtitle_path = valid_artifact_path(job, "input_subtitles")
        if subtitle_path is None:
            raise ValueError("job has no valid input_subtitles checkpoint")
        imported_cues = load_subtitles(
            subtitle_path,
            role="source",
            source_language=job.config.get("source_language"),
        )
        set_stage(job, StageName.ALIGN, StageStatus.RUNNING, event_sink)
        alignment = align_source_cues(imported_cues, transcript)
        alignment_path = get_job_directory(job) / "artifacts" / "alignment.json"
        write_json(alignment_path, asdict(alignment))
        add_artifact(job, "subtitle_alignment", alignment_path, event_sink)
        minimum_confidence = float(
            job.config.get("minimum_alignment_confidence", DEFAULT_MIN_ALIGNMENT_CONFIDENCE)
        )
        if (
            not job.config.get("allow_low_confidence_alignment", False)
            and alignment.confidence < minimum_confidence
        ):
            # 文案与 `_process_adaptive_media` 的同一失败对齐：那一侧经外层 except
            # 边界写出带实测值与阈值的完整句，这里也必须给出数值，否则同一种失败在
            # `job.json` 与 `status --json` 里有两种详细程度。
            message = (
                "subtitle alignment confidence is below the required threshold "
                f"({alignment.confidence:.3f} < {minimum_confidence:.3f})"
            )
            set_stage(
                job,
                StageName.ALIGN,
                StageStatus.FAILED,
                event_sink,
                error=message,
            )
            raise ValueError(message)
        set_stage(job, StageName.ALIGN, StageStatus.SUCCEEDED, event_sink)
        cues = list(alignment.cues)
    else:
        set_stage(job, StageName.SEGMENT, StageStatus.RUNNING, event_sink)
        try:
            cues = segment_transcript(
                transcript,
                segmentation_config_for_preset(
                    str(job.config.get("segmentation_preset", DEFAULT_SEGMENTATION_PRESET))
                ),
                media_duration_ms=media_info.duration_ms,
            )
        except ValueError as error:
            set_stage(
                job,
                StageName.SEGMENT,
                StageStatus.FAILED,
                event_sink,
                error=str(error),
                error_category=error_category(error),
            )
            raise
        if not cues:
            set_stage(
                job,
                StageName.SEGMENT,
                StageStatus.FAILED,
                event_sink,
                error="transcription produced no subtitle cues",
            )
            raise RuntimeError("transcription produced no subtitle cues")
        set_stage(job, StageName.SEGMENT, StageStatus.SUCCEEDED, event_sink)
    return _finish_resumed_source(job, cues, media_info, event_sink)


def rebuild_source(
    job_file: str | Path,
    *,
    strategy: str,
    cookies_from_browser: str | None = None,
    proxy: str | None = None,
    event_sink: EventSink | None = None,
) -> RebuildSourceResult:
    """显式使用 ASR 重建既有 Job 的 source cues，并废弃全部旧下游产物。"""

    if strategy != SourceStrategy.ASR:
        raise ValueError("rebuild source strategy must be asr")

    requested_job_file = Path(job_file).expanduser().resolve()
    job = load_job(requested_job_file)
    if requested_job_file != get_job_file(job).resolve():
        raise ValueError("job file path does not match the Job's persisted identity")
    if job.config.get("input_kind") == "subtitle":
        raise ValueError("source rebuild requires media input; subtitle-only jobs are unsupported")

    engine = str(job.config.get("engine", DEFAULT_ENGINE))
    if engine not in {"mlx", "faster"}:
        raise ValueError(f"unsupported transcription engine: {engine}")
    source_language = job.config.get("source_language")
    if source_language is not None and not isinstance(source_language, str):
        raise ValueError("source_language must be a string or null")
    language = None if source_language in {None, "auto"} else str(source_language)
    segmentation_config_for_preset(
        str(job.config.get("segmentation_preset", DEFAULT_SEGMENTATION_PRESET))
    )
    minimum_alignment_confidence_value = job.config.get(
        "minimum_alignment_confidence", DEFAULT_MIN_ALIGNMENT_CONFIDENCE
    )
    if isinstance(minimum_alignment_confidence_value, bool) or not isinstance(
        minimum_alignment_confidence_value, int | float
    ):
        raise ValueError("minimum_alignment_confidence must be a number")
    minimum_alignment_confidence = float(minimum_alignment_confidence_value)
    allow_low_confidence_alignment_value = job.config.get("allow_low_confidence_alignment", False)
    if not isinstance(allow_low_confidence_alignment_value, bool):
        raise ValueError("allow_low_confidence_alignment must be a boolean")
    allow_low_confidence_alignment = allow_low_confidence_alignment_value

    parsed = urlparse(job.input)
    is_remote = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
    word_transcript = valid_artifact_path(job, "word_transcript")
    normalized_audio = valid_artifact_path(job, "normalized_audio")
    media_info = valid_artifact_path(job, "media_info")
    reusable_remote_media = None
    reusable_remote_media_info = None
    if is_remote and word_transcript is None and normalized_audio is None:
        reusable_remote_media = valid_artifact_path(job, "source_video") or valid_artifact_path(
            job, "downloaded_media"
        )
        if reusable_remote_media is not None:
            reusable_remote_media_info = probe_media(reusable_remote_media)
    if not is_remote:
        source = Path(job.input).expanduser()
        has_checkpoint = media_info is not None and (
            word_transcript is not None or normalized_audio is not None
        )
        if not source.is_file() and not has_checkpoint:
            raise ValueError("source rebuild requires an available local media file")
        if source.is_file() and not has_checkpoint:
            probed = probe_media(source)
            media_info = get_job_directory(job) / "artifacts" / "media.json"
            write_json(media_info, asdict(probed))
            add_artifact(job, "media_info", media_info, event_sink)

    invalidated_kinds = downstream_of(ArtifactKind.SOURCE_CUES) | {
        ArtifactKind.SOURCE_CANDIDATE_QA,
        ArtifactKind.SOURCE_CUES,
        ArtifactKind.SOURCE_REVIEW_REVISION,
        ArtifactKind.SUBTITLE_ALIGNMENT,
        ArtifactKind.TRANSLATION_RESEGMENTATION,
        ArtifactKind.TRANSLATION_REVIEW_REVISION,
    }
    invalidated = [artifact for artifact in job.artifacts if artifact.kind in invalidated_kinds]
    job.artifacts = [
        artifact for artifact in job.artifacts if artifact.kind not in invalidated_kinds
    ]
    job.config["source_strategy"] = SourceStrategy.ASR.value
    job.config["source_rebuild_requested"] = True
    job.config["align_subtitles"] = False
    clear_burn_fingerprint(job)

    stages_to_reset = {
        StageName.IMPORT_SUBTITLES,
        StageName.ALIGN,
        StageName.SEGMENT,
        StageName.SUBTITLE_QA,
        StageName.RENDER,
        StageName.TRANSLATE,
        StageName.BURN,
        StageName.MEDIA_QA,
    }
    if word_transcript is None:
        stages_to_reset.add(StageName.TRANSCRIBE)
    else:
        record = job.stages[StageName.TRANSCRIBE]
        if record.status is not StageStatus.SUCCEEDED:
            record.started_at = None
            record.completed_at = utc_now()
        record.status = StageStatus.SUCCEEDED
        record.error = None
    if word_transcript is None and normalized_audio is None:
        stages_to_reset.add(StageName.NORMALIZE_AUDIO)
    elif normalized_audio is not None:
        record = job.stages[StageName.NORMALIZE_AUDIO]
        if record.status is not StageStatus.SUCCEEDED:
            record.started_at = None
            record.completed_at = utc_now()
        record.status = StageStatus.SUCCEEDED
        record.error = None
    else:
        record = job.stages[StageName.NORMALIZE_AUDIO]
        record.status = StageStatus.SKIPPED
        record.started_at = None
        record.completed_at = utc_now()
        record.error = None
    for stage in stages_to_reset:
        record = job.stages[stage]
        record.status = StageStatus.PENDING
        record.started_at = None
        record.completed_at = None
        record.error = None
    save_job(job)

    if event_sink:
        for artifact in invalidated:
            event_sink(
                create_event(
                    EventType.ARTIFACT_INVALIDATED,
                    **job_event_fields(job.id, get_job_file(job)),
                    kind=artifact.kind,
                    path=artifact.path,
                )
            )

    if reusable_remote_media_info is not None:
        media_info = get_job_directory(job) / "artifacts" / "media.json"
        write_json(media_info, asdict(reusable_remote_media_info))
        add_artifact(job, "media_info", media_info, event_sink)

    has_transcription_checkpoint = media_info is not None and (
        word_transcript is not None or normalized_audio is not None
    )
    if not is_remote and media_info is not None:
        set_stage(job, StageName.INSPECT, StageStatus.SUCCEEDED, event_sink)
        set_stage(job, StageName.ACQUIRE, StageStatus.SKIPPED, event_sink)
    elif is_remote and (has_transcription_checkpoint or reusable_remote_media is not None):
        set_stage(job, StageName.INSPECT, StageStatus.SUCCEEDED, event_sink)
        set_stage(job, StageName.ACQUIRE, StageStatus.SUCCEEDED, event_sink)

    if has_transcription_checkpoint:
        rebuilt = resume_job(requested_job_file, event_sink=event_sink)
    elif reusable_remote_media is not None and reusable_remote_media_info is not None:
        rebuilt = _process_adaptive_media(
            job,
            reusable_remote_media,
            reusable_remote_media_info,
            engine=engine,
            language=language,
            candidate=_candidate_from_job(job),
            align_subtitles=False,
            minimum_alignment_confidence=minimum_alignment_confidence,
            allow_low_confidence_alignment=allow_low_confidence_alignment,
            event_sink=event_sink,
            emit_job_completed=False,
        )
    elif is_remote:
        rebuilt = resume_remote_transcription(
            requested_job_file,
            cookies_from_browser=cookies_from_browser,
            proxy=proxy,
            event_sink=event_sink,
        )
    else:
        rebuilt = resume_job(requested_job_file, event_sink=event_sink)

    paths = {
        kind: valid_artifact_path(rebuilt, kind)
        for kind in ("source_cues", "source_srt", "subtitle_qa", "source_selection")
    }
    missing = [kind for kind, path in paths.items() if path is None]
    if missing:
        raise RuntimeError(f"source rebuild completed without required artifacts: {missing}")
    return RebuildSourceResult(
        source_cues_path=paths["source_cues"],
        source_srt_path=paths["source_srt"],
        qa_path=paths["subtitle_qa"],
        selection_path=paths["source_selection"],
    )

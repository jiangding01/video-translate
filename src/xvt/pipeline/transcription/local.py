"""本地输入腿：本地媒体的自适应转写、ASR 机制与纯字幕输入 Job。

``_process_adaptive_media`` 是现代媒体 Job 的主路径（候选 QA → 源选择 →
必要时对齐或回落 ASR → 分段），远程腿与 ``resume`` 腿都复用它；音频规范化、
Whisper 调用与 checkpoint 复用的 ASR 机制也在这里，供远程腿与 ``resume``
腿共用。纯字幕输入 Job 不走 ASR，但同属本地输入，一并放在本模块。
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from xvt.align import align_source_cues
from xvt.events import EventSink, EventType, create_event, job_event_fields
from xvt.job_store import create_job, get_job_directory, get_job_file, save_job
from xvt.media import probe_media
from xvt.models import Cue, Job, StageName, StageStatus, Transcript, Word
from xvt.pipeline.common import add_artifact, error_category, set_stage, write_json
from xvt.pipeline.transcription.shared import (
    AcquiredSubtitleCandidate,
    _record_candidate_qa,
    _record_source_selection,
    _render_source_srt,
    _run_source_subtitle_qa,
    _segmentation_config,
)
from xvt.process import run_command
from xvt.qa import check_subtitles
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
from xvt.stages import SUBTITLE_INPUT_SKIPS, TRAILING_TRANSCRIPTION_SKIPS
from xvt.subtitles import SUPPORTED_SUBTITLE_EXTENSIONS, load_subtitles
from xvt.transcribe import transcribe_audio
from xvt.workflow_config import get_workflow_config


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
            # 无条件先落盘再判定：这份报告是 `alignment_accepted`、强制对齐的 QA error 门
            # 与回落 ASR 的依据，而强制对齐被该门拒绝时函数直接抛出。写在任何分支之前，
            # 抛出路径上也必然留下可校验的产物；`add_artifact` 自己 `save_job`，
            # 登记随之落到磁盘上的 `job.json`，不依赖后续的收尾。它只是证据，不参与
            # 放行与阻断的判定，也只在对齐真的跑过时存在（与 `subtitle_alignment` 同一条件）。
            aligned_qa_path = artifacts_directory / "aligned-subtitle-qa.json"
            write_json(aligned_qa_path, asdict(aligned_report))
            add_artifact(job, "aligned_subtitle_qa", aligned_qa_path, event_sink)
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
                # 对齐这一步到此已经做完：算出了 alignment、落盘了 alignment.json 与
                # aligned-subtitle-qa.json、做出了「不采用」的判定。终态就落在决策完成
                # 的这一刻，**早于**回落分段这项后续工作可能的失败——否则
                # `segment_transcript` 抛出时 `current_stage` 已经交棒给 SEGMENT，
                # 外层 except 只记 SEGMENT FAILED，ALIGN 永久停在 `running`，
                # Job 状态解释不了自己。
                #
                # 刻意不把这行提到 if/elif/else **之前**：强制对齐的两道门（置信度门、
                # 对齐后 QA error 门）就在上面抛出，提前标成功会被外层 except 覆写成
                # failed，在同一个 stage 上造出 #116 修过的「先完成后失败」形态。
                set_stage(job, StageName.ALIGN, StageStatus.SUCCEEDED, event_sink)
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
                selected_kind = "asr"
                reference_allowed = False
                selection_reason = (
                    "alignment_below_threshold"
                    if alignment.confidence < minimum_alignment_confidence
                    else "alignment_post_qa_failed"
                )
            if selected_kind == "aligned_subtitles":
                # 采用对齐结果的两支（强制与自动）在这里落 ALIGN 终态并跳过 SEGMENT；
                # 回落 ASR 的那一支已在交棒给 SEGMENT 之前自己落过。与紧随其后的
                # SEGMENT 记账同一口径：谁走哪条分支，谁自己交出两个 stage 的终态。
                set_stage(job, StageName.ALIGN, StageStatus.SUCCEEDED, event_sink)
                set_stage(job, StageName.SEGMENT, StageStatus.SKIPPED, event_sink)
        elif plan.action is SourceSelectionAction.ASR:
            set_stage(job, StageName.ALIGN, StageStatus.SKIPPED, event_sink)
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


def _load_transcript(path: Path) -> Transcript:
    data = json.loads(path.read_text(encoding="utf-8"))
    return Transcript(
        language=data["language"],
        engine=data["engine"],
        words=tuple(Word(**word) for word in data["words"]),
        language_probability=data.get("language_probability"),
    )

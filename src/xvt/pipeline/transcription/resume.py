"""``resume`` 与 ``rebuild-source``：转写 checkpoint 恢复与源时间轴重建。

``resume_job`` 按 Job 已有的 checkpoint（规范化音频 / 词级转写 / 源 cue）
决定从哪一步续跑，``rebuild_source`` 在用户明确要求时以 ASR 重建源时间轴。
``resume_job`` 的对齐分支与 ``remote`` 的 ``_process_acquired_media`` 同属
**冻结的 legacy 路径**（仅 config 缺 ``source_strategy`` 的旧 Job 可达）。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse

from xvt.align import align_source_cues
from xvt.artifacts import ArtifactKind, downstream_of
from xvt.config import DEFAULT_ENGINE, DEFAULT_MIN_ALIGNMENT_CONFIDENCE
from xvt.events import EventSink, EventType, create_event, job_event_fields
from xvt.job_store import get_job_directory, get_job_file, load_job, save_job, utc_now
from xvt.media import MediaInfo, probe_media
from xvt.models import Cue, Job, StageName, StageStatus
from xvt.pipeline.common import add_artifact, error_category, load_cues, set_stage, write_json
from xvt.pipeline.transcription.local import (
    _finish_subtitle_job,
    _load_transcript,
    _process_adaptive_media,
    _record_effective_engine,
)
from xvt.pipeline.transcription.remote import resume_remote_transcription
from xvt.pipeline.transcription.shared import (
    RebuildSourceResult,
    _candidate_from_job,
    _record_candidate_qa,
    _record_source_selection,
    _render_source_srt,
    _run_source_subtitle_qa,
    _segmentation_config,
    source_selection_matches,
)
from xvt.recovery import valid_artifact_path
from xvt.segment import segment_transcript
from xvt.source_selection import (
    SourceCandidateKind,
    SourceStrategy,
    evaluate_source_candidate,
    invalid_source_candidate,
)
from xvt.subtitles import load_subtitles
from xvt.transcribe import transcribe_audio
from xvt.workflow_config import clear_burn_fingerprint


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


def _finish_resumed_source(
    job: Job,
    cues: list[Cue],
    media_info: MediaInfo,
    event_sink: EventSink | None,
) -> Job:
    """登记恢复出的 source cues，补齐源选择登记，再跑源字幕 QA 与源 SRT 渲染。

    QA 段与渲染段的失败归属由局部 ``current_stage`` + 一个 ``except`` 承担，写法与
    ``transcription.local`` 里的姐妹终结函数 ``_finish_subtitle_job`` 同构：这里调用的
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

    # 此分支仅缺 source_strategy 的旧 Job（v0.1.x）可达，刻意冻结 legacy 语义：
    # 无对齐后 QA error 门、无回落 ASR；error 级 issue 由下游 _run_source_subtitle_qa 兜底。
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
                _segmentation_config(job),
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
    # fail-fast 预校验：返回值丢弃，只为在改动 Job 之前对未知 preset 提前失败。
    _segmentation_config(job)
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
        ArtifactKind.ALIGNED_SUBTITLE_QA,
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

"""转写腿三簇共用的记账与只读 helper。

本地转写（``local``）、远程获取（``remote``）与 ``resume`` / ``rebuild``
（``resume``）三簇都要用到的候选 QA 登记、源选择记账、源字幕 QA 与 SRT 渲染
都收在这里，是子包里唯一不依赖任何兄弟模块的叶子。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from xvt.config import DEFAULT_SEGMENTATION_PRESET
from xvt.download import RemoteMediaInfo, select_subtitle_language
from xvt.events import EventSink, EventType, create_event, job_event_fields
from xvt.job_store import get_job_directory, get_job_file
from xvt.media import MediaInfo
from xvt.models import Cue, Job, StageName, StageStatus
from xvt.pipeline.common import add_artifact, artifact_checksum_for, set_stage, write_json
from xvt.pipeline.review import complete_qa_stage
from xvt.qa import check_subtitles
from xvt.recovery import valid_artifact_path
from xvt.segment import SegmentationConfig, segmentation_config_for_preset
from xvt.source_selection import SourceCandidateEvaluation, SourceCandidateKind, SourceStrategy
from xvt.subtitles import save_subtitles


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


def _segmentation_config(job: Job) -> SegmentationConfig:
    """Job 分段 preset 的唯一查询点；未知 preset 抛 ValueError（调用方各自记账）。"""
    return segmentation_config_for_preset(
        str(job.config.get("segmentation_preset", DEFAULT_SEGMENTATION_PRESET))
    )


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


def _translated_leg_blocks_subtitle_qa(job: Job) -> bool:
    """``SUBTITLE_QA`` 此刻是不是被译文腿的未决阻断占着。

    判据与 ``recovery.subtitle_qa_failure_guidance`` 同口径：stage 停在 FAILED /
    AWAITING_INPUT，且译文 QA 报告仍 checksum 有效——那条报告就是 guidance 会拿去
    解释阻断、并据此给出 ``continue --acknowledge-subtitle-review`` 的那一份。
    译文报告已失效时不算（``recovery`` 侧由 ``_translated_qa_needs_rebuild`` 同样让路），
    源腿照常记账。
    """

    return (
        job.stages[StageName.SUBTITLE_QA].status in {StageStatus.FAILED, StageStatus.AWAITING_INPUT}
        and valid_artifact_path(job, "translated_subtitle_qa") is not None
    )


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

    译文腿正占着这个 stage 的阻断时（见 ``_translated_leg_blocks_subtitle_qa``），本函数
    照常重算并重新登记源侧的 ``qa.json``，但不改 stage：产物是真话，stage 的真实状态
    此刻属于更靠后、尚未解决的译文复核。此时连 RUNNING 也不写——写了就得有终态来收，
    而放行分支恰恰不写终态。
    """
    preserve_blocking_stage = _translated_leg_blocks_subtitle_qa(job)
    if not preserve_blocking_stage:
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
    return complete_qa_stage(
        job,
        report,
        event_sink,
        preserve_blocking_stage=preserve_blocking_stage,
    )


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

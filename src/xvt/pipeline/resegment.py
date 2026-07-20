"""超宽 ASR 译文 cue 的语义重分段：重建 source cue 并使翻译及下游产物失效。

只对具备词级时间戳的 ASR cue 生效；不拆分旧译文，也不改写用户导入字幕的
时间轴。失效集合由 artifact 依赖图闭包推导。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from math import ceil
from pathlib import Path

from xvt.artifacts import ArtifactKind, downstream_of
from xvt.events import EventSink, EventType, create_event, job_event_fields
from xvt.job_store import get_job_directory, get_job_file, load_job, save_job
from xvt.models import Artifact, Cue, StageName, StageStatus
from xvt.pipeline.common import (
    _checksum,
    _load_cues,
    _write_json,
)
from xvt.qa import display_width, display_width_review_limit, target_overflow_cue_ids
from xvt.recovery import valid_artifact_path
from xvt.segment import split_cue_by_parts
from xvt.subtitles import normalize_display_text, save_subtitles


@dataclass(frozen=True, slots=True)
class ResegmentationResult:
    changed: bool
    source_cues_path: Path | None = None
    report_path: Path | None = None


def resegment_translation(
    job_file: str | Path,
    *,
    event_sink: EventSink | None = None,
) -> ResegmentationResult:
    job = load_job(job_file)
    if job.config.get("input_kind") == "subtitle":
        raise ValueError(
            "re-segmentation requires ASR word timestamps; subtitle-only jobs are unsupported"
        )
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
    # source_cues 的下游闭包，扣除本函数原位重建的产物，并入被作废的用户输入。
    # cue_retranslation_batches/cue_retranslation_response 引用重分段前的废弃 cue id，
    # 虽然 apply-cue-retranslation 会因 translated_cues 已失效而拒绝执行（无正确性漏洞），
    # 但若不失效会以 checksum 有效的姿态污染 status/verify 与恢复计划的可复用产物清单
    # （P7e：修正 P6a 遗留的「维持现状」排除项）。
    invalidated_kinds = (
        downstream_of(ArtifactKind.SOURCE_CUES)
        - {
            # source_srt 由本函数在下方立即以新内容原位重建，不在此失效；
            # subtitle_qa 只重置 stage、不立即重建，旧报告引用废弃 cue 必须失效（P7f）。
            ArtifactKind.SOURCE_SRT,
        }
    ) | {
        # input_translation_subtitles 是被 resegment 作废的用户输入而非派生产物，显式并入。
        ArtifactKind.INPUT_TRANSLATION_SUBTITLES
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
                    EventType.ARTIFACT_CREATED,
                    **job_event_fields(job.id, get_job_file(job)),
                    kind=artifact.kind,
                    path=artifact.path,
                )
            )
        for artifact in invalidated:
            event_sink(
                create_event(
                    EventType.ARTIFACT_INVALIDATED,
                    **job_event_fields(job.id, get_job_file(job)),
                    kind=artifact.kind,
                    path=artifact.path,
                )
            )
        event_sink(
            create_event(
                EventType.TRANSLATION_RESEGMENTED,
                **job_event_fields(job.id, get_job_file(job)),
                source_cue_count=len(source_cues),
                resegmented_cue_count=len(resegmented),
                report_path=str(report_output),
            )
        )
    return ResegmentationResult(True, source_output, report_output)

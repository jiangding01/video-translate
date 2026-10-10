"""短片段预览与正式烧录：复用同一 ASS 样式、码率与硬件回退逻辑。

`preview` 不进入持久化工作流目标，也不覆盖正式 artifact；`burn` 在媒体 QA
通过后登记 `burned_video` 与 `media_qa` artifact。
"""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from math import isfinite
from pathlib import Path
from urllib.parse import urlparse

from xvt.burn import burn_subtitles, recommended_video_bitrate
from xvt.config import DEFAULT_HARDWARE_ENCODING
from xvt.download import download_remote_media
from xvt.errors import UserInputError
from xvt.events import EventSink, EventType, create_event, job_event_fields
from xvt.job_store import get_job_directory, get_job_file, load_job
from xvt.media import MediaInfo, probe_media
from xvt.models import Cue, Job, StageName, StageStatus
from xvt.paths import assert_safe_output, protected_job_paths, resolve_absolute_output
from xvt.pipeline.common import (
    add_artifact,
    error_category,
    invalidate_preview_artifacts,
    load_cues,
    record_media_download_selection,
    required_glossary_terms,
    set_stage,
    write_json,
)
from xvt.pipeline.review import complete_qa_stage, review_gate_satisfied
from xvt.qa import check_subtitles
from xvt.recovery import atomic_translated_qa_rebuild_command, valid_artifact_path
from xvt.render import AssStyleOptions, save_bilingual_ass
from xvt.workflow_config import get_workflow_config

_WORKFLOW_QA_GATE_GUIDANCE = (
    "run `xvt status JOB` and `xvt continue JOB` to finish subtitle QA first; "
    "a needs_review report can be released with "
    "`xvt continue JOB --acknowledge-subtitle-review`"
)

# 原子 Job 没有持久化 workflow 目标，`xvt continue` 会直接以「job has no persisted
# workflow goal」失败，因此不能沿用 workflow 文案。原子链路上唯一能重算译文 QA 的
# 入口是重跑翻译；`--acknowledge-subtitle-review` 只挂在 `continue` 上，确认必须在
# 端到端 Job 上做。
_ATOMIC_QA_GATE_GUIDANCE = (
    "this job has no persisted workflow goal, so `xvt continue` does not apply; "
    "rerun `xvt translate JOB --target-language LANG` to regenerate the translation "
    "and its QA report, and fix the reported issues at the source; "
    "a needs_review report can only be acknowledged on an end-to-end job "
    "created by `xvt run`"
)


def _qa_gate_guidance(job: Job, *, rebuildable: bool = False) -> str:
    """按 Job 是否有持久化 workflow 目标给出实际可执行的恢复指引。

    ``rebuildable=True`` 只用于「报告缺失或 checksum 失效」这条产物缺口——此时 Job 内
    若还留着 checksum 有效的译文字幕，就能零 API 成本重建报告与渲染产物，门、`status`
    与 CLI 三处必须给同一条出口。报告存在且判定不放行（error / 未确认的 needs_review）
    时不适用：重建只会重新算出同一份阻断报告，真正的处置是修字幕或重跑翻译。
    """

    if get_workflow_config(job) is not None:
        return _WORKFLOW_QA_GATE_GUIDANCE
    rebuild = atomic_translated_qa_rebuild_command(job) if rebuildable else None
    if rebuild is None:
        return _ATOMIC_QA_GATE_GUIDANCE
    return (
        "this job has no persisted workflow goal, so `xvt continue` does not apply; "
        "the translated subtitles in this job are still checksum-valid, so "
        f"`{rebuild}` rebuilds the QA report and the rendered subtitles at zero "
        "translation-API cost (it registers those subtitles as an imported translation); "
        "rerun `xvt translate JOB --target-language LANG` instead when the translation "
        "itself needs to change; "
        "a needs_review report can only be acknowledged on an end-to-end job "
        "created by `xvt run`"
    )


def _assert_translated_qa_gate(job: Job, *, action: str) -> Path:
    """译文 QA 安全门：checksum 有效 + 报告放行判定通过，返回 translated_cues 路径。

    原子 `xvt burn` / `xvt preview` 直达 pipeline，不经 workflow 的
    `subtitle_qa_failure_guidance`，因此门必须落在这里。Fail-closed：没有逃生口，
    `needs_review` 的合法放行走既有 acknowledge 通道。
    """
    translated_path = valid_artifact_path(job, "translated_cues")
    qa_path = valid_artifact_path(job, "translated_subtitle_qa")
    if translated_path is None or qa_path is None:
        raise RuntimeError(
            f"{action} requires checksum-valid translated subtitles and QA report; "
            f"{_qa_gate_guidance(job, rebuildable=True)}"
        )
    try:
        qa_data = json.loads(qa_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"translated subtitle QA report is not readable: {error}") from error
    if not review_gate_satisfied(job, qa_data):
        raise RuntimeError(
            f"translated subtitle QA must pass before {action} "
            f"(status={qa_data.get('status') if isinstance(qa_data, dict) else 'unknown'}); "
            f"{_qa_gate_guidance(job)}"
        )
    return translated_path


def _check_render_layout(
    job: Job,
    translated: list[Cue],
    media_info: MediaInfo,
    options: AssStyleOptions,
    event_sink: EventSink | None,
    *,
    persist: bool = True,
) -> None:
    report = check_subtitles(
        translated,
        require_translation=True,
        measure_layout=True,
        media_duration_ms=media_info.duration_ms,
        video_width=media_info.width,
        video_height=media_info.height,
        render_options=options,
        required_terms=required_glossary_terms(job),
    )
    if not persist:
        payload = asdict(report)
        payload["issues"] = list(payload["issues"])
        if not review_gate_satisfied(job, payload):
            raise UserInputError(
                "subtitle QA blocks rendering for the selected media/fonts; "
                "use the workflow media/fonts or fix the subtitles"
            )
        return
    path = get_job_directory(job) / "artifacts" / "qa-translated.json"
    write_json(path, asdict(report))
    add_artifact(job, "translated_subtitle_qa", path, event_sink)
    if not complete_qa_stage(job, report, event_sink):
        raise UserInputError(
            f"subtitle layout needs review for the current media/fonts; {_qa_gate_guidance(job)}"
        )


def preview_translated_job(
    job_file: str | Path,
    *,
    cue_id: int | None = None,
    start_seconds: float | None = None,
    duration_seconds: float = 15,
    media: str | Path | None = None,
    output: str | Path | None = None,
    hardware_encoding: bool | None = None,
    target_font: str | None = None,
    source_font: str | None = None,
    event_sink: EventSink | None = None,
) -> Path:
    if cue_id is not None and start_seconds is not None:
        raise ValueError("--cue and --start cannot be used together")
    if cue_id is not None and cue_id < 1:
        raise ValueError("--cue must be a positive integer")
    if start_seconds is not None and (not isfinite(start_seconds) or start_seconds < 0):
        raise ValueError("--start must not be negative")
    if not isfinite(duration_seconds) or duration_seconds <= 0:
        raise ValueError("--duration must be positive")

    job = load_job(job_file)
    if job.config.get("input_kind") == "subtitle":
        raise ValueError("preview requires media input")
    translated_path = _assert_translated_qa_gate(job, action="preview")

    workflow_config = get_workflow_config(job)
    if hardware_encoding is None:
        hardware_encoding = (
            workflow_config.hardware_encoding
            if workflow_config is not None
            else DEFAULT_HARDWARE_ENCODING
        )
    if target_font is None and workflow_config is not None:
        target_font = workflow_config.target_font
    if source_font is None and workflow_config is not None:
        source_font = workflow_config.source_font

    configured_media = workflow_config.burn_media if workflow_config is not None else None
    if media is not None:
        media_path = Path(media).expanduser()
    elif configured_media:
        media_path = Path(configured_media).expanduser()
    else:
        parsed = urlparse(job.input)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            media_path = valid_artifact_path(job, "source_video")
            if media_path is None:
                raise ValueError(
                    "remote preview requires a checksum-valid source_video or explicit --media"
                )
        else:
            media_path = Path(job.input).expanduser()
    if not media_path.is_file():
        raise ValueError(f"video file does not exist: {media_path}")
    media_path = media_path.resolve()
    media_info = probe_media(media_path)
    if not media_info.has_video or not media_info.width or not media_info.height:
        raise ValueError("preview requires a video stream with known dimensions")
    _check_render_layout(
        job,
        load_cues(translated_path),
        media_info,
        AssStyleOptions(target_font=target_font, source_font=source_font),
        event_sink,
        persist=False,
    )
    if media_info.duration_ms <= 0:
        raise ValueError("preview requires media with a known positive duration")

    translated = load_cues(translated_path)
    selected_cue = None
    if start_seconds is None:
        if cue_id is None:
            selected_cue = next((cue for cue in translated if cue.target_text), None)
            if selected_cue is None:
                raise ValueError("translated subtitles contain no previewable cues")
        else:
            selected_cue = next((cue for cue in translated if cue.id == cue_id), None)
            if selected_cue is None:
                raise ValueError(f"translated subtitles do not contain cue {cue_id}")

    requested_duration_ms = round(duration_seconds * 1000)
    if selected_cue is not None:
        preview_duration_ms = max(
            requested_duration_ms,
            selected_cue.end_ms - selected_cue.start_ms + 4000,
        )
        preview_duration_ms = min(preview_duration_ms, media_info.duration_ms)
        start_ms = max(selected_cue.start_ms - 2000, 0)
        end_ms = min(start_ms + preview_duration_ms, media_info.duration_ms)
        start_ms = max(end_ms - preview_duration_ms, 0)
    else:
        start_ms = round((start_seconds or 0) * 1000)
        if start_ms >= media_info.duration_ms:
            raise ValueError("--start must be before the end of the media")
        end_ms = min(start_ms + requested_duration_ms, media_info.duration_ms)

    preview_cues = [
        replace(
            cue,
            start_ms=max(cue.start_ms, start_ms) - start_ms,
            end_ms=min(cue.end_ms, end_ms) - start_ms,
        )
        for cue in translated
        if cue.start_ms < end_ms and cue.end_ms > start_ms and cue.target_text
    ]
    if not preview_cues:
        raise ValueError("preview range contains no translated subtitle cues")

    artifacts_directory = get_job_directory(job) / "artifacts"
    ass_path = artifacts_directory / "preview.ass"
    output_path = resolve_absolute_output(
        output if output is not None else artifacts_directory / "preview.mp4",
        label="preview output",
    )
    assert_safe_output(
        output_path,
        protected=protected_job_paths(
            job,
            media_path,
            writing_kind="preview_video",
            extra=(ass_path,),
        ),
        label="preview output",
    )
    invalidate_preview_artifacts(job)
    if event_sink:
        event_sink(
            create_event(
                EventType.PREVIEW_STARTED,
                **job_event_fields(job.id, get_job_file(job)),
                start_ms=start_ms,
                duration_ms=end_ms - start_ms,
            )
        )
    save_bilingual_ass(
        preview_cues,
        ass_path,
        video_width=media_info.width,
        video_height=media_info.height,
        options=AssStyleOptions(target_font=target_font, source_font=source_font),
    )
    add_artifact(job, "preview_ass", ass_path, event_sink)
    preview_path = burn_subtitles(
        media_path,
        ass_path,
        output_path,
        hardware_encoding=hardware_encoding,
        video_bitrate=recommended_video_bitrate(media_info.height),
        duration_ms=end_ms - start_ms,
        start_ms=start_ms,
        clip_duration_ms=end_ms - start_ms,
        progress_event_type=EventType.PREVIEW_PROGRESS,
        event_sink=event_sink,
    )
    preview_info = probe_media(preview_path)
    issues = []
    if not preview_info.has_video:
        issues.append("preview has no video stream")
    if media_info.has_audio and not preview_info.has_audio:
        issues.append("preview has no audio stream")
    elif preview_info.has_audio and preview_info.audio_codec != "aac":
        issues.append("preview audio codec is not AAC")
    if (preview_info.width, preview_info.height) != (media_info.width, media_info.height):
        issues.append("preview dimensions differ from source video")
    if abs(preview_info.duration_ms - (end_ms - start_ms)) > 500:
        issues.append("preview duration differs by more than 500ms")
    if issues:
        raise RuntimeError(f"preview video failed media QA: {', '.join(issues)}")
    add_artifact(job, "preview_video", preview_path, event_sink)
    if event_sink:
        event_sink(
            create_event(
                EventType.PREVIEW_COMPLETED,
                **job_event_fields(job.id, get_job_file(job)),
                path=str(preview_path),
                start_ms=start_ms,
                duration_ms=end_ms - start_ms,
            )
        )
    return preview_path


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
    persist_workflow_qa: bool = False,
    event_sink: EventSink | None = None,
) -> Path:
    job = load_job(job_file)
    if job.config.get("input_kind") == "subtitle":
        raise ValueError("burn-in requires media input")
    # QA 门必须先于远程媒体下载，避免为一个注定被拒的 Job 白下载整段视频。
    translated_cues_path = _assert_translated_qa_gate(job, action="burn-in")
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
                    add_artifact(job, "source_video", media_path, event_sink)
                    record_media_download_selection(job, media_path, event_sink)
            else:
                media_path = Path(job.input).expanduser()
        if not media_path.is_file():
            raise ValueError(f"video file does not exist: {media_path}")
        media_path = media_path.resolve()
        media_info = probe_media(media_path)
        if not media_info.has_video or not media_info.width or not media_info.height:
            raise ValueError("burn-in requires a video stream with known dimensions")

        translated = load_cues(translated_cues_path)
        _check_render_layout(
            job,
            translated,
            media_info,
            AssStyleOptions(target_font=target_font, source_font=source_font),
            event_sink,
            persist=persist_workflow_qa,
        )
        # 路径校验放在 RENDER/BURN 记账之前，避免错误输出先留下半成品 stage 状态。
        artifacts_directory = get_job_directory(job) / "artifacts"
        ass_path = artifacts_directory / "bilingual.ass"
        output_path = resolve_absolute_output(
            output if output else artifacts_directory / "burned.mp4",
            label="burn output",
        )
        assert_safe_output(
            output_path,
            protected=protected_job_paths(
                job,
                media_path,
                writing_kind="burned_video",
                extra=(ass_path,),
            ),
            label="burn output",
        )

        set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
        ass_path = save_bilingual_ass(
            translated,
            ass_path,
            video_width=media_info.width,
            video_height=media_info.height,
            options=AssStyleOptions(target_font=target_font, source_font=source_font),
        )
        add_artifact(job, "bilingual_ass", ass_path, event_sink)
        set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)

        current_stage = StageName.BURN
        set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
        burned_path = burn_subtitles(
            media_path,
            ass_path,
            output_path,
            hardware_encoding=hardware_encoding,
            video_bitrate=recommended_video_bitrate(media_info.height),
            duration_ms=media_info.duration_ms,
            event_sink=event_sink,
        )
        add_artifact(job, "burned_video", burned_path, event_sink)
        set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)

        current_stage = StageName.MEDIA_QA
        set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
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
        write_json(report_path, media_report)
        add_artifact(job, "media_qa", report_path, event_sink)
        if media_report["status"] == "failed":
            raise RuntimeError("burned video failed media QA")
        set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)
        return burned_path
    except UserInputError:
        # 输出路径冲突属于用户输入错误，不把当前 stage 记成 failed。
        raise
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

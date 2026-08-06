"""翻译主链：批次构建、兼容 API 调用、断点进度、Agent 回填、导入译文与统一收尾。

`finalize_translation` 是三条译文回填路径（局部重译、Agent/API 响应、导入译文
字幕）共用的收尾：写译文 cue → 失效旧下游产物 → 译文 QA → 渲染。
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from xvt.artifacts import ArtifactKind, downstream_of
from xvt.config import DEFAULT_BATCH_SIZE, DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT_SECONDS
from xvt.events import EventSink, EventType, create_event, job_event_fields
from xvt.job_store import get_job_directory, get_job_file, load_job, save_job
from xvt.models import Cue, Job, StageName, StageStatus
from xvt.pipeline.common import (
    add_artifact,
    artifact_path,
    checksum,
    error_category,
    invalidate_preview_artifacts,
    load_cues,
    set_stage,
    write_json,
)
from xvt.pipeline.review import complete_qa_stage
from xvt.process import raise_if_cancelled
from xvt.qa import check_subtitles
from xvt.recovery import valid_artifact_path
from xvt.render import save_bilingual_ass
from xvt.subtitles import load_subtitles, save_subtitles
from xvt.translate import (
    SEMANTIC_GROUPING_VERSION,
    TRANSLATE_AND_POLISH_TASK_VERSION,
    PolishBatch,
    SemanticRepairBatch,
    SemanticReviewBatch,
    TranslationBatch,
    TranslationPolicy,
    agent_translation_manifest_fingerprint,
    apply_translations,
    build_translation_batches,
    load_translation_policy,
    map_subtitle_translations,
    translation_policy_from_dict,
)
from xvt.translation_provider import (
    TRANSLATION_PROMPT_VERSION,
    CompatibleProviderConfig,
    ProviderResponseError,
    TranslationProviderError,
    resolve_provider,
    translate_batch,
)
from xvt.workflow_config import (
    clear_burn_fingerprint,
    get_workflow_config,
    resolve_translation_quality,
)

_RESPONSE_CORRECTION = (
    "The previous response was invalid. Return every requested id "
    "exactly once with non-empty translated text."
)


@dataclass(frozen=True, slots=True)
class TranslationRunResult:
    mode: str
    paths: tuple[Path, ...]
    fallback_reason: str | None = None


def translate_batch_with_retries(
    batch: TranslationBatch | PolishBatch | SemanticReviewBatch | SemanticRepairBatch,
    provider_config: CompatibleProviderConfig,
    *,
    max_retries: int,
    on_retry: Callable[[int, float, str], None] | None = None,
    request: Callable[..., Any] | None = None,
    response_correction: str = _RESPONSE_CORRECTION,
) -> Any:
    transport_retries = 0
    response_retries = 0
    request_attempt = 1
    correction: str | None = None
    request_batch = request or translate_batch
    while True:
        raise_if_cancelled()
        try:
            return request_batch(batch, provider_config, correction=correction)
        except TranslationProviderError as error:
            if not error.retryable or transport_retries >= max_retries:
                raise
            delay = 30 if transport_retries >= 5 else 2**transport_retries
            if (
                error.retry_after is not None
                and math.isfinite(error.retry_after)
                and error.retry_after >= 0
            ):
                delay = min(error.retry_after, 30)
            transport_retries += 1
            request_attempt += 1
            correction = None
            if on_retry:
                on_retry(request_attempt, delay, "provider temporarily unavailable")
            time.sleep(delay)
            raise_if_cancelled()
        except ProviderResponseError:
            if response_retries >= 1:
                raise
            delay = 0.5
            response_retries += 1
            request_attempt += 1
            correction = response_correction
            if on_retry:
                on_retry(request_attempt, delay, "invalid provider response")
            time.sleep(delay)
            raise_if_cancelled()


def _required_glossary_terms(job: Job) -> tuple[tuple[str, str], ...]:
    policy = translation_policy_from_dict(job.config.get("translation_policy"))
    return tuple((term.source, term.target) for term in policy.terms)


def load_translation_response(response_file: str | Path, expected_ids: set[int]) -> dict[int, str]:
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
    if set(translations) != expected_ids:
        raise ValueError("translation response must cover the requested cue ids exactly once")
    return translations


def _requested_translation_quality(job: Job, requested: str | None) -> str:
    current = resolve_translation_quality(job)
    workflow = get_workflow_config(job)
    if workflow is not None:
        if requested is not None and requested != current:
            raise ValueError("translation quality must match the persisted workflow goal")
        return current
    if requested is None:
        return current
    if requested not in {"standard", "polished"}:
        raise ValueError("translation quality must be standard or polished")
    if "translation_quality" in job.config and requested != current:
        raise ValueError("translation quality must match the persisted atomic job goal")
    return requested


def _translation_fingerprint(
    *,
    source_cues_checksum: str,
    source_language: str,
    target_language: str,
    provider: str,
    base_url: str,
    model: str,
    batch_size: int,
    policy: TranslationPolicy,
) -> str:
    payload = {
        "source_cues_checksum": source_cues_checksum,
        "source_language": source_language,
        "target_language": target_language,
        "provider": provider,
        "base_url": base_url,
        "model": model,
        "batch_size": batch_size,
        "prompt_version": TRANSLATION_PROMPT_VERSION,
        "semantic_grouping_version": SEMANTIC_GROUPING_VERSION,
        "translation_policy": policy.to_dict(),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _atomic_draft_matches_translation_progress(
    job: Job,
    cues: list[Cue],
    batches: list[TranslationBatch],
    fingerprint: str,
    target_language: str,
) -> Path | None:
    draft_path = valid_artifact_path(job, "translation_draft_cues")
    progress_path = valid_artifact_path(job, "translation_progress")
    if draft_path is None or progress_path is None:
        return None
    try:
        data = json.loads(progress_path.read_text(encoding="utf-8"))
        if (
            not isinstance(data, dict)
            or data.get("fingerprint") != fingerprint
            or data.get("completed_batches") != [batch.id for batch in batches]
        ):
            return None
        expected_ids = {item.id for batch in batches for item in batch.items}
        items = data.get("items")
        if not isinstance(items, list):
            return None
        translations = load_translation_response(progress_path, expected_ids)
        expected = apply_translations(cues, translations, target_language=target_language)
        return draft_path if load_cues(draft_path) == expected else None
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _translation_manifest(job: Job) -> dict[str, Any] | None:
    path = valid_artifact_path(job, "translation_batches")
    if path is None:
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return manifest if isinstance(manifest, dict) else None


def _agent_polished_manifest(job: Job) -> dict[str, Any] | None:
    manifest = _translation_manifest(job)
    if manifest is None or manifest.get("task") != "translate_and_polish":
        return None
    source_path = valid_artifact_path(job, "source_cues")
    workflow = get_workflow_config(job)
    target_language = (
        workflow.target_language if workflow is not None else job.config.get("target_language")
    )
    policy = (
        workflow.translation_policy.to_dict()
        if workflow is not None and workflow.translation_policy is not None
        else translation_policy_from_dict(job.config.get("translation_policy")).to_dict()
    )
    source_language = manifest.get("source_language")
    batch_size = manifest.get("batch_size")
    fingerprint = manifest.get("fingerprint")
    if (
        source_path is None
        or manifest.get("job_id") != job.id
        or manifest.get("source_cues_checksum") != checksum(source_path)
        or manifest.get("target_language") != target_language
        or manifest.get("policy") != policy
        or not isinstance(source_language, str)
        or not source_language
        or not isinstance(batch_size, int)
        or isinstance(batch_size, bool)
        or batch_size < 1
        or manifest.get("task_version") != TRANSLATE_AND_POLISH_TASK_VERSION
        or manifest.get("prompt_version") != TRANSLATION_PROMPT_VERSION
        or manifest.get("semantic_grouping_version") != SEMANTIC_GROUPING_VERSION
        or not isinstance(fingerprint, str)
        or fingerprint != agent_translation_manifest_fingerprint(manifest)
    ):
        return None
    expected_batches = build_translation_batches(
        load_cues(source_path),
        source_language=source_language,
        target_language=target_language,
        batch_size=batch_size,
        policy=translation_policy_from_dict(policy),
    )
    expected_payload = json.loads(
        json.dumps([batch.to_dict() for batch in expected_batches], ensure_ascii=False)
    )
    if manifest.get("batches") != expected_payload:
        return None
    return manifest


def _manifest_batch_ids_and_cue_ids(manifest: dict[str, Any]) -> tuple[list[int], set[int]]:
    batches = manifest.get("batches")
    if not isinstance(batches, list):
        raise ValueError("translation batch manifest must contain a batches array")
    batch_ids: list[int] = []
    cue_ids: set[int] = set()
    for batch in batches:
        batch_id = batch.get("id") if isinstance(batch, dict) else None
        items = batch.get("items") if isinstance(batch, dict) else None
        if (
            not isinstance(batch_id, int)
            or isinstance(batch_id, bool)
            or batch_id in batch_ids
            or not isinstance(items, list)
        ):
            raise ValueError("translation batch manifest contains an invalid batch")
        batch_ids.append(batch_id)
        for item in items:
            cue_id = item.get("id") if isinstance(item, dict) else None
            if not isinstance(cue_id, int) or isinstance(cue_id, bool) or cue_id in cue_ids:
                raise ValueError("translation batch manifest contains an invalid cue id")
            cue_ids.add(cue_id)
    return batch_ids, cue_ids


def _load_agent_polished_response(
    response_file: str | Path, manifest: dict[str, Any]
) -> dict[int, str]:
    _batch_ids, cue_ids = _manifest_batch_ids_and_cue_ids(manifest)
    response = json.loads(Path(response_file).read_text(encoding="utf-8"))
    if not isinstance(response, dict) or response.get("fingerprint") != manifest.get("fingerprint"):
        raise ValueError("translation response fingerprint does not match the current manifest")
    items = response.get("items")
    if not isinstance(items, list):
        raise ValueError("translation response must contain an items array")
    translations: dict[int, str] = {}
    for item in items:
        cue_id = item.get("id") if isinstance(item, dict) else None
        text = item.get("text") if isinstance(item, dict) else None
        if not isinstance(cue_id, int) or isinstance(cue_id, bool) or cue_id in translations:
            raise ValueError("translation response contains an invalid or duplicate cue id")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"translation for cue {cue_id} is empty")
        translations[cue_id] = text.strip()
    if set(translations) != cue_ids:
        raise ValueError("translation response must cover the requested cue ids exactly once")
    return translations


def _agent_polish_progress(
    job: Job, manifest: dict[str, Any]
) -> tuple[list[int], dict[int, str]] | None:
    path = valid_artifact_path(job, "translation_polish_progress")
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        batch_ids, cue_ids = _manifest_batch_ids_and_cue_ids(manifest)
        if (
            not isinstance(data, dict)
            or data.get("fingerprint") != manifest.get("fingerprint")
            or data.get("completed_batches") != batch_ids
        ):
            return None
        items = data.get("items")
        if not isinstance(items, list):
            return None
        translations: dict[int, str] = {}
        for item in items:
            cue_id = item.get("id") if isinstance(item, dict) else None
            text = item.get("text") if isinstance(item, dict) else None
            if (
                not isinstance(cue_id, int)
                or isinstance(cue_id, bool)
                or cue_id in translations
                or not isinstance(text, str)
                or not text.strip()
            ):
                return None
            translations[cue_id] = text.strip()
        if set(translations) != cue_ids:
            return None
        return batch_ids, translations
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None


def agent_translation_polish_waiting(job: Job) -> bool:
    manifest = _agent_polished_manifest(job)
    return manifest is not None and _agent_polish_progress(job, manifest) is None


def agent_translation_polish_started(job: Job) -> bool:
    artifact = next((item for item in job.artifacts if item.kind == "translation_batches"), None)
    data = None
    if artifact is not None:
        try:
            data = json.loads(Path(artifact.path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
    if isinstance(data, dict) and data.get("task") == "translate_and_polish":
        return True
    kinds = {item.kind for item in job.artifacts}
    return (
        "translation_draft_cues" not in kinds
        and "translation_polish_batches" not in kinds
        and (
            job.stages[StageName.TRANSLATE].status is StageStatus.AWAITING_INPUT
            or bool({"translation_polish_progress", "translation_polished_cues"} & kinds)
        )
    )


def agent_translation_polish_progress_ready(job: Job) -> bool:
    manifest = _agent_polished_manifest(job)
    return manifest is not None and _agent_polish_progress(job, manifest) is not None


def _agent_polished_cues(job: Job) -> list[Cue]:
    manifest = _agent_polished_manifest(job)
    if manifest is None:
        raise ValueError("job has no valid translate_and_polish manifest")
    progress = _agent_polish_progress(job, manifest)
    if progress is None:
        raise ValueError("job has no complete translate_and_polish response")
    source_path = valid_artifact_path(job, "source_cues")
    if source_path is None:
        raise ValueError("translate_and_polish requires checksum-valid source_cues")
    target_language = manifest.get("target_language")
    if not isinstance(target_language, str) or not target_language:
        raise ValueError("translate_and_polish manifest has no target language")
    return apply_translations(
        load_cues(source_path),
        progress[1],
        target_language=target_language,
    )


def agent_translation_polish_core_satisfied(job: Job) -> bool:
    try:
        expected = _agent_polished_cues(job)
        polished_path = valid_artifact_path(job, "translation_polished_cues")
        return polished_path is not None and load_cues(polished_path) == expected
    except (OSError, TypeError, ValueError):
        return False


def complete_agent_polished_outputs(
    job_file: str | Path,
    *,
    event_sink: EventSink | None = None,
) -> tuple[Path, ...]:
    job = load_job(job_file)
    expected = _agent_polished_cues(job)
    artifacts_directory = get_job_directory(job) / "artifacts"
    polished_path = valid_artifact_path(job, "translation_polished_cues")
    if polished_path is None or load_cues(polished_path) != expected:
        invalidated = downstream_of(ArtifactKind.TRANSLATION_POLISHED_CUES)
        job.artifacts = [artifact for artifact in job.artifacts if artifact.kind not in invalidated]
        clear_burn_fingerprint(job)
        for stage in (StageName.SUBTITLE_QA, StageName.RENDER, StageName.BURN, StageName.MEDIA_QA):
            record = job.stages[stage]
            record.status = StageStatus.PENDING
            record.started_at = None
            record.completed_at = None
            record.error = None
        polished_path = artifacts_directory / "translation-polished-cues.json"
        write_json(polished_path, [asdict(cue) for cue in expected])
        add_artifact(job, "translation_polished_cues", polished_path, event_sink)
    set_stage(
        job,
        StageName.TRANSLATE,
        StageStatus.SUCCEEDED,
        event_sink,
        extras={"phase": "polish"},
    )
    return (polished_path,)


def finalize_translation(
    job: Job,
    translated: list[Cue],
    event_sink: EventSink | None,
    *,
    invalidate: Callable[[Job], None],
    polished_result: bool = False,
) -> tuple[Path, ...]:
    """翻译成功后的统一收尾：写译文 cue → 失效旧下游产物 → 译文 QA → 渲染。

    三条译文回填路径（局部重译、Agent/API 响应、导入译文字幕）共用本函数，
    保证 stage 记账、QA 参数与渲染行为完全一致；QA/渲染阶段的失败在此持久化。
    """
    current_stage = StageName.TRANSLATE
    try:
        artifacts_directory = get_job_directory(job) / "artifacts"
        if resolve_translation_quality(job) == "polished" and not polished_result:
            draft_path = artifacts_directory / "translation-draft-cues.json"
            write_json(draft_path, [asdict(cue) for cue in translated])
            add_artifact(job, "translation_draft_cues", draft_path, event_sink)
            invalidated = downstream_of(ArtifactKind.TRANSLATION_DRAFT_CUES)
            job.artifacts = [
                artifact for artifact in job.artifacts if artifact.kind not in invalidated
            ]
            clear_burn_fingerprint(job)
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
            return (draft_path,)
        translated_cues_path = artifacts_directory / "translated-cues.json"
        write_json(translated_cues_path, [asdict(cue) for cue in translated])
        invalidate(job)
        add_artifact(job, "translated_cues", translated_cues_path, event_sink)
        set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)
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
    # QA/渲染失败由 _qa_and_render_translation 记账，勿回写 TRANSLATE=failed。
    return _qa_and_render_translation(job, translated, event_sink)


def complete_translated_outputs(
    job_file: str | Path,
    *,
    event_sink: EventSink | None = None,
) -> tuple[Path, ...]:
    """从已有 ``translated_cues`` 补跑译文 QA 与渲染，不调用翻译 API、不重写 cue。

    用于 ``needs_review`` 确认后的 continue：cues 已落盘但 SRT/ASS 尚未写出的断点。
    """
    job = load_job(job_file)
    translated = load_cues(artifact_path(job, "translated_cues"))
    return _qa_and_render_translation(job, translated, event_sink)


def _qa_and_render_translation(
    job: Job,
    translated: list[Cue],
    event_sink: EventSink | None,
) -> tuple[Path, ...]:
    """译文 QA → 渲染；失败时持久化当前 stage。"""
    current_stage = StageName.SUBTITLE_QA
    try:
        artifacts_directory = get_job_directory(job) / "artifacts"
        set_stage(
            job,
            current_stage,
            StageStatus.RUNNING,
            event_sink,
            extras={"detail": "translated"},
        )
        media_data = _media_data(job)
        report = check_subtitles(
            translated,
            media_duration_ms=_media_duration_ms(media_data),
            video_width=media_data.get("width"),
            video_height=media_data.get("height"),
            require_translation=True,
            required_terms=_required_glossary_terms(job),
        )
        qa_path = artifacts_directory / "qa-translated.json"
        write_json(qa_path, asdict(report))
        add_artifact(job, "translated_subtitle_qa", qa_path, event_sink)
        complete_qa_stage(job, report, event_sink)

        current_stage = StageName.RENDER
        set_stage(
            job,
            current_stage,
            StageStatus.RUNNING,
            event_sink,
            extras={"detail": "translated"},
        )
        paths = _render_translated_outputs(job, translated, media_data, event_sink)
        set_stage(job, current_stage, StageStatus.SUCCEEDED, event_sink)
        return paths
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


def _media_data(job: Job) -> dict[str, Any]:
    if job.config.get("input_kind") == "subtitle":
        return {}
    media_path = artifact_path(job, "media_info")
    return json.loads(media_path.read_text(encoding="utf-8"))


def _media_duration_ms(media_data: dict[str, Any]) -> int | None:
    value = media_data.get("duration_ms")
    return int(value) if value is not None else None


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
    add_artifact(job, "translated_srt", translated_path, event_sink)
    add_artifact(job, "bilingual_srt", bilingual_path, event_sink)
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
        add_artifact(job, "bilingual_ass", ass_path, event_sink)
        paths.append(ass_path)
    return tuple(paths)


def prepare_agent_translation(
    job_file: str | Path,
    *,
    source_language: str,
    target_language: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    policy: TranslationPolicy | None = None,
    glossary: str | Path | None = None,
    translation_style: str = "faithful",
    translation_quality: str | None = None,
    await_input: bool = True,
    event_sink: EventSink | None = None,
) -> Path:
    job = load_job(job_file)
    workflow = get_workflow_config(job)
    quality = _requested_translation_quality(job, translation_quality)
    if workflow is not None and workflow.target_language != target_language:
        message = (
            "target language must match the polished workflow goal"
            if quality == "polished"
            else "target language must match the persisted workflow goal"
        )
        raise ValueError(message)
    if workflow is None and job.config.get("target_language") not in {None, target_language}:
        raise ValueError("target language must match the persisted atomic job goal")
    resolved_policy = policy or load_translation_policy(glossary, style=translation_style)
    source_path = valid_artifact_path(job, "source_cues")
    if source_path is None:
        raise ValueError("translation requires checksum-valid source_cues")
    cues = load_cues(source_path)
    batches = build_translation_batches(
        cues,
        source_language=source_language,
        target_language=target_language,
        batch_size=batch_size,
        policy=resolved_policy,
    )
    set_stage(job, StageName.TRANSLATE, StageStatus.RUNNING, event_sink)
    try:
        output_path = get_job_directory(job) / "artifacts" / "translation-batches.json"
        manifest: dict[str, Any] = {
            "job_id": job.id,
            "source_language": source_language,
            "target_language": target_language,
            "prompt_version": TRANSLATION_PROMPT_VERSION,
            "semantic_grouping_version": SEMANTIC_GROUPING_VERSION,
            "policy": resolved_policy.to_dict(),
            "batches": [batch.to_dict() for batch in batches],
        }
        if quality == "polished" and await_input:
            manifest.update(
                {
                    "batch_size": batch_size,
                    "source_cues_checksum": checksum(source_path),
                    "task": "translate_and_polish",
                    "task_version": TRANSLATE_AND_POLISH_TASK_VERSION,
                }
            )
            manifest["fingerprint"] = agent_translation_manifest_fingerprint(manifest)
        write_json(
            output_path,
            manifest,
        )
        add_artifact(job, "translation_batches", output_path, event_sink)
        job.config["target_language"] = target_language
        job.config["source_language"] = source_language
        job.config["translation_policy"] = resolved_policy.to_dict()
        if workflow is None:
            job.config["translation_quality"] = quality
            save_job(job)
        if await_input:
            set_stage(job, StageName.TRANSLATE, StageStatus.AWAITING_INPUT, event_sink)
        return output_path
    except Exception as error:
        set_stage(
            job,
            StageName.TRANSLATE,
            StageStatus.FAILED,
            event_sink,
            error=str(error),
            error_category=error_category(error),
        )
        raise


def run_translation(
    job_file: str | Path,
    *,
    target_language: str,
    provider: str = "auto",
    base_url: str | None = None,
    model: str | None = None,
    api_key_env: str = "XVT_TRANSLATION_API_KEY",
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_retries: int = DEFAULT_MAX_RETRIES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    policy: TranslationPolicy | None = None,
    glossary: str | Path | None = None,
    translation_style: str = "faithful",
    translation_quality: str | None = None,
    event_sink: EventSink | None = None,
) -> TranslationRunResult:
    if max_retries < 0:
        raise ValueError("max_retries must not be negative")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    job = load_job(job_file)
    workflow = get_workflow_config(job)
    quality = _requested_translation_quality(job, translation_quality)
    if workflow is not None and workflow.target_language != target_language:
        message = (
            "target language must match the polished workflow goal"
            if quality == "polished"
            else "target language must match the persisted workflow goal"
        )
        raise ValueError(message)
    if workflow is None and job.config.get("target_language") not in {None, target_language}:
        raise ValueError("target language must match the persisted atomic job goal")
    resolved_policy = policy or load_translation_policy(glossary, style=translation_style)
    source_path = valid_artifact_path(job, "source_cues")
    if source_path is None:
        raise ValueError("translation requires checksum-valid source_cues")
    cues = load_cues(source_path)
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
    batches: list[TranslationBatch] | None = None
    translation_fingerprint: str | None = None
    if provider_config is not None:
        batches = build_translation_batches(
            cues,
            source_language=source_language,
            target_language=target_language,
            batch_size=batch_size,
            policy=resolved_policy,
        )
        translation_fingerprint = _translation_fingerprint(
            source_cues_checksum=checksum(source_path),
            source_language=source_language,
            target_language=target_language,
            provider=mode,
            base_url=provider_config.base_url,
            model=provider_config.model,
            batch_size=batch_size,
            policy=resolved_policy,
        )
        if workflow is None and quality == "polished":
            draft_path = _atomic_draft_matches_translation_progress(
                job,
                cues,
                batches,
                translation_fingerprint,
                target_language,
            )
            if draft_path is not None:
                return TranslationRunResult("draft_reused", (draft_path,))
    # API 路径准备批次时不得进入 awaiting_input，避免人类可读进度误报「等待」。
    batches_path = prepare_agent_translation(
        job_file,
        source_language=source_language,
        target_language=target_language,
        batch_size=batch_size,
        policy=resolved_policy,
        translation_quality=quality,
        await_input=(mode == "agent"),
        event_sink=event_sink,
    )
    if mode == "agent":
        if fallback_reason and event_sink:
            event_sink(
                create_event(
                    EventType.TRANSLATION_FALLBACK,
                    **job_event_fields(job.id, get_job_file(job)),
                    mode="agent",
                    reason=fallback_reason,
                    path=str(batches_path),
                )
            )
        return TranslationRunResult("agent", (batches_path,), fallback_reason)

    if provider_config is None:
        raise RuntimeError("translation provider configuration was not resolved")
    job = load_job(job_file)
    if batches is None or translation_fingerprint is None:
        raise RuntimeError("translation batches were not resolved")
    set_stage(job, StageName.TRANSLATE, StageStatus.RUNNING, event_sink)
    if event_sink:
        event_sink(
            create_event(
                EventType.TRANSLATION_PROVIDER_SELECTED,
                **job_event_fields(job.id, get_job_file(job)),
                **({"phase": "draft"} if quality == "polished" else {}),
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
                    EventType.TRANSLATION_PROGRESS_RESET,
                    **job_event_fields(job.id, get_job_file(job)),
                    **({"phase": "draft"} if quality == "polished" else {}),
                    reason="translation fingerprint changed",
                )
            )

    artifacts_directory = get_job_directory(job) / "artifacts"
    progress_output = artifacts_directory / "translation-progress.json"
    batch_total = len(batches)
    try:
        for batch in batches:
            raise_if_cancelled()
            batch_ids = {item.id for item in batch.items}
            if batch.id in completed_batches and batch_ids <= translations.keys():
                continue
            if event_sink:
                event_sink(
                    create_event(
                        EventType.TRANSLATION_BATCH_STARTED,
                        **job_event_fields(job.id, get_job_file(job)),
                        **({"phase": "draft"} if quality == "polished" else {}),
                        batch=batch.id,
                        batch_total=batch_total,
                    )
                )

            def emit_retry(attempt: int, delay: float, reason: str) -> None:
                if event_sink:
                    event_sink(
                        create_event(
                            EventType.TRANSLATION_BATCH_RETRYING,
                            **job_event_fields(job.id, get_job_file(job)),
                            **({"phase": "draft"} if quality == "polished" else {}),
                            batch=batch.id,
                            batch_total=batch_total,
                            attempt=attempt,
                            delay_seconds=delay,
                            reason=reason,
                        )
                    )

            translated_batch = translate_batch_with_retries(
                batch,
                provider_config,
                max_retries=max_retries,
                on_retry=emit_retry,
            )
            translations.update(translated_batch)
            completed_batches.add(batch.id)
            write_json(
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
            add_artifact(job, "translation_progress", progress_output, event_sink)
            if event_sink:
                event_sink(
                    create_event(
                        EventType.TRANSLATION_BATCH_COMPLETED,
                        **job_event_fields(job.id, get_job_file(job)),
                        **({"phase": "draft"} if quality == "polished" else {}),
                        batch=batch.id,
                        batch_total=batch_total,
                    )
                )
    except Exception as error:
        set_stage(
            job,
            StageName.TRANSLATE,
            StageStatus.FAILED,
            event_sink,
            error=str(error),
            error_category=error_category(error),
        )
        raise

    response_path = artifacts_directory / "translation-response.json"
    write_json(
        response_path,
        {"items": [{"id": cue_id, "text": text} for cue_id, text in sorted(translations.items())]},
    )
    add_artifact(job, "translation_response", response_path, event_sink)
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
    workflow = get_workflow_config(job)
    quality = resolve_translation_quality(job)
    if workflow is not None and workflow.target_language != target_language:
        message = (
            "target language must match the polished workflow goal"
            if quality == "polished"
            else "target language must match the persisted workflow goal"
        )
        raise ValueError(message)
    if workflow is None and job.config.get("target_language") not in (
        {target_language} if quality == "polished" else {None, target_language}
    ):
        raise ValueError("target language must match the persisted atomic job goal")
    manifest = _translation_manifest(job)
    task = manifest.get("task") if manifest is not None else None
    if task not in {None, "translate_and_polish"}:
        raise ValueError(f"unsupported translation task: {task}")
    if task == "translate_and_polish":
        polished_manifest = _agent_polished_manifest(job)
        if polished_manifest is None:
            raise ValueError("translate_and_polish manifest is invalid or stale")
        if quality != "polished" or polished_manifest.get("target_language") != target_language:
            raise ValueError("translation response does not match the polished job goal")
        translations = _load_agent_polished_response(response_file, polished_manifest)
        if agent_translation_polish_core_satisfied(job):
            polished_path = valid_artifact_path(job, "translation_polished_cues")
            assert polished_path is not None
            return (polished_path,)
        current_progress = _agent_polish_progress(job, polished_manifest)
        if current_progress is None:
            batch_ids, _cue_ids = _manifest_batch_ids_and_cue_ids(polished_manifest)
            progress_path = (
                get_job_directory(job) / "artifacts" / "translation-polish-progress.json"
            )
            write_json(
                progress_path,
                {
                    "fingerprint": polished_manifest["fingerprint"],
                    "completed_batches": batch_ids,
                    "items": [
                        {"id": cue_id, "text": text}
                        for cue_id, text in sorted(translations.items())
                    ],
                },
            )
            add_artifact(job, "translation_polish_progress", progress_path, event_sink)
        elif current_progress[1] != translations:
            raise ValueError("translation response differs from completed polished progress")
        set_stage(
            job,
            StageName.TRANSLATE,
            StageStatus.RUNNING,
            event_sink,
            extras={"phase": "polish"},
        )
        try:
            return complete_agent_polished_outputs(job_file, event_sink=event_sink)
        except Exception as error:
            set_stage(
                job,
                StageName.TRANSLATE,
                StageStatus.FAILED,
                event_sink,
                error=str(error),
                error_category=error_category(error),
                extras={"phase": "polish"},
            )
            raise
    current_stage = StageName.TRANSLATE
    set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
    try:
        cues = load_cues(artifact_path(job, "source_cues"))
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
    return finalize_translation(
        job, translated, event_sink, invalidate=invalidate_preview_artifacts
    )


def apply_subtitle_translation(
    job_file: str | Path,
    subtitle_file: str | Path,
    *,
    target_language: str,
    event_sink: EventSink | None = None,
) -> tuple[Path, ...]:
    job = load_job(job_file)
    workflow = get_workflow_config(job)
    quality = resolve_translation_quality(job)
    if workflow is not None and workflow.target_language != target_language:
        message = (
            "target language must match the polished workflow goal"
            if quality == "polished"
            else "target language must match the persisted workflow goal"
        )
        raise ValueError(message)
    if workflow is None and job.config.get("target_language") not in {None, target_language}:
        raise ValueError("target language must match the persisted atomic job goal")
    current_stage = StageName.TRANSLATE
    set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
    try:
        source_cues = load_cues(artifact_path(job, "source_cues"))
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
        if subtitle_path.resolve() != imported_path.resolve():
            shutil.copy2(subtitle_path, imported_path)
        add_artifact(job, "input_translation_subtitles", imported_path, event_sink)
        mapping_path = artifacts_directory / "translation-mapping.json"
        write_json(mapping_path, mapping_report)
        add_artifact(job, "translation_mapping", mapping_path, event_sink)
        job.config["target_language"] = target_language
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
    return finalize_translation(
        job, translated, event_sink, invalidate=invalidate_preview_artifacts
    )

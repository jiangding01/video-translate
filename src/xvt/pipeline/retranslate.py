"""指定 cue 的局部重译：只回填选中 ID，保留其余译文与全部时间轴。

每个目标 cue 携带相邻源 cue 作为只读上下文；回填后经 `finalize_translation`
完整重跑译文 QA，并使旧渲染、烧录与预览产物失效。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from xvt.config import DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT_SECONDS
from xvt.events import EventSink, EventType, create_event, job_event_fields
from xvt.job_store import get_job_directory, get_job_file, load_job
from xvt.models import Cue, Job, StageName, StageStatus
from xvt.pipeline.common import (
    add_artifact,
    artifact_path,
    checksum,
    error_category,
    invalidate_rendered_artifacts,
    load_cues,
    load_user_response_json,
    set_stage,
    write_json,
)
from xvt.pipeline.translation import (
    TranslationRunResult,
    finalize_translation,
    load_translation_response,
    resolved_translation_policy,
    translate_batch_with_retries,
)
from xvt.process import raise_if_cancelled
from xvt.recovery import file_checksum as readonly_file_checksum
from xvt.recovery import valid_artifact_path
from xvt.translate import (
    SEMANTIC_GROUPING_VERSION,
    TranslationBatch,
    agent_translation_manifest_fingerprint,
    build_cue_retranslation_batches,
    normalize_generated_subtitle_text,
)
from xvt.translation_provider import (
    TRANSLATION_PROMPT_VERSION,
    resolve_provider,
)

CUE_RETRANSLATION_TASK_VERSION = 1


def _load_cue_retranslation_manifest(job: Job) -> dict | None:
    """读出 checksum 有效的重译 manifest；不可读或不是对象时返回 None。

    只读，不写任何产物，因此可以安全地在 ``build_recovery_plan`` 的只读判定链里调用。
    """

    manifest_path = valid_artifact_path(job, "cue_retranslation_batches")
    if manifest_path is None:
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return manifest if isinstance(manifest, dict) else None


def cue_retranslation_waiting(job: Job) -> bool:
    """Job 是否真的停在一次仍然挂得住的原子重译等待上。

    严格四条件（缺一不可）：``TRANSLATE`` stage 在等输入；重译 manifest checksum 有效；
    当前 ``translated_cues`` checksum 有效；manifest 记录的 source / translated cue
    checksum 与当前逐一相等。

    后两条不能省。``cue_retranslation_batches`` 在 artifact 依赖图上只派生自
    ``source_cues``，译文被改写时它不会被失效，因此「重译已回填、manifest 只是残留」
    的旧文件仍然 checksum 有效；只看 stage + manifest 有效会把随后的润色/语义等待
    错误地盖成重译等待。真正挡住这种误判的是条件一与条件四：``apply-cue-retranslation``
    走 ``finalize_translation``，standard 质量下把 ``translated_cues`` **重写**并重新登记
    （条件四的 checksum 随之失配）、``TRANSLATE`` 置 succeeded（条件一为假）；polished
    质量下结果被写成新草稿，``translated_cues`` 被失效移出登记（条件三也为假）、
    ``TRANSLATE`` 回落 pending。三者任一都足以让谓词判否，缺一条就会误判。

    残留 manifest 还有一条不靠本谓词的出口：``prepare_agent_translation`` 在写出新的
    整轮 ``translation_batches`` 时会主动把它移出登记（整轮翻译取代局部重译）。

    checksum 走 ``recovery.file_checksum``：本函数是只读判定，在
    ``build_recovery_plan`` 的记忆化作用域内能复用同一次判定里已经算过的哈希；
    无作用域时与直连哈希逐字等价。
    """

    stage = job.stages.get(StageName.TRANSLATE)
    if stage is None or stage.status is not StageStatus.AWAITING_INPUT:
        return False
    manifest = _load_cue_retranslation_manifest(job)
    if manifest is None:
        return False
    source_path = valid_artifact_path(job, "source_cues")
    translated_path = valid_artifact_path(job, "translated_cues")
    if source_path is None or translated_path is None:
        return False
    return manifest.get("source_cues_checksum") == readonly_file_checksum(
        source_path
    ) and manifest.get("translated_cues_checksum") == readonly_file_checksum(translated_path)


def cue_retranslation_request(job: Job) -> tuple[tuple[int, ...], str] | None:
    """从有效 manifest 读出 ``(cue_ids, target_language)``，供恢复计划拼出可复制的回填命令。

    cue id 取 ``batches[].items[].id``：``build_cue_retranslation_batches`` 为每个目标 cue
    生成独立批次且 ``items`` 恰好一个元素，相邻上下文放在 ``context_before`` /
    ``context_after``，不会混进来。manifest 结构不符合预期时返回 None，由调用方退回
    形状占位，绝不猜测 cue id。

    **顺序必须与 manifest 内的批次顺序一致，不能排序**：``apply-cue-retranslation`` 会用
    传入的 ``--cue`` 顺序重新构造 manifest 并与盘上那份做全等比对，用户写
    ``--cue 3 --cue 1`` 时批次就是 3、1 的顺序；这里排成升序会让按恢复计划复制的命令必然
    比对失败。去重保留首次出现的位置（manifest 本身不含重复 id，这里只是防御）。
    """

    manifest = _load_cue_retranslation_manifest(job)
    if manifest is None:
        return None
    target_language = manifest.get("target_language")
    if not isinstance(target_language, str) or not target_language:
        return None
    batches = manifest.get("batches")
    if not isinstance(batches, list):
        return None
    cue_ids: list[int] = []
    for batch in batches:
        items = batch.get("items") if isinstance(batch, dict) else None
        if not isinstance(items, list):
            return None
        for item in items:
            if not isinstance(item, dict):
                return None
            cue_id = item.get("id")
            # bool 是 int 的子类；与项目其余解析器同口径显式排除。
            if isinstance(cue_id, bool) or not isinstance(cue_id, int):
                return None
            cue_ids.append(cue_id)
    if not cue_ids:
        return None
    return tuple(dict.fromkeys(cue_ids)), target_language


def _cue_retranslation_manifest(
    job: Job,
    cue_ids: tuple[int, ...],
    target_language: str,
) -> tuple[dict, list[Cue], list[TranslationBatch]]:
    if not cue_ids:
        raise ValueError("at least one cue id is required")
    if len(set(cue_ids)) != len(cue_ids):
        raise ValueError("cue ids must not contain duplicates")
    source_path = artifact_path(job, "source_cues")
    translated_path = artifact_path(job, "translated_cues")
    source_cues = load_cues(source_path)
    translated_cues = load_cues(translated_path)
    translated_by_id = {cue.id: cue for cue in translated_cues}
    if len(translated_by_id) != len(translated_cues):
        raise ValueError("translated_cues contains duplicate cue ids")
    expected_ids = set(cue_ids)
    missing = sorted(expected_ids - translated_by_id.keys())
    if missing:
        raise ValueError(f"translated_cues does not contain ids: {missing}")
    existing_languages = {cue.target_language for cue in translated_cues if cue.target_language}
    if existing_languages and existing_languages != {target_language}:
        raise ValueError("target language must match the existing translated cues")
    source_language = next(
        (cue.source_language for cue in source_cues if cue.source_language),
        job.config.get("source_language", "auto"),
    )
    # 与译文 QA 的术语校验共用同一解析口径（workflow 快照优先、顶层配置回退），
    # 避免端到端 Job 上重译 manifest 与 QA 判定读到两份不同的策略。
    policy = resolved_translation_policy(job)
    batches = build_cue_retranslation_batches(
        source_cues,
        cue_ids,
        source_language=source_language,
        target_language=target_language,
        policy=policy,
    )
    manifest = {
        "task": "retranslate_cues",
        "task_version": CUE_RETRANSLATION_TASK_VERSION,
        "job_id": job.id,
        "source_cues_checksum": checksum(source_path),
        "translated_cues_checksum": checksum(translated_path),
        "source_language": source_language,
        "target_language": target_language,
        "policy": policy.to_dict(),
        "prompt_version": TRANSLATION_PROMPT_VERSION,
        "semantic_grouping_version": SEMANTIC_GROUPING_VERSION,
        "batches": [batch.to_dict() for batch in batches],
    }
    manifest = json.loads(json.dumps(manifest, ensure_ascii=False))
    manifest["fingerprint"] = agent_translation_manifest_fingerprint(manifest)
    return manifest, translated_cues, batches


def apply_cue_retranslation(
    job_file: str | Path,
    response_file: str | Path,
    *,
    cue_ids: tuple[int, ...],
    target_language: str,
    event_sink: EventSink | None = None,
) -> tuple[Path, ...]:
    job = load_job(job_file)
    current_stage = StageName.TRANSLATE
    set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
    try:
        manifest, translated_cues, _batches = _cue_retranslation_manifest(
            job, cue_ids, target_language
        )
        batches_path = valid_artifact_path(job, "cue_retranslation_batches")
        if batches_path is None:
            raise ValueError("job has no valid cue retranslation manifest")
        stored_manifest = json.loads(batches_path.read_text(encoding="utf-8"))
        if stored_manifest != manifest:
            # 失配不等于「输入被改动」：manifest 里还压了翻译策略、prompt 版本与语义分组
            # 版本，升级 XVT 后同一份输入也会生成不同的 manifest。文案必须涵盖这条路径，
            # 否则用户会去核对一个字节都没变的输入。
            raise ValueError(
                "cue retranslation manifest does not match what this xvt version would "
                "generate (inputs or translation policy resolution may have changed, "
                "e.g. after an upgrade); rerun xvt retranslate-cues to regenerate the "
                "manifest and retranslate with the new batches"
            )
        expected_ids = set(cue_ids)
        response = load_user_response_json(response_file, what="cue retranslation response")
        if not isinstance(response, dict) or response.get("fingerprint") != manifest["fingerprint"]:
            raise ValueError("cue retranslation response fingerprint does not match current inputs")
        translations = load_translation_response(response_file, expected_ids)
        repaired = [
            replace(
                cue,
                target_text=normalize_generated_subtitle_text(
                    cue.source_text,
                    translations[cue.id],
                    target_language=target_language,
                ),
                target_language=target_language,
            )
            if cue.id in translations
            else cue
            for cue in translated_cues
        ]
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
    return finalize_translation(job, repaired, event_sink, invalidate=invalidate_rendered_artifacts)


def retranslate_cues(
    job_file: str | Path,
    *,
    cue_ids: tuple[int, ...],
    target_language: str,
    provider: str = "auto",
    base_url: str | None = None,
    model: str | None = None,
    api_key_env: str = "XVT_TRANSLATION_API_KEY",
    max_retries: int = DEFAULT_MAX_RETRIES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    event_sink: EventSink | None = None,
) -> TranslationRunResult:
    if max_retries < 0:
        raise ValueError("max_retries must not be negative")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    job = load_job(job_file)
    manifest, _translated_cues, batches = _cue_retranslation_manifest(job, cue_ids, target_language)
    mode, provider_config, fallback_reason = resolve_provider(
        provider,
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        timeout_seconds=timeout_seconds,
    )
    artifacts_directory = get_job_directory(job) / "artifacts"
    batches_path = artifacts_directory / "cue-retranslation-batches.json"
    write_json(batches_path, manifest)
    add_artifact(job, "cue_retranslation_batches", batches_path, event_sink)
    if mode == "agent":
        set_stage(job, StageName.TRANSLATE, StageStatus.AWAITING_INPUT, event_sink)
        return TranslationRunResult("agent", (batches_path,), fallback_reason)
    if provider_config is None:
        raise RuntimeError("translation provider configuration was not resolved")

    translations = {}
    set_stage(job, StageName.TRANSLATE, StageStatus.RUNNING, event_sink)
    batch_total = len(batches)
    try:
        for batch in batches:
            raise_if_cancelled()

            def emit_retry(attempt: int, delay: float, reason: str) -> None:
                if event_sink:
                    event_sink(
                        create_event(
                            EventType.TRANSLATION_BATCH_RETRYING,
                            **job_event_fields(job.id, get_job_file(job)),
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

    response_path = artifacts_directory / "cue-retranslation-response.json"
    write_json(
        response_path,
        {
            "fingerprint": manifest["fingerprint"],
            "items": [
                {"id": cue_id, "text": text} for cue_id, text in sorted(translations.items())
            ],
        },
    )
    add_artifact(job, "cue_retranslation_response", response_path, event_sink)
    paths = apply_cue_retranslation(
        job_file,
        response_path,
        cue_ids=cue_ids,
        target_language=target_language,
        event_sink=event_sink,
    )
    return TranslationRunResult(mode, (response_path, *paths))

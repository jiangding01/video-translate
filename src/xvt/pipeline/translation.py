"""翻译主链：批次构建、兼容 API 调用、断点进度、Agent 回填、导入译文与统一收尾。

`finalize_translation` 是三条译文回填路径（局部重译、Agent/API 响应、导入译文
字幕）共用的收尾：写译文 cue → 失效旧下游产物 → 译文 QA → 渲染。
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from xvt.artifacts import ArtifactKind, downstream_of
from xvt.config import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_TRANSLATION_STYLE,
)
from xvt.errors import UserInputError, XvtError
from xvt.events import EventSink, EventType, create_event, job_event_fields
from xvt.job_store import get_job_directory, get_job_file, load_job, save_job
from xvt.models import Cue, Job, StageName, StageStatus
from xvt.pipeline.common import (
    add_artifact,
    artifact_path,
    checksum,
    emit_agent_fallback,
    error_category,
    invalidate_rendered_artifacts,
    load_cues,
    load_user_response_json,
    parse_cue_text_items,
    resolved_translation_policy,
    set_stage,
    translation_qa_context,
    write_json,
)
from xvt.pipeline.review import complete_qa_stage
from xvt.process import raise_if_cancelled
from xvt.qa import check_subtitles
from xvt.recovery import valid_artifact_path
from xvt.render import AssStyleOptions, save_bilingual_ass
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
    WorkflowConfig,
    clear_burn_fingerprint,
    get_workflow_config,
    resolve_translation_quality,
    set_workflow_config,
)

_RESPONSE_CORRECTION = (
    "The previous response was invalid. Return every requested id "
    "exactly once with non-empty translated text."
)
# 传输退避的硬上限：指数基数与服务端 Retry-After 都经 min() 钳到这里，指数序列从
# 2**5=32 起一律按 30 秒计。
_MAX_BACKOFF_SECONDS = 30
# 结构性无效响应的纠错预算，独立于 --max-retries（传输预算）：最多 2 次纠错重试，
# 连同首次请求共 3 次。挂在 --max-retries 上会让「网络别重试」的用户连纠错也失去。
# 纯内存态，不进 fingerprint/checkpoint，旧 Job 恢复零影响。
_RESPONSE_RETRY_BUDGET = 2
# 坏响应重发几乎不存在惊群同步性，抖动无收益，固定短延迟即可。
_RESPONSE_RETRY_DELAY_SECONDS = 0.5


def _backoff_delay(base: float) -> float:
    """在 ``[base/2, base]`` 内取一次 equal jitter 退避秒数。

    equal jitter 既保留退避下界（不会退化成立刻重发），又不会把上界拖长。
    ``round(..., 2)`` 让 ``translation_batch_retrying`` 事件里的 ``delay_seconds``
    与实际 sleep 值完全一致，且人类进度渲染可读。

    边界：``random`` 在本模块只服务于这一处退避抖动。它的取值绝不能进入 fingerprint、
    checkpoint、artifact 内容或事件语义判定，否则同样的输入会产出不同的产物 checksum。
    """
    return round(random.uniform(base / 2, base), 2)


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
            if (
                error.retry_after is not None
                and math.isfinite(error.retry_after)
                and error.retry_after >= 0
            ):
                # 服务端显式指定了重试时刻，尊重原值不加抖动，只做上限钳位。
                # `_parse_retry_after` 已过滤掉非有限值与负值，这里再查一遍是针对
                # `retry_after` 这个公开字段的防御：它也可由 provider 之外的调用方构造。
                delay = round(min(error.retry_after, _MAX_BACKOFF_SECONDS), 2)
            else:
                delay = _backoff_delay(min(2**transport_retries, _MAX_BACKOFF_SECONDS))
            transport_retries += 1
            request_attempt += 1
            # 传输失败意味着请求没有被服务端消费，上一轮的纠正提示词还没有起效，
            # 因此原样保留 correction。清空它会让「坏响应 → 纠正 → 429 → 回到原始
            # 提示 → 又坏」白烧纠错预算。_RESPONSE_CORRECTION 里的「上一次响应」在此
            # 仍准确指向最近一次真正收到的（无效的）响应。
            if on_retry:
                on_retry(request_attempt, delay, "provider temporarily unavailable")
            time.sleep(delay)
            raise_if_cancelled()
        except ProviderResponseError:
            if response_retries >= _RESPONSE_RETRY_BUDGET:
                raise
            delay = _RESPONSE_RETRY_DELAY_SECONDS
            response_retries += 1
            request_attempt += 1
            correction = response_correction
            if on_retry:
                on_retry(request_attempt, delay, "invalid provider response")
            time.sleep(delay)
            raise_if_cancelled()


ProviderBatch = TranslationBatch | PolishBatch | SemanticReviewBatch | SemanticRepairBatch


def run_provider_batches(
    job: Job,
    batches: Sequence[ProviderBatch],
    *,
    provider_config: CompatibleProviderConfig,
    max_retries: int,
    event_sink: EventSink | None,
    request: Callable[..., Any] | None = None,
    response_correction: str = _RESPONSE_CORRECTION,
    phase: str | None = None,
    stage_extras: dict[str, Any] | None = None,
    emit_progress_events: bool = True,
    with_batch_index: bool = False,
    fail_stage: bool = True,
    skip: Callable[[Any], bool] | None = None,
    on_result: Callable[[Any, Any], None] | None = None,
) -> None:
    """按批次串行调用 provider 的**唯一**循环骨架：取消点、批次事件、逐批回调与失败收口。

    `translate_batch_with_retries` 统一了单批的传输与重试，这个函数统一它外面那层循环；
    两者必须留在同一个模块里，原因是实测的，不是风格偏好：

    - **桩位**：大量测试把桩打在 ``xvt.pipeline.translation`` 的 ``translate_batch`` /
      ``time.sleep`` / ``_backoff_delay`` 上（局部重译的用例也打在这里，因为它复用
      ``translate_batch_with_retries`` 的默认 ``request``）。把循环或重试实现搬到新模块，
      默认 ``request`` 的名字解析就会离开本模块全局，这些桩会一次性失效，provider 调用
      绕过所有既有打桩点。
    - **依赖方向**：新模块若 import 重试实现就会与 translation 成环；而 polish /
      semantic / retranslate 已经依赖 translation，放这里零新增依赖边。

    参数与四条链路之间的真实差异一一对应：

    - ``phase``：事件里的 ``phase`` 字段。初译只在 ``--translation-quality polished`` 下
      带 ``"draft"``，润色带 ``"polish"``，局部重译完全不带；``None`` 表示不写这个键
      （不是写 null）。
    - ``stage_extras``：失败收口 ``set_stage(TRANSLATE, failed)`` 的 ``extras``。它与
      ``phase`` **不是**一回事，不能互相派生：初译带 ``phase="draft"`` 但失败事件历来
      不带任何 extras，润色则必须带 ``extras={"phase": "polish"}``。``None`` 表示不带，
      与初译 / 局部重译重构前的失败事件逐字节一致。
    - ``emit_progress_events``：是否发 ``translation_batch_started`` /
      ``translation_batch_completed``。局部重译只发重试事件，传 ``False``。
      重试事件（``translation_batch_retrying``）不受此开关影响，四条链路都发。
    - ``with_batch_index``：批次事件是否额外带 ``batch_index``，即**本轮循环的 1 起序号**，
      由这里的 ``enumerate`` 注入。它与稳定的 ``batch.id`` 是两个不同的东西：恢复后只跑
      剩余批次时 ``batch.id`` 可能是 38 而本轮序号是 1，把两者混用会显示成「批次 38/1」。
      只有语义校对（初审 / 修复 / 复审）历来带它，初译、润色与局部重译都不带，所以默认
      ``False``；三类事件（started / retrying / completed）同时带或同时不带。
    - ``fail_stage``：失败时是否在这里 ``set_stage(TRANSLATE, failed)``。初译、润色与局部
      重译都在这里收口；语义校对不收，它的 stage 失败由
      ``run_translation_semantic_review`` 的外层 ``except`` 统一带
      ``extras={"phase": "semantic_review"}`` 收口，这里再收一次会多发一个 ``stage_failed``
      事件（且第一个不带 phase）。传 ``False`` 时异常原样向外抛。
    - ``skip``：checkpoint 跳过判定。初译要求「批次已登记完成**且**译文覆盖该批全部 cue」；
      润色只看 ``batch.id in completed``（覆盖范围由 ``_load_progress`` 在读 checkpoint 时
      一次性校验）；局部重译没有 checkpoint，传 ``None``。
    - ``on_result``：每批成功后的落盘与记账（初译：累积译文、写
      ``translation-progress.json``、登记 artifact、首批成功后固化 provider 身份；
      润色：累积译文、记完成批次、写 ``translation-polish-progress.json``；
      局部重译：只在内存里累积）。它在 ``translation_batch_completed`` **之前**调用，
      与重构前的顺序一致。
    - ``request`` / ``response_correction``：原样透传给 ``translate_batch_with_retries``。
      初译与局部重译都不传，默认 ``request=None`` 让默认名 ``translate_batch`` 仍在本模块
      全局里解析，桩位不变；润色显式传 ``polish_batch``，同理保住
      ``xvt.pipeline.polish.polish_batch`` 这个桩位。

    ``fail_stage=True`` 时失败一律按 ``TRANSLATE`` / ``failed``（``extras=stage_extras``）收口后
    re-raise，与重构前初译 / 润色 / 局部重译三处的 ``except`` 相同。
    ``raise_if_cancelled()`` 保持在每批开头（紧接 ``skip`` 判定之前）。
    """
    batch_total = len(batches)
    phase_fields = {"phase": phase} if phase is not None else {}
    try:
        for batch_index, batch in enumerate(batches, start=1):
            raise_if_cancelled()
            if skip is not None and skip(batch):
                continue
            # 本轮序号只在显式要求时才进事件，键位紧跟 batch_total，与重构前 semantic 的
            # `_emit_batch_event(..., batch_index=...)` 完全一致。
            index_fields = {"batch_index": batch_index} if with_batch_index else {}
            if emit_progress_events and event_sink:
                event_sink(
                    create_event(
                        EventType.TRANSLATION_BATCH_STARTED,
                        **job_event_fields(job.id, get_job_file(job)),
                        **phase_fields,
                        batch=batch.id,
                        batch_total=batch_total,
                        **index_fields,
                    )
                )

            def emit_retry(attempt: int, delay: float, reason: str) -> None:
                if event_sink:
                    event_sink(
                        create_event(
                            EventType.TRANSLATION_BATCH_RETRYING,
                            **job_event_fields(job.id, get_job_file(job)),
                            **phase_fields,
                            batch=batch.id,
                            batch_total=batch_total,
                            **index_fields,
                            attempt=attempt,
                            delay_seconds=delay,
                            reason=reason,
                        )
                    )

            result = translate_batch_with_retries(
                batch,
                provider_config,
                max_retries=max_retries,
                on_retry=emit_retry,
                request=request,
                response_correction=response_correction,
            )
            if on_result is not None:
                on_result(batch, result)
            if emit_progress_events and event_sink:
                event_sink(
                    create_event(
                        EventType.TRANSLATION_BATCH_COMPLETED,
                        **job_event_fields(job.id, get_job_file(job)),
                        **phase_fields,
                        batch=batch.id,
                        batch_total=batch_total,
                        **index_fields,
                    )
                )
    except Exception as error:
        if fail_stage:
            set_stage(
                job,
                StageName.TRANSLATE,
                StageStatus.FAILED,
                event_sink,
                error=str(error),
                error_category=error_category(error),
                extras=stage_extras,
            )
        raise


def load_translation_response(response_file: str | Path, expected_ids: set[int]) -> dict[int, str]:
    """读取**用户提交的**响应文件并解析出译文映射。

    读不出或解析不了时抛 ``UserInputError``（经 ``load_user_response_json``）。已登记
    artifact 的读取不要走这里，改用 ``translations_from_response``。
    """
    return translations_from_response(
        load_user_response_json(response_file, what="translation response"), expected_ids
    )


def translations_from_response(response: Any, expected_ids: set[int]) -> dict[int, str]:
    """从已解析的响应载荷里取出 ``{cue_id: 译文}``；结构或覆盖范围不符时抛 ``ValueError``。"""
    items = response.get("items") if isinstance(response, dict) else None
    if not isinstance(items, list):
        raise ValueError("translation response must contain an items array")
    translations = {}
    for item in items:
        cue_id = item.get("id") if isinstance(item, dict) else None
        text = item.get("text") if isinstance(item, dict) else None
        if not isinstance(cue_id, int) or isinstance(cue_id, bool) or cue_id in translations:
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
        expected = apply_translations(
            cues,
            translations,
            target_language=target_language,
            normalize_generated=True,
        )
        return draft_path if load_cues(draft_path) == expected else None
    # XvtError：本函数经 load_translation_response 间接调用 load_user_response_json，
    # 已登记 artifact 读不出时抛的是 UserInputError；漏掉它会让只读谓词抛异常而不是判否。
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, XvtError):
        return None


# `repair_polish_draft` 无法零成本重建时的统一文案：status（recovery）与
# continue（workflow）必须给出同一句话，避免两处口径漂移。
REPAIR_POLISH_DRAFT_MANUAL_FIX_MESSAGE = (
    "润色草稿缺失或 checksum 已失效，不能从最终译文安全重建，"
    "且 translation_progress checkpoint 不足以零成本重建；"
    "请重新导入或生成原始译文草稿。"
)


def translation_progress_rebuildable(job: Job, config: WorkflowConfig) -> bool:
    """判定 translation_progress checkpoint 能否**零 API 成本**重建译文草稿。

    `plan_advance_steps` 在「polished 且 draft 失效」时给出 `repair_polish_draft`。
    该步骤对应的执行路径是 `run_translation`：只有当 checkpoint 覆盖全部批次且
    fingerprint 与当前配置一致时，逐批循环才会全部命中 `continue` 分支、一次
    provider 调用都不发生。任一条件不满足就会真实调用 API 全量重翻，因此这里
    fail-closed。

    `status`（recovery）与 `continue`（workflow）必须共用本谓词，否则两边对
    provider 可解析性的判断会因环境变量差异而漂移。

    provider 无法解析（Agent 模式或缺少 base URL / model / API key）时返回 False
    而不抛异常——status 只是查询，不应因环境不全而失败。
    """
    target_language = config.target_language
    if not target_language:
        return False
    source_path = valid_artifact_path(job, "source_cues")
    progress_path = valid_artifact_path(job, "translation_progress")
    if source_path is None or progress_path is None:
        return False
    try:
        mode, provider_config, _fallback = resolve_provider(
            config.provider,
            base_url=config.base_url,
            model=config.model,
            api_key_env=config.api_key_env,
            timeout_seconds=config.timeout_seconds,
        )
    except UserInputError:
        # 显式 openai-compatible provider 缺配置时会抛错；查询路径不应因此失败。
        return False
    if provider_config is None:
        # Agent 模式没有确定性的零成本重建路径。
        return False
    # 与 workflow 执行侧同口径（两边都走 resolved_translation_policy），
    # 保证这里重算的 fingerprint 与实际执行用的策略一致；口径一漂移，
    # checkpoint 会被误判为不可重建，整轮翻译白白重付一次 API 费用。
    policy = resolved_translation_policy(job, config)
    try:
        cues = load_cues(source_path)
        if not cues:
            return False
        source_language = next(
            (cue.source_language for cue in cues if cue.source_language),
            job.config.get("source_language", "auto"),
        )
        batches = build_translation_batches(
            cues,
            source_language=source_language,
            target_language=target_language,
            batch_size=config.batch_size,
            policy=policy,
        )
        fingerprint = _translation_fingerprint(
            source_cues_checksum=checksum(source_path),
            source_language=source_language,
            target_language=target_language,
            provider=mode,
            base_url=provider_config.base_url,
            model=provider_config.model,
            batch_size=config.batch_size,
            policy=policy,
        )
        data = json.loads(progress_path.read_text(encoding="utf-8"))
        if (
            not isinstance(data, dict)
            or data.get("fingerprint") != fingerprint
            or data.get("completed_batches") != [batch.id for batch in batches]
        ):
            return False
        expected_ids = {item.id for batch in batches for item in batch.items}
        # 每个批次的全部 cue 都必须已有译文，否则该批次仍会被重新调用。
        load_translation_response(progress_path, expected_ids)
    # XvtError：本函数经 load_translation_response 间接调用 load_user_response_json，
    # 已登记 artifact 读不出时抛的是 UserInputError；漏掉它会让只读谓词抛异常而不是判否。
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, XvtError):
        return False
    return True


def _translation_manifest(job: Job) -> dict[str, Any] | None:
    path = valid_artifact_path(job, "translation_batches")
    if path is None:
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return manifest if isinstance(manifest, dict) else None


def _require_usable_translation_manifest(job: Job) -> None:
    """回填前收紧已登记 manifest 的可信度，避免可疑状态与极旧 Job 走同一放行路径。

    `_translation_manifest` 对「从未登记」「已登记但 checksum 失效或文件缺失」
    「内容不是 JSON 对象」一律返回 None。只有第一种是合法的历史状态（极旧 Job 没有
    manifest artifact，回填按无 manifest 处理）；后两种一旦被同样放行，`task` 路由会
    静默退化成标准翻译，并跳过 `_require_manifest_source_cues` 的源 cue 绑定。

    只在回填这一写路径收紧：`agent_translate_and_polish_waiting` 等只读谓词仍使用不抛
    异常的 `_translation_manifest`，`status` 不应因 manifest 损坏而失败。
    """
    if not any(item.kind == "translation_batches" for item in job.artifacts):
        return
    if valid_artifact_path(job, "translation_batches") is None:
        raise ValueError(
            "translation batch manifest is missing or its checksum is stale; "
            "rerun prepare-translation and translate the regenerated manifest"
        )
    if _translation_manifest(job) is None:
        raise ValueError(
            "translation batch manifest is not a readable JSON object; "
            "rerun prepare-translation and translate the regenerated manifest"
        )


def _require_manifest_source_cues(manifest: dict[str, Any] | None, source_path: Path) -> None:
    """确认 manifest 与当前源 cue 同源，避免陈旧响应错位回填。

    修复前 prepare 的标准 manifest 没有 `source_cues_checksum`，此时保持接受（前向兼容）。
    """
    if manifest is None:
        return
    recorded = manifest.get("source_cues_checksum")
    if not isinstance(recorded, str) or recorded == checksum(source_path):
        return
    raise ValueError(
        "translation batch manifest was prepared for different source cues; "
        "rerun prepare-translation and translate the regenerated manifest"
    )


def _agent_polished_manifest(job: Job) -> dict[str, Any] | None:
    manifest = _translation_manifest(job)
    if manifest is None or manifest.get("task") != "translate_and_polish":
        return None
    source_path = valid_artifact_path(job, "source_cues")
    workflow = get_workflow_config(job)
    target_language = (
        workflow.target_language if workflow is not None else job.config.get("target_language")
    )
    policy = resolved_translation_policy(job).to_dict()
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
    response = load_user_response_json(response_file, what="polish response")
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
        translations = parse_cue_text_items(data.get("items"))
        if translations is None:
            return None
        if set(translations) != cue_ids:
            return None
        return batch_ids, translations
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None


def agent_translate_and_polish_waiting(job: Job) -> bool:
    manifest = _agent_polished_manifest(job)
    return manifest is not None and _agent_polish_progress(job, manifest) is None


def agent_translate_and_polish_started(job: Job) -> bool:
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


def agent_translate_and_polish_progress_ready(job: Job) -> bool:
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
        normalize_generated=True,
    )


def agent_translate_and_polish_core_satisfied(job: Job) -> bool:
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
    """一次成型 ``translate_and_polish`` 的收尾：润色译文直接来自单次 Agent 响应。

    这条路径**从不写** ``translation-draft-cues.json``——初译与润色在同一次回填里
    完成，``translation_polished_cues`` 派生自 ``source_cues`` 与那份响应，不派生自
    任何草稿。因此 Job 里残留的 ``translation_draft_cues`` 登记（只可能来自此前某一
    轮 Q5a 润色）已经不属于 Job 的当前状态：它一路挂到 ``completed``，让 ``status``
    报 complete 并推荐 ``xvt verify``，而 ``verify`` 必然以「artifact file is missing」
    失败（完成标准：Job、stage、artifact、checksum 和事件能够解释任务当前状态）。

    摘记录沿用既有的「按 kind 过滤 ``job.artifacts``」失效机制，不另造写法；只摘这
    一条，**不**用 ``downstream_of(TRANSLATION_DRAFT_CUES)``——那个闭包包含本函数
    刚刚产出的 ``translation_polished_cues``。恢复依据（约束 7）不受影响：planner 的
    polished 腿对一次成型 Job 走 ``agent_translate_and_polish_core_satisfied`` /
    ``agent_translate_and_polish_started``（两者都只看 manifest 与响应，不看草稿），
    ``polish-translation`` 本来就以 ``AGENT_ONE_SHOT_POLISH_RERUN_MESSAGE`` 拒绝这
    类 Job，``translation_progress`` 等 checkpoint 记录原样保留。物理文件（若还在）
    不删，只是不再被登记为本 Job 的产物。
    """

    job = load_job(job_file)
    expected = _agent_polished_cues(job)
    artifacts_directory = get_job_directory(job) / "artifacts"
    stale_draft = {ArtifactKind.TRANSLATION_DRAFT_CUES}
    job.artifacts = [artifact for artifact in job.artifacts if artifact.kind not in stale_draft]
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
        context = translation_qa_context(job)
        media_data = {"width": context["video_width"], "height": context["video_height"]}
        report = check_subtitles(
            translated,
            **context,
            require_translation=True,
        )
        qa_path = artifacts_directory / "qa-translated.json"
        write_json(qa_path, asdict(report))
        add_artifact(job, "translated_subtitle_qa", qa_path, event_sink)
        if not complete_qa_stage(job, report, event_sink):
            return (qa_path,)

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
        config = get_workflow_config(job)
        ass_path = save_bilingual_ass(
            translated,
            artifacts_directory / "bilingual.ass",
            video_width=width,
            video_height=height,
            options=AssStyleOptions(
                target_font=config.target_font if config else None,
                source_font=config.source_font if config else None,
            ),
        )
        add_artifact(job, "bilingual_ass", ass_path, event_sink)
        paths.append(ass_path)
    return tuple(paths)


def _resolved_translation_inputs(
    workflow: WorkflowConfig | None,
    *,
    policy: TranslationPolicy | None,
    glossary: str | Path | None,
    translation_style: str | None,
    batch_size: int | None,
) -> tuple[TranslationPolicy, int]:
    """解析本次翻译生效的策略与批次大小：显式参数优先，其余回退 Job 固化的 workflow 快照。

    与同文件里 base_url / model 的回退同口径：端到端 Job 上 ``xvt translate``
    （恢复计划里「放弃重译」的推荐命令）如果按 CLI 默认值现算策略，译文就不带术语，
    而译文 QA 仍按 ``workflow.translation_policy`` 判 ``glossary_violation``——术语死锁
    没有出口。策略与 batch_size 都进翻译指纹，漂移还会清零批次进度，全部批次重付一次
    翻译费。

    逐字段回退而不是整份替换：只传 ``--glossary`` 时风格仍沿用快照，只传
    ``--translation-style`` 时术语表仍沿用快照，显式值永远压过快照。
    ``policy`` 由调用方直接给出时（workflow 执行路径）原样采用。
    原子 Job（``workflow is None``）保持历史行为：顶层 ``translation_policy``
    会被本次调用重写，QA 与翻译用的是同一份策略，不存在同款死锁。
    """

    snapshot = workflow.translation_policy if workflow is not None else None
    resolved_batch_size = batch_size
    if resolved_batch_size is None:
        resolved_batch_size = workflow.batch_size if workflow is not None else DEFAULT_BATCH_SIZE
    if policy is not None:
        return policy, resolved_batch_size
    if glossary is None and translation_style is None and snapshot is not None:
        return snapshot, resolved_batch_size
    style = translation_style
    if style is None:
        style = snapshot.style if snapshot is not None else DEFAULT_TRANSLATION_STYLE
    # load_translation_policy 负责 style 白名单校验与术语表解析。
    resolved = load_translation_policy(glossary, style=style)
    if glossary is None and snapshot is not None:
        resolved = replace(resolved, terms=snapshot.terms)
    return resolved, resolved_batch_size


def prepare_agent_translation(
    job_file: str | Path,
    *,
    source_language: str,
    target_language: str,
    batch_size: int | None = None,
    policy: TranslationPolicy | None = None,
    glossary: str | Path | None = None,
    translation_style: str | None = None,
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
    resolved_policy, batch_size = _resolved_translation_inputs(
        workflow,
        policy=policy,
        glossary=glossary,
        translation_style=translation_style,
        batch_size=batch_size,
    )
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
            # 标准 manifest 也绑定源 cue checksum，回填时据此拒绝源文变更后的陈旧响应。
            "source_cues_checksum": checksum(source_path),
            "batches": [batch.to_dict() for batch in batches],
        }
        if quality == "polished" and await_input:
            manifest.update(
                {
                    "batch_size": batch_size,
                    "task": "translate_and_polish",
                    "task_version": TRANSLATE_AND_POLISH_TASK_VERSION,
                }
            )
            manifest["fingerprint"] = agent_translation_manifest_fingerprint(manifest)
        write_json(
            output_path,
            manifest,
        )
        # 配置必须先于 add_artifact 的 save_job 写入：API 模式（await_input=False）且
        # 有 workflow 时，add_artifact 是本函数最后一次落盘机会，晚写会被后续
        # load_job 丢弃，术语表等策略随之失效。
        job.config["target_language"] = target_language
        job.config["source_language"] = source_language
        job.config["translation_policy"] = resolved_policy.to_dict()
        # 整轮翻译准备取代任何未回填的局部重译：cue_retranslation_batches 在 artifact
        # 依赖图上只派生自 source_cues，源 cue 没变时它不会被任何失效规则带走，于是
        # 「放弃重译后重新翻译」的 Job 上四条件仍可全真，cue_retranslation_waiting 会把
        # 合法的初译等待盖成重译等待，status 与 apply 一起指向作废的 manifest。
        # 这里显式移出登记，让新的 translation_batches 成为唯一在途的翻译请求。
        job.artifacts = [item for item in job.artifacts if item.kind != "cue_retranslation_batches"]
        if workflow is None:
            # workflow Job 的 quality 由 workflow 配置派生，不写入顶层 config。
            job.config["translation_quality"] = quality
        add_artifact(job, "translation_batches", output_path, event_sink)
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


def _persist_resolved_provider(job: Job, provider_config: CompatibleProviderConfig) -> None:
    """把实际生效的 base_url / model 写回 ``job.config["workflow"]``（调用方负责 save_job）。

    端到端 Job 在 ``run`` 时可以不带 ``--base-url`` / ``--model``，此时两个字段落盘为 null，
    ``resolve_provider`` 靠环境变量补齐。它们都参与翻译指纹：``continue`` 若在另一个环境变量
    不同的终端里执行，指纹就会变化、批次进度清零，还可能拿过期模型名去撞 4xx。因此只要
    openai-compatible 模式真的成功拿回过一个批次，就把生效值固化进 Job；后续 ``continue``
    读到非 null 值便不再受进程环境影响。``xvt translate`` 显式传入的覆盖值同样写回，
    保证 translate 与 continue / status 看到同一套身份。

    必须在**首个批次成功之后**才调用：先写回再请求，环境变量里的过期模型名会先被钉进
    job.json，用户修好环境变量也没有入口再改它（``continue`` 没有 ``--model``）。
    指纹早在解析时已按同一份 ``provider_config`` 算出，推迟写回不影响一致性。
    Agent 降级不会走到这里；``api_key`` 永远不落盘；原子 Job 没有 workflow 配置，不写。
    """
    workflow = get_workflow_config(job)
    if workflow is None:
        return
    if workflow.base_url == provider_config.base_url and workflow.model == provider_config.model:
        return
    set_workflow_config(
        job,
        replace(workflow, base_url=provider_config.base_url, model=provider_config.model),
    )


def _fallback_reason_with_persisted_provider(
    reason: str | None,
    workflow: WorkflowConfig | None,
    *,
    api_key_env: str,
) -> str | None:
    """Job 已固化 provider 身份却仍降级时，把「缺的只是 API Key」这层原因说清楚，避免静默降级。

    ``base_url`` / ``model`` 非空既可能是首批成功后写回，也可能是用户首次 ``run`` 时显式传入
    （此时还从未翻译过），因此措辞只说「Job 钉住了这套身份」，不断言历史。
    """
    if reason is None or workflow is None or not (workflow.base_url and workflow.model):
        return reason
    return (
        f"{reason}; this job is pinned to openai-compatible "
        f"(model={workflow.model}, base_url={workflow.base_url}) "
        f"but the current shell lacks ${api_key_env}"
    )


def run_translation(
    job_file: str | Path,
    *,
    target_language: str,
    provider: str = "auto",
    base_url: str | None = None,
    model: str | None = None,
    api_key_env: str = "XVT_TRANSLATION_API_KEY",
    batch_size: int | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    policy: TranslationPolicy | None = None,
    glossary: str | Path | None = None,
    translation_style: str | None = None,
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
    resolved_policy, batch_size = _resolved_translation_inputs(
        workflow,
        policy=policy,
        glossary=glossary,
        translation_style=translation_style,
        batch_size=batch_size,
    )
    source_path = valid_artifact_path(job, "source_cues")
    if source_path is None:
        raise ValueError("translation requires checksum-valid source_cues")
    cues = load_cues(source_path)
    source_language = next(
        (cue.source_language for cue in cues if cue.source_language),
        job.config.get("source_language", "auto"),
    )
    if workflow is not None:
        # 与 polish._polished_config 同口径：调用方未显式覆盖时回退到 Job 固化的身份，
        # 否则 `xvt translate` 会走环境变量而 continue / status 走 Job 值，指纹分叉。
        base_url = base_url if base_url is not None else workflow.base_url
        model = model if model is not None else workflow.model
    mode, provider_config, fallback_reason = resolve_provider(
        provider,
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        timeout_seconds=timeout_seconds,
    )
    fallback_reason = _fallback_reason_with_persisted_provider(
        fallback_reason, workflow, api_key_env=api_key_env
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
            # 初译侧的降级事件不带 phase；AWAITING_INPUT 已由上面的
            # prepare_agent_translation(await_input=True) 设过，helper 只发事件。
            emit_agent_fallback(
                job,
                manifest_path=batches_path,
                reason=fallback_reason,
                event_sink=event_sink,
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
    provider_persisted = False

    def skip_completed_batch(batch: TranslationBatch) -> bool:
        batch_ids = {item.id for item in batch.items}
        return batch.id in completed_batches and batch_ids <= translations.keys()

    def record_batch(batch: TranslationBatch, translated_batch: dict[int, str]) -> None:
        nonlocal provider_persisted
        translations.update(translated_batch)
        completed_batches.add(batch.id)
        if not provider_persisted:
            # 首个批次成功才固化 provider 身份；下面的 add_artifact 会一并 save_job。
            _persist_resolved_provider(job, provider_config)
            provider_persisted = True
        write_json(
            progress_output,
            {
                "fingerprint": translation_fingerprint,
                "completed_batches": sorted(completed_batches),
                "items": [
                    {"id": cue_id, "text": text} for cue_id, text in sorted(translations.items())
                ],
            },
        )
        add_artifact(job, "translation_progress", progress_output, event_sink)

    run_provider_batches(
        job,
        batches,
        provider_config=provider_config,
        max_retries=max_retries,
        event_sink=event_sink,
        phase="draft" if quality == "polished" else None,
        skip=skip_completed_batch,
        on_result=record_batch,
    )
    if not provider_persisted:
        # 全部批次都命中 checkpoint：指纹已按这套身份校验通过，同样值得固化。
        _persist_resolved_provider(job, provider_config)

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
    _require_usable_translation_manifest(job)
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
        if agent_translate_and_polish_core_satisfied(job):
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
    # 回填校验必须全部完成后才允许触碰 stage：response 是用户敲进命令行的输入，坏文件
    # （读不出、结构不符、覆盖不全）不改写 Job 状态，TRANSLATE 保持 awaiting_input——
    # 一旦提前置 running 再回写 failed，plan_advance_steps 就不再返回等待步骤，统一
    # xvt apply 会误报「not awaiting Agent input」，恢复计划也从等待回填降级成重跑翻译。
    # 校验通过后由 finalize_translation 接管 stage 记账（其内部失败自会置 failed）。
    source_path = valid_artifact_path(job, "source_cues")
    if source_path is None:
        raise ValueError("translation requires checksum-valid source_cues")
    _require_manifest_source_cues(manifest, source_path)
    cues = load_cues(source_path)
    translations = load_translation_response(response_file, {cue.id for cue in cues})
    translated = apply_translations(
        cues,
        translations,
        target_language=target_language,
        normalize_generated=True,
    )
    set_stage(job, StageName.TRANSLATE, StageStatus.RUNNING, event_sink)
    return finalize_translation(
        job, translated, event_sink, invalidate=invalidate_rendered_artifacts
    )


def apply_subtitle_translation(
    job_file: str | Path,
    subtitle_file: str | Path,
    *,
    target_language: str,
    event_sink: EventSink | None = None,
    require_timeline_overlap: bool = False,
) -> tuple[Path, ...]:
    """把一份译文字幕回填到 Job 上。

    ``require_timeline_overlap`` 原样透传给 ``map_subtitle_translations``：只有
    「重新套用 Job 内早先存下的副本」的调用方传 ``True``，用户此刻显式交出字幕的
    路径（``xvt import-translation`` / ``run --translation-subtitles``）保持默认宽松。
    """

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
    # 与 apply_agent_translation 同一约定：字幕文件是用户输入，读取、解析与映射都是
    # 只读校验，放在 set_stage 之前——坏文件不改写 Job 状态（若 TRANSLATE 正停在
    # Agent 等待上，等待态不得被顶成 failed）。
    source_cues = load_cues(artifact_path(job, "source_cues"))
    subtitle_path = Path(subtitle_file).expanduser()
    translated_cues = load_subtitles(
        subtitle_path,
        role="translated",
        target_language=target_language,
    )
    translations, mapping_report = map_subtitle_translations(
        source_cues,
        translated_cues,
        require_timeline_overlap=require_timeline_overlap,
    )
    translated = apply_translations(
        source_cues,
        translations,
        target_language=target_language,
        normalize_generated=False,
    )
    current_stage = StageName.TRANSLATE
    set_stage(job, current_stage, StageStatus.RUNNING, event_sink)
    try:
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
        job, translated, event_sink, invalidate=invalidate_rendered_artifacts
    )

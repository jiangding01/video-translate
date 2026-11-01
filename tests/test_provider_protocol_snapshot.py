"""provider 协议快照回归网（R3.1 Phase 1）。

本模块只冻结既有事实，不改变任何行为。它是后续把 translation / polish / semantic /
retranslate 四条链路里重复的 provider 编排代码收敛成单份实现时的安全网：红线是
「artifact 文件名与内容、manifest 格式、fingerprint 语义、事件序列字节级不变」。
**重构后这里出现任何 diff，就地回退重构，不要修改 golden 值。**

golden 值在 ``tests/fixtures/provider-protocol-snapshot.json``。

全部场景都是纯字幕 Job（无媒体，因此不渲染 ASS、不烧录），源文固定为同一份 4 条 cue 的
英文 SRT（cue 2/3 构成一个跨 cue 语义组）。A、B 两个场景是
``--translation-quality polished``，C 之后的场景是标准质量（``standard``）。

A 场景（API / openai-compatible）
--------------------------------
真实入口顺序取自 ``src/xvt/workflow.py::_ensure_translation`` 的 polished 分支：

1. ``run_translation(..., translation_quality="polished")`` —— 初译 draft
2. ``run_translation_polish(job_file)`` —— 润色（provider 身份从 Job 读回，不再传参）
3. ``run_translation_semantic_review(job_file)`` —— 语义初审 → 恰好一次修复 → 复审 → 收口

依据的现有用例：

- ``tests/test_workflow.py::WorkflowTest::test_polished_api_stub_runs_end_to_end``
  （三步编排与三个打桩点）
- ``tests/test_pipeline_polish.py::TranslationPolishPipelineTest``
  ``::test_atomic_api_polished_rerun_reuses_draft_and_final_result``（draft/polish 分工）
- ``tests/test_pipeline_semantic.py::SemanticReviewPipelineTest``
  ``::test_api_repairs_complete_group_once_then_reviews_again``（一次修复后复审收口）

B 场景（Agent 传输）
-------------------
1. ``run_translation(..., provider="agent", translation_quality="polished")`` ——
   走 ``prepare_agent_translation(await_input=True)``，写出 ``task=translate_and_polish``
   的 ``translation-batches.json`` 并进入 ``awaiting_input``
2. ``apply_agent_translation`` —— 一份响应同时回填 draft 与润色结果（agent polished 路径
   不产生 ``translation_draft_cues`` / ``translation_polish_batches``）
3. ``run_translation_semantic_review`` —— agent provider 下转为
   ``prepare_agent_semantic_review``，返回 ``awaiting_agent``
4. ``apply_agent_semantic_review`` —— 合并响应（初审 + 一次修复 + 复审）一次收口

依据的现有用例：

- ``tests/test_pipeline_polish.py::TranslationPolishPipelineTest``
  ``::test_new_agent_job_translates_and_polishes_in_one_response``（单响应 translate+polish）
- ``tests/test_workflow.py::WorkflowTest``
  ``::test_agent_polished_workflow_waits_once_then_renders_without_draft``（只等待一次）
- ``tests/test_pipeline_semantic.py::SemanticReviewPipelineTest``
  ``::test_agent_combined_response_repairs_once_and_finalizes``（合并响应一次修复后收口）

C 场景（``standard_agent_fallback``）
-----------------------------------
缺 provider 身份时 ``provider="auto"`` 的降级传输，冻住 ``translation_fallback`` 事件本身
（此前全仓只断言返回值 ``TranslationRunResult.fallback_reason``，事件丢了不会有人红）。

1. Job 固化了 ``provider="auto"`` 与 base URL / model（相当于 ``run --base-url ... --model
   ...``），但当前终端三个 ``XVT_TRANSLATION_*`` 环境变量与 ``$TEST_TRANSLATION_API_KEY``
   全空
2. ``run_translation(..., provider="auto")`` —— ``resolve_provider`` 返回
   ``("agent", None, "missing API key environment variable ...")``，
   ``_fallback_reason_with_persisted_provider`` 再补上「Job 钉住了这套身份」那一截
3. 写出标准 ``translate`` 形态的 ``translation-batches.json`` 并停在 ``awaiting_input``；
   发射点是 ``src/xvt/pipeline/translation.py`` 的 ``mode == "agent"`` 分支

依据的现有用例：

- ``tests/test_pipeline_translation.py::TranslationProviderPersistenceTest``
  ``::test_agent_fallback_names_persisted_provider_when_only_api_key_is_missing``
  （降级文案与「只缺 API Key」的措辞）
- ``::test_agent_fallback_does_not_persist_provider_config``（降级不写回 provider 身份）

D 场景（``api_progress_reset``）
-------------------------------
冻住 ``translation_progress_reset`` 事件在序列中的位置（此前全仓零断言）。同一个 Job 上
连跑两次 API 初译，第二次换 model：

1. ``run_translation(..., model="snapshot-model")`` —— 写出 ``translation-progress.json``
2. ``run_translation(..., model="snapshot-model-v2")`` —— checkpoint 仍 checksum 有效但
   ``_translation_fingerprint`` 变了，发射 ``translation_progress_reset``（reason 为固定
   字面量 ``translation fingerprint changed``）后重跑全部批次

依据的现有用例：``tests/test_pipeline_polish.py::TranslationPolishPipelineTest``
``::test_source_language_change_resets_api_polish_progress``（指纹漂移清零 checkpoint 的既有
语义，只是那条断言的是 polish 侧的产物而非事件）。

E / F 场景（``retranslate_api`` / ``retranslate_agent``）
--------------------------------------------------------
局部重译链路此前零快照。两个场景共用同一个前置：标准质量 + API 初译完成的纯字幕 Job
（前置过程不接事件，事件序列只覆盖重译本身）。

E（API 分支）：``retranslate_cues(cue_ids=(2, 3), provider="openai-compatible")`` ——
写 ``cue-retranslation-batches.json`` → 首个批次一次可重试传输失败（冻住
``translation_batch_retrying`` 的独立重试预算）→ 写 ``cue-retranslation-response.json``
→ 内部回填并重跑译文 QA。

F（Agent 分支）：``retranslate_cues(..., provider="agent")`` 停在 ``awaiting_input``，随后
``apply_cue_retranslation`` 按 manifest fingerprint 回填。

依据的现有用例：

- ``tests/test_pipeline_retranslate.py::CueRetranslationTest``
  ``::test_cue_retranslation_uses_independent_retry_budgets_and_events``（API 分支与退避桩位）
- ``::test_cue_retranslation_updates_only_requested_cues_and_invalidates_burn_outputs``
  （Agent manifest → 响应 → 回填）

G 场景（``polish_agent_rerun``）
-------------------------------
B 场景走的是单响应 ``task=translate_and_polish``，到不了 polish 自己的 Agent 前奏。本场景
在「已有标准 draft 的 Job 上显式补跑润色、且 provider 落 agent」时才可达：

1. 前置：Job 固化 ``provider="auto"`` 与 base URL / model，在有 API Key 的终端用 API 跑完
   标准初译（不接事件）
2. ``run_translation_polish(job_file)`` —— 换到缺 API Key 的终端：``_prepare_draft`` 把
   ``translated_cues`` 复制成 ``translation_draft_cues`` 并把目标改写成 polished，
   ``_run_translation_polish`` 落到 agent 模式，写出 ``translation-polish-batches.json``、
   停在 ``awaiting_input`` 并发射带 ``phase=polish`` 的 ``translation_fallback``
   （发射点是 ``src/xvt/pipeline/polish.py`` 的 ``mode == "agent"`` 分支；注意它**不**走
   ``_fallback_reason_with_persisted_provider``，因此文案比 C 场景短一截，这条不对称也被冻住）
3. ``apply_agent_translation_polish`` —— 回填润色响应，写 progress 并收口译文产物

依据的现有用例：

- ``tests/test_pipeline_polish.py::TranslationPolishPipelineTest``
  ``::test_agent_polish_remains_applicable_when_auto_provider_becomes_available``
  （auto + 缺 Key → agent 模式 → ``apply_agent_translation_polish``）
- ``::test_agent_polish_round_trip_preserves_timeline``（manifest → 响应 → 回填的时间轴不变）

provider 打桩位置与现有测试完全一致（重构阶段要靠它们继续有效）：
``xvt.pipeline.translation.translate_batch``、``xvt.pipeline.polish.polish_batch``、
``xvt.pipeline.semantic.semantic_review_batch``、``xvt.pipeline.semantic.semantic_repair_batch``。
**局部重译（E 场景）也打 ``xvt.pipeline.translation.translate_batch``**：
``pipeline.retranslate`` 复用 ``translate_batch_with_retries`` 的默认 request（即 translation
模块里按值绑定的 ``translate_batch``），现有重译测试就是这么打桩的。重构把四条链路的编排
收敛成单份时必须保住这个桩位，否则重译的 provider 调用会绕过所有现有打桩点。
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from xvt.events import Event
from xvt.job_store import get_job_file, load_job, save_job
from xvt.models import Job
from xvt.pipeline import (
    apply_agent_semantic_review,
    apply_agent_translation,
    apply_agent_translation_polish,
    apply_cue_retranslation,
    retranslate_cues,
    run_subtitle_transcription,
    run_translation,
    run_translation_polish,
    run_translation_semantic_review,
)
from xvt.recovery import file_checksum
from xvt.translate import SemanticIssue, SemanticSuggestedItem, TranslationPolicy
from xvt.translation_provider import TranslationProviderError
from xvt.workflow_config import WorkflowConfig, get_workflow_config, set_workflow_config

GOLDEN_PATH = Path(__file__).parent / "fixtures" / "provider-protocol-snapshot.json"

# 固定源文：cue 1 与 cue 4 各自成句，cue 2 行尾无终止标点、cue 3 续写同一句，
# 于是语义分组必然产出 [1] / [2, 3] / [4] 三个批次，一次修复覆盖跨 cue 的那一组。
SOURCE_SRT = (
    "1\n00:00:00,000 --> 00:00:02,000\nWe built the pipeline last winter.\n\n"
    "2\n00:00:02,000 --> 00:00:04,000\nIt runs on a laptop and it keeps\n\n"
    "3\n00:00:04,000 --> 00:00:06,000\nevery subtitle timing untouched.\n\n"
    "4\n00:00:06,000 --> 00:00:08,000\nNothing else changes.\n"
)

# 假译文直接写成最终形态（不带全角句末标点），避免再绕一层简中标点收口的困惑；
# 写入仍走真实路径，normalize_generated_subtitle_text 照常执行。
DRAFT_TEXTS = {
    1: "我们去年冬天搭好了这条流水线",
    2: "它跑在一台笔记本上",
    3: "并且完全不动字幕的时间轴",
    4: "其他什么都没变",
}
POLISHED_TEXTS = {
    1: "我们去年冬天把这条流水线搭了起来",
    2: "它跑在一台笔记本上",
    3: "而且完全不改字幕时间轴",
    4: "别的什么都没动",
}
# 修复建议必须确实改过文本，否则 parse_semantic_review_response 会拒绝该建议。
REPAIRED_TEXTS = {
    2: "它跑在一台普通笔记本上",
    3: "而且一个字幕时间点都不改",
}
# 局部重译只改选中的 cue，文本必须与 DRAFT_TEXTS 不同，否则「只回填选中 ID」无从观察。
RETRANSLATE_CUE_IDS = (2, 3)
RETRANSLATED_TEXTS = {
    2: "它跑在一台旧笔记本上",
    3: "并且一帧字幕时间都没动",
}
SEMANTIC_ISSUE_MESSAGE = "two cues read as a broken sentence"

PROVIDER_BASE_URL = "https://provider.example.com/v1"
PROVIDER_MODEL = "snapshot-model"
# D 场景第二趟用的 model：只为让 _translation_fingerprint 漂移，触发 checkpoint 清零。
PROVIDER_MODEL_NEXT = "snapshot-model-v2"
API_KEY_ENV = "TEST_TRANSLATION_API_KEY"
BATCH_SIZE = 2

# 「缺 provider 身份」的终端。空串在 resolve_provider 眼里与未设置等价，用赋空值而不是
# 删键，是为了不依赖跑测试那台机器的真实环境变量（否则 C / G 场景会随开发机漂移）。
MISSING_PROVIDER_ENV = {
    "XVT_TRANSLATION_BASE_URL": "",
    "XVT_TRANSLATION_MODEL": "",
    "XVT_TRANSLATION_API_KEY": "",
    API_KEY_ENV: "",
}

# 事件投影保留的 data 字段。任务要求的 (type, phase, batch, batch_index) 之外多留
# stage / status / kind：它们同样确定，且能把 stage_* 与 artifact_created 事件区分开，
# 否则整段序列会塌缩成一串无法分辨的同名事件。不含 ts、job_id、job_file 与路径。
#
# mode / reason / attempt 是后加的：translation_fallback、translation_progress_reset 与
# translation_batch_retrying 三类事件在 (type, stage, status, kind, phase, batch) 上几乎没有
# 可辨识负载，不留 reason 就只能冻住「序列里有这么一个事件」，连降级原因写错都看不出来。
# 三个键在 A / B 两个场景的事件里从不出现（投影只在键存在时才落字段），因此它们的 golden
# 字节不变。取值全部确定：fallback 的 reason 由固定 env / Job 身份派生，progress_reset 的
# reason 是固定字面量，batch_retrying 的 reason 也是 translate_batch_with_retries 里的固定
# 字面量（不是 provider 的报错文案）。失败收口走的是 ``error`` 键而非 reason，不在投影内，
# 因此不会把绝对路径或密钥带进 golden。
_EVENT_FIELDS = (
    "stage",
    "status",
    "kind",
    "phase",
    "batch",
    "batch_index",
    "mode",
    "reason",
    "attempt",
)

# 需要「规范化投影」而不能直接冻结文件内容 checksum 的字段。
#
# ``job_id``（两个场景都要抹）：``prepare_agent_translation``、``polish._write_batches``
# 与 ``prepare_agent_semantic_review`` 都把 ``job.id`` 写进 manifest，而 job id 含创建
# 时间戳，逐字节冻结必然漂移。
_API_BLANKED_KEYS = frozenset({"job_id"})
#
# ``fingerprint``（只在 B 场景抹）：Agent 传输的 manifest fingerprint 是对「含 job_id 的
# 整份 payload」取 sha256（``translate.agent_translation_manifest_fingerprint`` 与
# ``semantic._agent_manifest_fingerprint``），B 场景下游的 polish progress / 语义
# checkpoint 又都复用这个值，因此全链路随 job id 变化。A 场景（API）的 fingerprint
# （``translation._translation_fingerprint``、``polish._fingerprint``、
# ``semantic._fingerprint``）payload 里既没有 job id 也没有路径，是纯内容寻址，所以
# A 场景不抹它：那些文件仍按原始字节冻结，fingerprint 另外再固化成字面值。
_AGENT_BLANKED_KEYS = frozenset({"job_id", "fingerprint"})
#
# 规则是自动的：文件里没有这些键时走原始字节 checksum（更严），出现时才退化为投影，
# 并把实际被抹掉的键记进 golden 的 ``normalized_keys``。某次重构若给新文件写入了
# job_id，``normalized_keys`` 随之变化，快照照样会红。


def _event_projection(records: list[Event]) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for record in records:
        item: dict[str, Any] = {"type": str(record.type)}
        for field in _EVENT_FIELDS:
            if field in record.data:
                value = record.data[field]
                item[field] = str(value) if isinstance(value, str) else value
        projected.append(item)
    return projected


def _blank_keys(node: Any, keys: frozenset[str], blanked: set[str]) -> Any:
    if isinstance(node, dict):
        result: dict[str, Any] = {}
        for key, value in node.items():
            if key in keys and isinstance(value, str):
                blanked.add(key)
                result[key] = f"<{key}>"
            else:
                result[key] = _blank_keys(value, keys, blanked)
        return result
    if isinstance(node, list):
        return [_blank_keys(item, keys, blanked) for item in node]
    return node


def _content_entry(path: Path, keys: frozenset[str]) -> dict[str, Any]:
    """返回该文件的内容 checksum；只有命中 ``keys`` 时才退化为规范化投影。"""

    if path.suffix == ".json":
        blanked: set[str] = set()
        projected = _blank_keys(json.loads(path.read_text(encoding="utf-8")), keys, blanked)
        if blanked:
            encoded = json.dumps(projected, ensure_ascii=False, separators=(",", ":")).encode()
            return {
                "checksum": hashlib.sha256(encoded).hexdigest(),
                "normalized_keys": sorted(blanked),
            }
    return {"checksum": file_checksum(path), "normalized_keys": []}


def _artifact_snapshot(job: Job, keys: frozenset[str]) -> list[dict[str, Any]]:
    """``job.artifacts`` 的有序 (kind, 文件名, 内容 checksum) 清单。绝不入绝对路径。"""

    return [
        {
            "kind": artifact.kind,
            "name": Path(artifact.path).name,
            **_content_entry(Path(artifact.path), keys),
        }
        for artifact in job.artifacts
    ]


def _artifact_path(job: Job, kind: str) -> Path:
    return next(Path(artifact.path) for artifact in job.artifacts if artifact.kind == kind)


def _fingerprint_of(job: Job, kind: str) -> str:
    data = json.loads(_artifact_path(job, kind).read_text(encoding="utf-8"))
    return str(data["fingerprint"])


def _manifest_fingerprint(payload: dict[str, Any]) -> str:
    """按 Agent manifest 的既有算法重新推导 fingerprint。

    这里刻意复刻 ``translate.agent_translation_manifest_fingerprint`` 的算法，而不是调用
    私有实现：B 场景的 fingerprint 因绑定 job id 无法固化成字面值，只能靠「文件自洽」这条
    不变量守住它的语义。算法一旦改变，本测试先红。
    """

    canonical = {key: value for key, value in payload.items() if key != "fingerprint"}
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()


def _fingerprint_links(job: Job) -> list[list[str]]:
    """按 fingerprint 取值把产物分组，冻结 fingerprint 在文件之间的传播关系。"""

    groups: dict[str, list[str]] = {}
    for artifact in job.artifacts:
        path = Path(artifact.path)
        if path.suffix != ".json":
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("fingerprint"), str):
            groups.setdefault(data["fingerprint"], []).append(path.name)
    return list(groups.values())


def _snapshot_job(
    root: Path,
    *,
    provider: str,
    translation_quality: str = "polished",
    pin_provider_identity: bool | None = None,
) -> Path:
    """固定源文的纯字幕 Job。转写阶段不接事件，本模块只冻结 provider 编排。

    ``pin_provider_identity`` 缺省时沿用 A / B 的口径（非 agent 就固化 base URL 与 model）。
    C / G 场景需要「Job 钉住了 openai-compatible 身份、但 provider 仍是 auto」这种真实形态，
    因此显式传 True。
    """

    pinned = provider != "agent" if pin_provider_identity is None else pin_provider_identity
    source = root / "source.srt"
    source.write_text(SOURCE_SRT, encoding="utf-8")
    job = run_subtitle_transcription(source, root / "output", language="en")
    set_workflow_config(
        job,
        WorkflowConfig(
            target_language="zh-CN",
            requested_outputs=("translated_subtitles", "bilingual_subtitles"),
            translation_quality=translation_quality,
            provider=provider,
            base_url=PROVIDER_BASE_URL if pinned else None,
            model=PROVIDER_MODEL if pinned else None,
            api_key_env=API_KEY_ENV,
            batch_size=BATCH_SIZE,
        ),
    )
    save_job(job)
    return get_job_file(job)


def _draft_translations(batch, _config, **_kwargs):
    """固定的 API 初译桩：按 cue id 回填 ``DRAFT_TEXTS``。"""

    return {item.id: DRAFT_TEXTS[item.id] for item in batch.items}


def _standard_api_translated_job(root: Path) -> Path:
    """标准质量 + API 初译已完成的纯字幕 Job（E / F 场景的共同前置）。

    前置不接 event_sink：重译场景要冻的是重译自己的事件序列，初译事件由 A / D 场景负责。
    """

    job_file = _snapshot_job(root, provider="openai-compatible", translation_quality="standard")
    with (
        patch.dict("os.environ", {API_KEY_ENV: "secret"}),
        patch("xvt.pipeline.translation.translate_batch", side_effect=_draft_translations),
    ):
        run_translation(
            job_file,
            target_language="zh-CN",
            provider="openai-compatible",
            base_url=PROVIDER_BASE_URL,
            model=PROVIDER_MODEL,
            api_key_env=API_KEY_ENV,
            batch_size=BATCH_SIZE,
            policy=TranslationPolicy(),
        )
    return job_file


def build_api_snapshot(root: Path) -> dict[str, Any]:
    """A 场景：API polished 全链路，初审返回一个可修复问题，恰好修复一次后复审通过。"""

    job_file = _snapshot_job(root, provider="openai-compatible")
    events: list[Event] = []
    review_calls: list[list[int]] = []

    def fake_translate(batch, _config, **_kwargs):
        return {item.id: DRAFT_TEXTS[item.id] for item in batch.items}

    def fake_polish(batch, _config, **_kwargs):
        return {item.id: POLISHED_TEXTS[item.id] for item in batch.items}

    def fake_review(batch, _config, **_kwargs):
        cue_ids = sorted(item.id for item in batch.items)
        review_calls.append(cue_ids)
        # 前 3 次是初审（3 个语义批次），之后是复审；复审一律返回空，使链路收口。
        if len(review_calls) <= 3 and cue_ids == [2, 3]:
            return (
                SemanticIssue(
                    "awkward_join",
                    (2, 3),
                    "review",
                    SEMANTIC_ISSUE_MESSAGE,
                    (
                        SemanticSuggestedItem(2, REPAIRED_TEXTS[2]),
                        SemanticSuggestedItem(3, REPAIRED_TEXTS[3]),
                    ),
                ),
            )
        return ()

    def fake_repair(batch, _config, **_kwargs):
        return {item.id: REPAIRED_TEXTS[item.id] for item in batch.items}

    with (
        patch.dict("os.environ", {API_KEY_ENV: "secret"}),
        patch("xvt.pipeline.translation.translate_batch", side_effect=fake_translate),
        patch("xvt.pipeline.polish.polish_batch", side_effect=fake_polish),
        patch("xvt.pipeline.semantic.semantic_review_batch", side_effect=fake_review),
        patch("xvt.pipeline.semantic.semantic_repair_batch", side_effect=fake_repair),
    ):
        draft = run_translation(
            job_file,
            target_language="zh-CN",
            provider="openai-compatible",
            base_url=PROVIDER_BASE_URL,
            model=PROVIDER_MODEL,
            api_key_env=API_KEY_ENV,
            batch_size=BATCH_SIZE,
            policy=TranslationPolicy(),
            translation_quality="polished",
            event_sink=events.append,
        )
        polish = run_translation_polish(job_file, event_sink=events.append)
        semantic = run_translation_semantic_review(job_file, event_sink=events.append)

    job = load_job(job_file)
    return {
        "modes": {
            "translation": draft.mode,
            "polish": polish.mode,
            "semantic": [semantic.status, semantic.mode],
        },
        "provider_calls": {"review_batches": review_calls},
        "events": _event_projection(events),
        "artifacts": _artifact_snapshot(job, _API_BLANKED_KEYS),
        # API 阶段的 fingerprint payload 里既没有 job id 也没有路径（见模块注释），
        # 所以可以安全固化成字面值。全部从落盘文件读出，不调私有函数。
        "fingerprints": {
            "translation": _fingerprint_of(job, "translation_progress"),
            "polish_batches": _fingerprint_of(job, "translation_polish_batches"),
            "polish_progress": _fingerprint_of(job, "translation_polish_progress"),
            "semantic_review": _fingerprint_of(job, "translation_semantic_qa"),
            "semantic_repair": _fingerprint_of(job, "translation_semantic_repair"),
            "semantic_final_review": _fingerprint_of(job, "translation_semantic_qa_final"),
        },
    }


def build_agent_snapshot(root: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """B 场景：Agent 传输 polished 全链路。

    第二个返回值是两份 Agent manifest 的在途 payload，只给自洽性断言用，不进 golden
    （它们含 job id）。
    """

    job_file = _snapshot_job(root, provider="agent")
    events: list[Event] = []

    translation = run_translation(
        job_file,
        target_language="zh-CN",
        provider="agent",
        batch_size=BATCH_SIZE,
        policy=TranslationPolicy(),
        translation_quality="polished",
        event_sink=events.append,
    )
    translation_manifest = json.loads(translation.paths[0].read_text(encoding="utf-8"))
    translation_response = root / "translate-and-polish-response.json"
    translation_response.write_text(
        json.dumps(
            {
                "fingerprint": translation_manifest["fingerprint"],
                "items": [{"id": cue_id, "text": text} for cue_id, text in POLISHED_TEXTS.items()],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    apply_agent_translation(
        job_file,
        translation_response,
        target_language="zh-CN",
        event_sink=events.append,
    )

    semantic = run_translation_semantic_review(job_file, event_sink=events.append)
    semantic_manifest_path = semantic.paths[0]
    semantic_manifest = json.loads(semantic_manifest_path.read_text(encoding="utf-8"))
    # apply 会原地覆盖这份 manifest，所以在途形态必须此刻取快照。
    semantic_manifest_at_awaiting = _content_entry(semantic_manifest_path, _AGENT_BLANKED_KEYS)
    repair_batch = next(
        batch
        for batch in semantic_manifest["batches"]
        if {item["id"] for item in batch["items"]} == {2, 3}
    )
    semantic_response = root / "semantic-response.json"
    semantic_response.write_text(
        json.dumps(
            {
                "fingerprint": semantic_manifest["fingerprint"],
                "initial_results": [
                    {
                        "batch": batch["id"],
                        "issues": (
                            [
                                {
                                    "category": "awkward_join",
                                    "cue_ids": [2, 3],
                                    "severity": "review",
                                    "message": SEMANTIC_ISSUE_MESSAGE,
                                    "suggested_items": [
                                        {"id": 2, "text": REPAIRED_TEXTS[2]},
                                        {"id": 3, "text": REPAIRED_TEXTS[3]},
                                    ],
                                }
                            ]
                            if batch["id"] == repair_batch["id"]
                            else []
                        ),
                    }
                    for batch in semantic_manifest["batches"]
                ],
                "repair_results": [
                    {
                        "batch": 1,
                        "items": [
                            {"id": 2, "text": REPAIRED_TEXTS[2]},
                            {"id": 3, "text": REPAIRED_TEXTS[3]},
                        ],
                    }
                ],
                "final_results": [
                    {"batch": batch["id"], "issues": []} for batch in semantic_manifest["batches"]
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    applied = apply_agent_semantic_review(job_file, semantic_response, event_sink=events.append)

    job = load_job(job_file)
    snapshot = {
        "modes": {
            "translation": translation.mode,
            "semantic_prepare": [semantic.status, semantic.mode],
            "semantic_apply": [applied.status, applied.mode],
        },
        "manifests": {
            "translate_and_polish_keys": list(translation_manifest),
            "semantic_agent_keys": list(semantic_manifest),
            "semantic_agent_at_awaiting": semantic_manifest_at_awaiting,
            "semantic_batches": [
                [batch["id"], [item["id"] for item in batch["items"]]]
                for batch in semantic_manifest["batches"]
            ],
        },
        "events": _event_projection(events),
        "artifacts": _artifact_snapshot(job, _AGENT_BLANKED_KEYS),
        # Agent 传输下所有 fingerprint 都由 manifest fingerprint 派生，而后者绑定 job id，
        # 无法固化成字面值；这里冻结它在文件之间的传播分组。
        "fingerprint_links": _fingerprint_links(job),
    }
    return snapshot, {
        "translate_and_polish": translation_manifest,
        "semantic_agent": semantic_manifest,
    }


def build_fallback_snapshot(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """C 场景：``provider="auto"`` 缺 API Key 时降级到 Agent 传输。

    第二个返回值是 ``translation-batches.json`` 的在途 payload，只给形状断言用：标准质量的
    这份 manifest 刻意不写 ``fingerprint``（只有 polished + await_input 才写），回填靠
    ``source_cues_checksum`` 把关，这条负向契约由测试单独断言。
    """

    job_file = _snapshot_job(
        root,
        provider="auto",
        translation_quality="standard",
        pin_provider_identity=True,
    )
    events: list[Event] = []

    def forbidden(*_args, **_kwargs):
        raise AssertionError("降级到 Agent 传输后不得再调用 provider")

    with (
        patch.dict("os.environ", MISSING_PROVIDER_ENV),
        patch("xvt.pipeline.translation.translate_batch", side_effect=forbidden),
    ):
        result = run_translation(
            job_file,
            target_language="zh-CN",
            provider="auto",
            api_key_env=API_KEY_ENV,
            batch_size=BATCH_SIZE,
            policy=TranslationPolicy(),
            event_sink=events.append,
        )

    job = load_job(job_file)
    manifest = json.loads(result.paths[0].read_text(encoding="utf-8"))
    workflow = get_workflow_config(job)
    assert workflow is not None
    return {
        "modes": {"translation": result.mode},
        # 降级原因与事件里的 reason 必须同时被冻：返回值对了而事件丢了是 F1 的原始缺口。
        "fallback_reason": result.fallback_reason,
        # 降级不得把半套 provider 身份写坏（见现有 test_agent_fallback_does_not_persist...）。
        "persisted_provider": [workflow.provider, workflow.base_url, workflow.model],
        "manifests": {
            "translation_batches_keys": list(manifest),
            "batches": [
                [batch["id"], [item["id"] for item in batch["items"]]]
                for batch in manifest["batches"]
            ],
        },
        "events": _event_projection(events),
        "artifacts": _artifact_snapshot(job, _API_BLANKED_KEYS),
    }, manifest


def build_progress_reset_snapshot(root: Path) -> dict[str, Any]:
    """D 场景：API 初译跑两趟，第二趟换 model 使指纹漂移、checkpoint 清零。"""

    job_file = _snapshot_job(root, provider="openai-compatible", translation_quality="standard")
    events: list[Event] = []

    def run(model: str):
        return run_translation(
            job_file,
            target_language="zh-CN",
            provider="openai-compatible",
            base_url=PROVIDER_BASE_URL,
            model=model,
            api_key_env=API_KEY_ENV,
            batch_size=BATCH_SIZE,
            policy=TranslationPolicy(),
            event_sink=events.append,
        )

    with (
        patch.dict("os.environ", {API_KEY_ENV: "secret"}),
        patch("xvt.pipeline.translation.translate_batch", side_effect=_draft_translations),
    ):
        first = run(PROVIDER_MODEL)
        first_fingerprint = _fingerprint_of(load_job(job_file), "translation_progress")
        second = run(PROVIDER_MODEL_NEXT)

    job = load_job(job_file)
    return {
        "modes": {"first": first.mode, "second": second.mode},
        "events": _event_projection(events),
        "artifacts": _artifact_snapshot(job, _API_BLANKED_KEYS),
        # 两趟的 translation 指纹都是纯内容寻址（payload 里没有 job id 与路径），可以固化成
        # 字面值；它们必须不同，否则 progress_reset 根本不会发射。
        "fingerprints": {
            "first_translation": first_fingerprint,
            "second_translation": _fingerprint_of(job, "translation_progress"),
        },
    }


def build_retranslate_api_snapshot(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """E 场景：API 分支局部重译，首个批次一次可重试传输失败。

    打桩点是 ``xvt.pipeline.translation.translate_batch``（见模块注释：重译复用
    ``translate_batch_with_retries`` 的默认 request），退避按现有重译测试的两个桩位钉死。
    """

    job_file = _standard_api_translated_job(root)
    events: list[Event] = []
    corrections: list[str | None] = []

    def flaky(batch, _config, *, correction=None, **_kwargs):
        corrections.append(correction)
        if len(corrections) == 1:
            raise TranslationProviderError("snapshot transport failure", retryable=True)
        return {item.id: RETRANSLATED_TEXTS[item.id] for item in batch.items}

    with (
        patch.dict("os.environ", {API_KEY_ENV: "secret"}),
        patch("xvt.pipeline.translation.translate_batch", side_effect=flaky),
        patch("xvt.pipeline.translation.time.sleep"),
        patch("xvt.pipeline.translation._backoff_delay", lambda base: float(base)),
    ):
        result = retranslate_cues(
            job_file,
            cue_ids=RETRANSLATE_CUE_IDS,
            target_language="zh-CN",
            provider="openai-compatible",
            base_url=PROVIDER_BASE_URL,
            model=PROVIDER_MODEL,
            api_key_env=API_KEY_ENV,
            max_retries=1,
            event_sink=events.append,
        )

    job = load_job(job_file)
    manifest_path = _artifact_path(job, "cue_retranslation_batches")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        "modes": {"retranslate": result.mode},
        # 传输失败意味着请求没被服务端消费，纠正提示词不得生效：两个批次加一次重试共三次
        # 调用，correction 全程为 None（ProviderResponseError 才会带纠正提示）。
        "provider_calls": {"corrections": corrections},
        "manifests": {
            "cue_retranslation_keys": list(manifest),
            "task": [manifest["task"], manifest["task_version"]],
            "batches": [
                [batch["id"], [item["id"] for item in batch["items"]]]
                for batch in manifest["batches"]
            ],
        },
        "events": _event_projection(events),
        "artifacts": _artifact_snapshot(job, _AGENT_BLANKED_KEYS),
        "fingerprint_links": _fingerprint_links(job),
        # 重译 manifest 的 fingerprint 绑定 job id（见 _AGENT_BLANKED_KEYS 的说明）而被抹掉，
        # 但初译 checkpoint 的指纹仍是纯内容寻址，单独固化成字面值补回这份严格度。
        "fingerprints": {"translation": _fingerprint_of(job, "translation_progress")},
    }, manifest


def build_retranslate_agent_snapshot(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """F 场景：Agent 分支局部重译 —— manifest → 等待 → ``apply_cue_retranslation``。"""

    job_file = _standard_api_translated_job(root)
    events: list[Event] = []

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Agent 分支的局部重译不得调用 provider")

    with patch("xvt.pipeline.translation.translate_batch", side_effect=forbidden):
        result = retranslate_cues(
            job_file,
            cue_ids=RETRANSLATE_CUE_IDS,
            target_language="zh-CN",
            provider="agent",
            event_sink=events.append,
        )
        manifest_path = result.paths[0]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        # 回填不会重写 manifest，但在途形态仍单独取一次，证明等待态落盘的就是这份内容。
        manifest_at_awaiting = _content_entry(manifest_path, _AGENT_BLANKED_KEYS)
        response = root / "cue-retranslation-user-response.json"
        response.write_text(
            json.dumps(
                {
                    "fingerprint": manifest["fingerprint"],
                    "items": [
                        {"id": cue_id, "text": RETRANSLATED_TEXTS[cue_id]}
                        for cue_id in RETRANSLATE_CUE_IDS
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        applied = apply_cue_retranslation(
            job_file,
            response,
            cue_ids=RETRANSLATE_CUE_IDS,
            target_language="zh-CN",
            event_sink=events.append,
        )

    job = load_job(job_file)
    return {
        "modes": {"retranslate": result.mode},
        "applied_names": [path.name for path in applied],
        "manifests": {
            "cue_retranslation_keys": list(manifest),
            "task": [manifest["task"], manifest["task_version"]],
            "cue_retranslation_at_awaiting": manifest_at_awaiting,
            "batches": [
                [batch["id"], [item["id"] for item in batch["items"]]]
                for batch in manifest["batches"]
            ],
        },
        "events": _event_projection(events),
        "artifacts": _artifact_snapshot(job, _AGENT_BLANKED_KEYS),
        "fingerprint_links": _fingerprint_links(job),
        "fingerprints": {"translation": _fingerprint_of(job, "translation_progress")},
    }, manifest


def build_polish_agent_snapshot(root: Path) -> dict[str, Any]:
    """G 场景：标准 draft 的 Job 上显式补跑润色，provider 落 agent。"""

    job_file = _snapshot_job(
        root,
        provider="auto",
        translation_quality="standard",
        pin_provider_identity=True,
    )
    with (
        patch.dict("os.environ", {API_KEY_ENV: "secret"}),
        patch("xvt.pipeline.translation.translate_batch", side_effect=_draft_translations),
    ):
        run_translation(
            job_file,
            target_language="zh-CN",
            provider="auto",
            api_key_env=API_KEY_ENV,
            batch_size=BATCH_SIZE,
            policy=TranslationPolicy(),
        )

    events: list[Event] = []

    def forbidden(*_args, **_kwargs):
        raise AssertionError("降级到 Agent 传输后不得再调用 provider")

    with (
        patch.dict("os.environ", MISSING_PROVIDER_ENV),
        patch("xvt.pipeline.polish.polish_batch", side_effect=forbidden),
    ):
        polish = run_translation_polish(job_file, event_sink=events.append)
        manifest = json.loads(polish.paths[0].read_text(encoding="utf-8"))
        manifest_at_awaiting = _content_entry(polish.paths[0], _API_BLANKED_KEYS)
        response = root / "polish-response.json"
        response.write_text(
            json.dumps(
                {
                    "fingerprint": manifest["fingerprint"],
                    "items": [
                        {"id": cue_id, "text": text} for cue_id, text in POLISHED_TEXTS.items()
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        applied = apply_agent_translation_polish(job_file, response, event_sink=events.append)

    job = load_job(job_file)
    return {
        "modes": {"polish": polish.mode},
        # polish 侧不走 _fallback_reason_with_persisted_provider，文案比 C 场景短一截。
        "fallback_reason": polish.fallback_reason,
        "applied_names": [path.name for path in applied],
        "manifests": {
            "translation_polish_keys": list(manifest),
            "translation_polish_at_awaiting": manifest_at_awaiting,
            "batches": [
                [batch["id"], [item["id"] for item in batch["items"]]]
                for batch in manifest["batches"]
            ],
        },
        "events": _event_projection(events),
        "artifacts": _artifact_snapshot(job, _API_BLANKED_KEYS),
        # polish 的 fingerprint payload 同样是纯内容寻址（见 polish._fingerprint），可固化。
        "fingerprints": {
            "polish_batches": _fingerprint_of(job, "translation_polish_batches"),
            "polish_progress": _fingerprint_of(job, "translation_polish_progress"),
        },
    }


def _golden() -> dict[str, Any]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))["scenarios"]


class SnapshotScopeTest(unittest.TestCase):
    """快照覆盖面本身的回归保护：它量不到的东西，不该被当成它在守。

    2026-11-01 的 E2 方案一度写着「往 layout 加字段必然改 artifact checksum、必然要
    重新生成 golden」，实际不成立：这里全部场景都是**纯字幕 Job**，没有画幅，
    `check_subtitles` 的 layout 块只在有画幅时构建（qa.py），所以 golden 里根本没有
    layout。照那个错误前提做，就会在一次本不该动 golden 的改动里动它。

    这条把"快照量不到 layout"钉成显式期望：哪天场景里加进带画幅的 Job，它会变红，
    提醒改动者重新判断 layout 相关改动对协议快照的影响。
    """

    def test_scenarios_are_subtitle_only_so_layout_never_reaches_golden(self) -> None:
        golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
        kinds = set()

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                kind = node.get("kind")
                if isinstance(kind, str):
                    kinds.add(kind)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(golden)
        self.assertTrue(kinds, "golden 里应当有 artifact kind，扫不到说明结构变了")
        media_kinds = sorted(
            kind for kind in kinds if any(word in kind for word in ("media", "video", "audio"))
        )
        self.assertEqual(
            media_kinds,
            [],
            "快照场景出现了带媒体的 Job：layout 块会随之进入 golden，"
            "layout 相关改动对协议快照的影响需要重新判断",
        )
        self.assertNotIn("layout", GOLDEN_PATH.read_text(encoding="utf-8"))


class ProviderProtocolSnapshotTest(unittest.TestCase):
    maxDiff = None

    def test_api_polished_snapshot_matches_golden(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = build_api_snapshot(Path(directory))

        self.assertEqual(snapshot, _golden()["api_polished"])

    def test_agent_polished_snapshot_matches_golden(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot, manifests = build_agent_snapshot(Path(directory))

        self.assertEqual(snapshot, _golden()["agent_polished"])
        # fingerprint 字面值不可冻（绑定 job id），改为冻结自洽性：两份 manifest 的
        # fingerprint 必须等于「整份 payload 去掉 fingerprint 后」的规范 sha256。
        for name, payload in manifests.items():
            with self.subTest(manifest=name):
                self.assertEqual(payload["fingerprint"], _manifest_fingerprint(payload))

    def test_standard_agent_fallback_snapshot_matches_golden(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot, manifest = build_fallback_snapshot(Path(directory))

        self.assertEqual(snapshot, _golden()["standard_agent_fallback"])
        # 标准 manifest 刻意不写 fingerprint（只有 polished + await_input 才写），回填靠
        # source_cues_checksum 把关。这条形状契约被改掉就等于换了一套回填校验口径。
        self.assertNotIn("fingerprint", manifest)

    def test_api_progress_reset_snapshot_matches_golden(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = build_progress_reset_snapshot(Path(directory))

        self.assertEqual(snapshot, _golden()["api_progress_reset"])
        # 两趟指纹必须不同，否则 translation_progress_reset 根本不会发射，这个场景就空了。
        self.assertNotEqual(
            snapshot["fingerprints"]["first_translation"],
            snapshot["fingerprints"]["second_translation"],
        )

    def test_retranslate_api_snapshot_matches_golden(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot, manifest = build_retranslate_api_snapshot(Path(directory))

        self.assertEqual(snapshot, _golden()["retranslate_api"])
        # 重译 manifest 的 fingerprint 绑定 job id，同 B 场景只能冻自洽性。
        self.assertEqual(manifest["fingerprint"], _manifest_fingerprint(manifest))

    def test_retranslate_agent_snapshot_matches_golden(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot, manifest = build_retranslate_agent_snapshot(Path(directory))

        self.assertEqual(snapshot, _golden()["retranslate_agent"])
        self.assertEqual(manifest["fingerprint"], _manifest_fingerprint(manifest))

    def test_polish_agent_rerun_snapshot_matches_golden(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = build_polish_agent_snapshot(Path(directory))

        self.assertEqual(snapshot, _golden()["polish_agent_rerun"])

    def test_prompt_version_change_breaks_the_snapshot(self) -> None:
        """负向对照：动一个参与 fingerprint 的 prompt version，快照必须红。

        ``TRANSLATION_PROMPT_VERSION`` 是 ``from xvt.config import ...`` 进来的按值绑定，
        因此必须 patch 使用处模块的名字。
        """

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("xvt.pipeline.translation.TRANSLATION_PROMPT_VERSION", 9999),
        ):
            snapshot = build_api_snapshot(Path(directory))

        golden = _golden()["api_polished"]
        self.assertNotEqual(
            snapshot["fingerprints"]["translation"],
            golden["fingerprints"]["translation"],
        )
        batches = next(
            item for item in snapshot["artifacts"] if item["kind"] == "translation_batches"
        )
        golden_batches = next(
            item for item in golden["artifacts"] if item["kind"] == "translation_batches"
        )
        self.assertNotEqual(batches["checksum"], golden_batches["checksum"])
        with self.assertRaises(AssertionError):
            self.assertEqual(snapshot, golden)


if __name__ == "__main__":
    unittest.main()

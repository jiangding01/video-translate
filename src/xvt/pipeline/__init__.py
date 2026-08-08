"""pipeline 包：stage 推进、artifact 登记与各业务主链的公共入口门面。

本文件只做 re-export，不含业务逻辑。子模块职责与依赖方向（无环）：

- ``common``：输入探测、stage 记账、原子 JSON 写入、artifact 登记与失效。
- ``review``：字幕 QA ``needs_review`` 的人工确认通道。
- ``revision``：人工源文/译文修订与严格回填。
- ``transcription``：素材获取、音频规范化、Whisper 转写、分段、对齐与恢复。
- ``translation``：翻译批次、兼容 API 调用、Agent 回填、导入译文与统一收尾。
- ``polish``：已有译文的可恢复 API/Agent 润色（依赖 ``translation`` 的收尾）。
- ``retranslate``：指定 cue 局部重译（依赖 ``translation`` 的收尾）。
- ``resegment``：超宽 ASR cue 语义重分段（仅依赖 ``common``）。
- ``burnin``：短片段预览与正式烧录、媒体 QA。
"""

from __future__ import annotations

from xvt.pipeline.burnin import burn_translated_job, preview_translated_job
from xvt.pipeline.common import inspect_input, interrupt_running_stage, is_subtitle_input
from xvt.pipeline.polish import (
    agent_translation_polish_manifest_valid,
    apply_agent_translation_polish,
    run_atomic_translation_polish,
    run_translation_polish,
    translation_polish_satisfied,
)
from xvt.pipeline.resegment import ResegmentationResult, resegment_translation
from xvt.pipeline.retranslate import apply_cue_retranslation, retranslate_cues
from xvt.pipeline.review import (
    acknowledge_subtitle_review,
    acknowledge_translation_review,
    finalize_acknowledged_subtitle_review,
)
from xvt.pipeline.revision import (
    REVISION_FINGERPRINT_LENGTH,
    TRANSLATION_REVIEW_STEM_PREFIX,
    apply_source_review_fixes,
    apply_translation_review_fixes,
    ensure_semantic_review_inputs,
    ensure_translation_review_input,
    prepare_source_review,
    source_review_input_path,
    source_review_supported,
    source_suspect_cue_ids,
    translation_review_input_path,
)
from xvt.pipeline.semantic import (
    SemanticRunResult,
    agent_semantic_review_waiting,
    apply_agent_semantic_review,
    complete_acknowledged_semantic_review,
    prepare_agent_semantic_review,
    run_translation_semantic_review,
    semantic_review_approved,
    semantic_review_satisfied,
    semantic_review_waiting,
)
from xvt.pipeline.transcription import (
    RebuildSourceResult,
    rebuild_source,
    resume_job,
    resume_remote_transcription,
    run_local_transcription,
    run_remote_transcription,
    run_subtitle_transcription,
    run_transcription,
)
from xvt.pipeline.translation import (
    TranslationRunResult,
    agent_translation_polish_core_satisfied,
    agent_translation_polish_progress_ready,
    agent_translation_polish_started,
    agent_translation_polish_waiting,
    apply_agent_translation,
    apply_subtitle_translation,
    complete_agent_polished_outputs,
    complete_translated_outputs,
    prepare_agent_translation,
    run_translation,
)

__all__ = [
    "REVISION_FINGERPRINT_LENGTH",
    "TRANSLATION_REVIEW_STEM_PREFIX",
    "ResegmentationResult",
    "RebuildSourceResult",
    "SemanticRunResult",
    "TranslationRunResult",
    "acknowledge_subtitle_review",
    "acknowledge_translation_review",
    "agent_translation_polish_core_satisfied",
    "agent_translation_polish_manifest_valid",
    "agent_translation_polish_progress_ready",
    "agent_translation_polish_started",
    "agent_translation_polish_waiting",
    "apply_agent_translation",
    "apply_agent_semantic_review",
    "apply_agent_translation_polish",
    "apply_cue_retranslation",
    "apply_subtitle_translation",
    "apply_source_review_fixes",
    "apply_translation_review_fixes",
    "burn_translated_job",
    "complete_agent_polished_outputs",
    "complete_acknowledged_semantic_review",
    "complete_translated_outputs",
    "ensure_semantic_review_inputs",
    "finalize_acknowledged_subtitle_review",
    "inspect_input",
    "ensure_translation_review_input",
    "interrupt_running_stage",
    "is_subtitle_input",
    "prepare_agent_translation",
    "prepare_agent_semantic_review",
    "prepare_source_review",
    "preview_translated_job",
    "rebuild_source",
    "resegment_translation",
    "resume_job",
    "resume_remote_transcription",
    "retranslate_cues",
    "run_atomic_translation_polish",
    "run_local_transcription",
    "run_remote_transcription",
    "run_subtitle_transcription",
    "run_transcription",
    "run_translation",
    "run_translation_semantic_review",
    "run_translation_polish",
    "translation_polish_satisfied",
    "translation_review_input_path",
    "semantic_review_approved",
    "semantic_review_satisfied",
    "semantic_review_waiting",
    "source_review_input_path",
    "source_review_supported",
    "source_suspect_cue_ids",
    "agent_semantic_review_waiting",
]

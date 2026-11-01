"""pipeline 包：stage 推进、artifact 登记与各业务主链的公共入口门面。

本文件只做 re-export，不含业务逻辑。子模块职责与依赖方向（无环）：

- ``common``：输入探测、stage 记账、原子 JSON 写入、artifact 登记与失效。
- ``context``：源 cue 的确定性翻译上下文候选包（仅依赖 ``common``）。
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
from xvt.pipeline.common import (
    add_artifact,
    artifact_checksum_for,
    error_category,
    inspect_input,
    interrupt_running_stage,
    is_subtitle_input,
    pending_translation_import_blocker,
    resolved_translation_policy,
)
from xvt.pipeline.context import TranslationContextResult, build_translation_context
from xvt.pipeline.polish import (
    agent_translation_polish_manifest_valid,
    apply_agent_translation_polish,
    run_atomic_translation_polish,
    run_translation_polish,
    translation_polish_satisfied,
)
from xvt.pipeline.resegment import ResegmentationResult, resegment_translation
from xvt.pipeline.retranslate import (
    apply_cue_retranslation,
    cue_retranslation_request,
    cue_retranslation_waiting,
    retranslate_cues,
)
from xvt.pipeline.review import (
    acknowledge_subtitle_review,
    acknowledge_translation_review,
    finalize_acknowledged_subtitle_review,
)
from xvt.pipeline.revision import (
    REVISION_FINGERPRINT_LENGTH,
    SOURCE_REVIEW_STEM_PREFIX,
    TRANSLATION_REVIEW_STEM_PREFIX,
    SourceRevisionResult,
    apply_source_review_fixes,
    apply_translation_review_fixes,
    ensure_semantic_review_inputs,
    ensure_translation_review_input,
    preflight_source_review_fixes,
    preflight_translation_review_fixes,
    prepare_source_review,
    semantic_review_issue_summaries,
    source_review_input_path,
    source_review_stem,
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
    source_selection_matches,
)
from xvt.pipeline.translation import (
    REPAIR_POLISH_DRAFT_MANUAL_FIX_MESSAGE,
    DroppedTranslationImport,
    TranslationRunResult,
    agent_translate_and_polish_core_satisfied,
    agent_translate_and_polish_progress_ready,
    agent_translate_and_polish_started,
    agent_translate_and_polish_waiting,
    apply_agent_translation,
    apply_subtitle_translation,
    complete_agent_polished_outputs,
    complete_translated_outputs,
    drop_imported_translation,
    prepare_agent_translation,
    run_translation,
    translation_progress_rebuildable,
)

__all__ = [
    "REPAIR_POLISH_DRAFT_MANUAL_FIX_MESSAGE",
    "REVISION_FINGERPRINT_LENGTH",
    "SOURCE_REVIEW_STEM_PREFIX",
    "TRANSLATION_REVIEW_STEM_PREFIX",
    "DroppedTranslationImport",
    "ResegmentationResult",
    "RebuildSourceResult",
    "SemanticRunResult",
    "TranslationRunResult",
    "acknowledge_subtitle_review",
    "acknowledge_translation_review",
    "add_artifact",
    "agent_translate_and_polish_core_satisfied",
    "agent_translate_and_polish_progress_ready",
    "agent_translate_and_polish_started",
    "agent_translate_and_polish_waiting",
    "agent_translation_polish_manifest_valid",
    "apply_agent_translation",
    "artifact_checksum_for",
    "apply_agent_semantic_review",
    "apply_agent_translation_polish",
    "apply_cue_retranslation",
    "apply_subtitle_translation",
    "SourceRevisionResult",
    "apply_source_review_fixes",
    "apply_translation_review_fixes",
    "TranslationContextResult",
    "build_translation_context",
    "burn_translated_job",
    "complete_agent_polished_outputs",
    "complete_acknowledged_semantic_review",
    "complete_translated_outputs",
    "drop_imported_translation",
    "ensure_semantic_review_inputs",
    "finalize_acknowledged_subtitle_review",
    "inspect_input",
    "ensure_translation_review_input",
    "error_category",
    "interrupt_running_stage",
    "is_subtitle_input",
    "pending_translation_import_blocker",
    "prepare_agent_translation",
    "prepare_agent_semantic_review",
    "prepare_source_review",
    "preview_translated_job",
    "preflight_source_review_fixes",
    "preflight_translation_review_fixes",
    "rebuild_source",
    "resegment_translation",
    "resolved_translation_policy",
    "resume_job",
    "resume_remote_transcription",
    "cue_retranslation_request",
    "cue_retranslation_waiting",
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
    "translation_progress_rebuildable",
    "translation_review_input_path",
    "semantic_review_approved",
    "semantic_review_satisfied",
    "semantic_review_waiting",
    "source_review_input_path",
    "source_review_stem",
    "source_review_supported",
    "source_selection_matches",
    "semantic_review_issue_summaries",
    "source_suspect_cue_ids",
    "agent_semantic_review_waiting",
]

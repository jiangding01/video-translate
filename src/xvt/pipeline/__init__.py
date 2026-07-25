"""pipeline 包：stage 推进、artifact 登记与各业务主链的公共入口门面。

本文件只做 re-export，不含业务逻辑。子模块职责与依赖方向（无环）：

- ``common``：输入探测、stage 记账、原子 JSON 写入、artifact 登记与失效。
- ``review``：字幕 QA ``needs_review`` 的人工确认通道。
- ``transcription``：素材获取、音频规范化、Whisper 转写、分段、对齐与恢复。
- ``translation``：翻译批次、兼容 API 调用、Agent 回填、导入译文与统一收尾。
- ``retranslate``：指定 cue 局部重译（依赖 ``translation`` 的收尾）。
- ``resegment``：超宽 ASR cue 语义重分段（仅依赖 ``common``）。
- ``burnin``：短片段预览与正式烧录、媒体 QA。
"""

from __future__ import annotations

from xvt.pipeline.burnin import burn_translated_job, preview_translated_job
from xvt.pipeline.common import inspect_input, interrupt_running_stage, is_subtitle_input
from xvt.pipeline.resegment import ResegmentationResult, resegment_translation
from xvt.pipeline.retranslate import apply_cue_retranslation, retranslate_cues
from xvt.pipeline.review import acknowledge_subtitle_review
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
    apply_agent_translation,
    apply_subtitle_translation,
    complete_translated_outputs,
    prepare_agent_translation,
    run_translation,
)

__all__ = [
    "ResegmentationResult",
    "RebuildSourceResult",
    "TranslationRunResult",
    "acknowledge_subtitle_review",
    "apply_agent_translation",
    "apply_cue_retranslation",
    "apply_subtitle_translation",
    "burn_translated_job",
    "complete_translated_outputs",
    "inspect_input",
    "interrupt_running_stage",
    "is_subtitle_input",
    "prepare_agent_translation",
    "preview_translated_job",
    "rebuild_source",
    "resegment_translation",
    "resume_job",
    "resume_remote_transcription",
    "retranslate_cues",
    "run_local_transcription",
    "run_remote_transcription",
    "run_subtitle_transcription",
    "run_transcription",
    "run_translation",
]

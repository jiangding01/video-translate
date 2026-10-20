"""转写主链：素材获取、音频规范化、Whisper 转写、分段、对齐与转写恢复。

覆盖本地媒体、纯字幕输入与远程（YouTube）三条获取路径，以及 `resume`
使用的规范化音频 / 词级转写 / 源 cue checkpoint 恢复逻辑。

2026-10-20 按 AGENTS.md「长函数治理约定（F13）」拆成子包，本模块**仅 re-export**：

- ``shared``：三簇共用的记账与只读 helper（零兄弟依赖的叶子）
- ``local``：本地输入腿（本地媒体自适应转写、ASR 机制、纯字幕输入 Job）
- ``remote``：远程获取腿（探测、字幕与媒体下载、获取后转写）
- ``resume``：``resume`` 与 ``rebuild-source``
- ``dispatch``：``run_transcription`` 的输入类型分派

包内依赖方向：``shared ← local ← remote ← resume``，``dispatch ← {local, remote}``，
由 ``tests/test_pipeline_imports.py`` 的白名单守卫。
"""

from __future__ import annotations

from xvt.pipeline.transcription.dispatch import run_transcription
from xvt.pipeline.transcription.local import run_local_transcription, run_subtitle_transcription
from xvt.pipeline.transcription.remote import (
    resume_remote_transcription,
    run_remote_transcription,
)
from xvt.pipeline.transcription.resume import rebuild_source, resume_job
from xvt.pipeline.transcription.shared import RebuildSourceResult, source_selection_matches

__all__ = [
    "RebuildSourceResult",
    "rebuild_source",
    "resume_job",
    "resume_remote_transcription",
    "run_local_transcription",
    "run_remote_transcription",
    "run_subtitle_transcription",
    "run_transcription",
    "source_selection_matches",
]

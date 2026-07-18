"""Stage 计划表：给定输入组合，声明转写阶段会跳过哪些 stage。

这是「哪些 stage 在什么条件下执行」的单一事实来源。入口在业务逻辑开始前
一次性把计划中的跳过项标记为 SKIPPED（先计划、后执行），取代散落在各分支
里的逐个记账。后续翻译 / 烧录目标会把被标记为 SKIPPED 的 TRANSLATE、BURN、
MEDIA_QA 重新置为 RUNNING，这与既有语义一致。
"""

from __future__ import annotations

from xvt.models import StageName

# 原子转写完成即收尾的 stage：没有翻译/烧录目标时始终标记为跳过，
# 由 workflow 的后续步骤按需重新激活。
TRAILING_TRANSCRIPTION_SKIPS = (
    StageName.TRANSLATE,
    StageName.BURN,
    StageName.MEDIA_QA,
)

# 纯字幕输入没有媒体：媒体探测、素材获取、音频与 ASR、对齐、分段全部跳过。
SUBTITLE_INPUT_SKIPS = (
    StageName.INSPECT,
    StageName.ACQUIRE,
    StageName.NORMALIZE_AUDIO,
    StageName.TRANSCRIBE,
    StageName.ALIGN,
    StageName.SEGMENT,
)


def media_transcription_skips(
    *,
    has_source_subtitles: bool,
    align_subtitles: bool,
) -> tuple[StageName, ...]:
    """媒体输入在 IMPORT_SUBTITLES 到 SEGMENT 区间会跳过的 stage。

    - 无源字幕：跳过导入与对齐，走 ASR + 分段。
    - 有源字幕且不对齐：跳过音频、ASR、对齐与分段，完全保留用户时间轴。
    - 有源字幕且请求对齐：额外运行 ASR 与对齐，仅跳过分段。
    """
    if not has_source_subtitles:
        return (StageName.IMPORT_SUBTITLES, StageName.ALIGN)
    if align_subtitles:
        return (StageName.SEGMENT,)
    return (
        StageName.NORMALIZE_AUDIO,
        StageName.TRANSCRIBE,
        StageName.ALIGN,
        StageName.SEGMENT,
    )

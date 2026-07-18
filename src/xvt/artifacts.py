"""Artifact 注册表与依赖图。

本模块是 artifact 依赖关系与失效闭包的单一来源。所有产物种类在 ``ArtifactKind``
中登记，产物之间的直接派生关系记录在 ``DERIVES_FROM``（下游 -> 直接上游）。
``downstream_of`` 沿依赖图反向计算传递闭包，供 ``pipeline`` 推导失效集合，
避免在多处手写并保持同步易错的硬编码集合。
"""

from __future__ import annotations

from enum import StrEnum


class ArtifactKind(StrEnum):
    """所有 artifact 种类。值即为持久化到 job 中的 kind 字符串。"""

    REMOTE_INFO = "remote_info"
    DOWNLOADED_MEDIA = "downloaded_media"
    DOWNLOADED_SUBTITLES = "downloaded_subtitles"
    SOURCE_VIDEO = "source_video"
    MEDIA_INFO = "media_info"
    INPUT_SUBTITLES = "input_subtitles"
    INPUT_TRANSLATION_SUBTITLES = "input_translation_subtitles"
    NORMALIZED_AUDIO = "normalized_audio"
    WORD_TRANSCRIPT = "word_transcript"
    SUBTITLE_ALIGNMENT = "subtitle_alignment"
    SOURCE_CUES = "source_cues"
    SOURCE_SRT = "source_srt"
    SUBTITLE_QA = "subtitle_qa"
    TRANSLATION_RESEGMENTATION = "translation_resegmentation"
    TRANSLATION_BATCHES = "translation_batches"
    TRANSLATION_MAPPING = "translation_mapping"
    TRANSLATION_PROGRESS = "translation_progress"
    TRANSLATION_RESPONSE = "translation_response"
    CUE_RETRANSLATION_BATCHES = "cue_retranslation_batches"
    CUE_RETRANSLATION_RESPONSE = "cue_retranslation_response"
    TRANSLATED_CUES = "translated_cues"
    TRANSLATED_SRT = "translated_srt"
    TRANSLATED_SUBTITLE_QA = "translated_subtitle_qa"
    BILINGUAL_SRT = "bilingual_srt"
    BILINGUAL_ASS = "bilingual_ass"
    BURNED_VIDEO = "burned_video"
    MEDIA_QA = "media_qa"
    PREVIEW_ASS = "preview_ass"
    PREVIEW_VIDEO = "preview_video"


# 下游 artifact -> 直接上游 artifact 集合。根节点（无派生来源的输入或用户提供的产物）
# 映射为空集。失效闭包沿本表反向推导。
DERIVES_FROM: dict[ArtifactKind, frozenset[ArtifactKind]] = {
    # 根节点：外部输入或用户提供，不派生自其他 artifact。
    ArtifactKind.REMOTE_INFO: frozenset(),
    ArtifactKind.MEDIA_INFO: frozenset(),
    ArtifactKind.INPUT_SUBTITLES: frozenset(),
    ArtifactKind.INPUT_TRANSLATION_SUBTITLES: frozenset(),
    ArtifactKind.TRANSLATION_RESEGMENTATION: frozenset(),
    # 下载与源媒体。
    ArtifactKind.DOWNLOADED_MEDIA: frozenset({ArtifactKind.REMOTE_INFO}),
    ArtifactKind.DOWNLOADED_SUBTITLES: frozenset({ArtifactKind.REMOTE_INFO}),
    ArtifactKind.SOURCE_VIDEO: frozenset({ArtifactKind.REMOTE_INFO}),
    # 转写链。
    ArtifactKind.NORMALIZED_AUDIO: frozenset(
        {
            ArtifactKind.SOURCE_VIDEO,
            ArtifactKind.DOWNLOADED_MEDIA,
            ArtifactKind.MEDIA_INFO,
        }
    ),
    ArtifactKind.WORD_TRANSCRIPT: frozenset({ArtifactKind.NORMALIZED_AUDIO}),
    ArtifactKind.SUBTITLE_ALIGNMENT: frozenset(
        {ArtifactKind.WORD_TRANSCRIPT, ArtifactKind.INPUT_SUBTITLES}
    ),
    ArtifactKind.SOURCE_CUES: frozenset(
        {
            ArtifactKind.WORD_TRANSCRIPT,
            ArtifactKind.INPUT_SUBTITLES,
            ArtifactKind.SUBTITLE_ALIGNMENT,
            ArtifactKind.TRANSLATION_RESEGMENTATION,
            ArtifactKind.DOWNLOADED_SUBTITLES,
        }
    ),
    ArtifactKind.SOURCE_SRT: frozenset({ArtifactKind.SOURCE_CUES}),
    ArtifactKind.SUBTITLE_QA: frozenset({ArtifactKind.SOURCE_CUES}),
    # 翻译链。
    ArtifactKind.TRANSLATION_BATCHES: frozenset({ArtifactKind.SOURCE_CUES}),
    ArtifactKind.TRANSLATION_MAPPING: frozenset(
        {ArtifactKind.SOURCE_CUES, ArtifactKind.INPUT_TRANSLATION_SUBTITLES}
    ),
    ArtifactKind.TRANSLATION_PROGRESS: frozenset({ArtifactKind.TRANSLATION_BATCHES}),
    ArtifactKind.TRANSLATION_RESPONSE: frozenset({ArtifactKind.TRANSLATION_BATCHES}),
    ArtifactKind.CUE_RETRANSLATION_BATCHES: frozenset({ArtifactKind.SOURCE_CUES}),
    ArtifactKind.CUE_RETRANSLATION_RESPONSE: frozenset({ArtifactKind.CUE_RETRANSLATION_BATCHES}),
    ArtifactKind.TRANSLATED_CUES: frozenset(
        {
            ArtifactKind.SOURCE_CUES,
            ArtifactKind.TRANSLATION_RESPONSE,
            ArtifactKind.TRANSLATION_MAPPING,
            ArtifactKind.TRANSLATION_PROGRESS,
            ArtifactKind.CUE_RETRANSLATION_RESPONSE,
        }
    ),
    # 渲染与烧录链。
    ArtifactKind.TRANSLATED_SUBTITLE_QA: frozenset({ArtifactKind.TRANSLATED_CUES}),
    ArtifactKind.TRANSLATED_SRT: frozenset({ArtifactKind.TRANSLATED_CUES}),
    ArtifactKind.BILINGUAL_SRT: frozenset({ArtifactKind.TRANSLATED_CUES}),
    ArtifactKind.BILINGUAL_ASS: frozenset({ArtifactKind.TRANSLATED_CUES}),
    ArtifactKind.BURNED_VIDEO: frozenset({ArtifactKind.BILINGUAL_ASS}),
    ArtifactKind.MEDIA_QA: frozenset({ArtifactKind.BURNED_VIDEO}),
    # 预览链。
    ArtifactKind.PREVIEW_ASS: frozenset({ArtifactKind.TRANSLATED_CUES}),
    ArtifactKind.PREVIEW_VIDEO: frozenset({ArtifactKind.PREVIEW_ASS}),
}


def downstream_of(*roots: ArtifactKind) -> frozenset[ArtifactKind]:
    """返回沿 ``DERIVES_FROM`` 反向可达的全部下游 artifact（不含 ``roots`` 自身）。

    换言之：所有（直接或间接）派生自任一 root 的产物集合。
    """

    root_set = set(roots)
    downstream: set[ArtifactKind] = set()
    changed = True
    while changed:
        changed = False
        for kind, upstreams in DERIVES_FROM.items():
            if kind in downstream:
                continue
            if upstreams & (root_set | downstream):
                downstream.add(kind)
                changed = True
    downstream -= root_set
    return frozenset(downstream)

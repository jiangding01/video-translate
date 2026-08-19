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
    SOURCE_CANDIDATE_QA = "source_candidate_qa"
    SOURCE_CUES = "source_cues"
    SOURCE_SELECTION = "source_selection"
    SOURCE_SRT = "source_srt"
    SUBTITLE_QA = "subtitle_qa"
    TRANSLATION_RESEGMENTATION = "translation_resegmentation"
    SOURCE_REVIEW_REVISION = "source_review_revision"
    TRANSLATION_BATCHES = "translation_batches"
    TRANSLATION_MAPPING = "translation_mapping"
    TRANSLATION_PROGRESS = "translation_progress"
    TRANSLATION_RESPONSE = "translation_response"
    CUE_RETRANSLATION_BATCHES = "cue_retranslation_batches"
    CUE_RETRANSLATION_RESPONSE = "cue_retranslation_response"
    TRANSLATION_DRAFT_CUES = "translation_draft_cues"
    TRANSLATION_POLISH_BATCHES = "translation_polish_batches"
    TRANSLATION_POLISH_PROGRESS = "translation_polish_progress"
    TRANSLATION_POLISHED_CUES = "translation_polished_cues"
    TRANSLATION_CANDIDATE_QA = "translation_candidate_qa"
    TRANSLATION_SEMANTIC_QA = "translation_semantic_qa"
    TRANSLATION_SEMANTIC_REPAIR = "translation_semantic_repair"
    TRANSLATION_REPAIRED_CUES = "translation_repaired_cues"
    TRANSLATION_SEMANTIC_QA_FINAL = "translation_semantic_qa_final"
    TRANSLATION_REVIEW_REVISION = "translation_review_revision"
    TRANSLATION_REVIEWED_CUES = "translation_reviewed_cues"
    TRANSLATION_SEMANTIC_QA_REVIEWED = "translation_semantic_qa_reviewed"
    TRANSLATED_CUES = "translated_cues"
    TRANSLATED_SRT = "translated_srt"
    TRANSLATED_SUBTITLE_QA = "translated_subtitle_qa"
    BILINGUAL_SRT = "bilingual_srt"
    BILINGUAL_ASS = "bilingual_ass"
    BURNED_VIDEO = "burned_video"
    BURN_FINGERPRINT = "burn_fingerprint"
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
    ArtifactKind.SOURCE_REVIEW_REVISION: frozenset(),
    ArtifactKind.TRANSLATION_REVIEW_REVISION: frozenset(),
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
    # 源字幕候选质量报告：本地/远程字幕及媒体边界共同决定候选是否可采用。
    ArtifactKind.SOURCE_CANDIDATE_QA: frozenset(
        {
            ArtifactKind.INPUT_SUBTITLES,
            ArtifactKind.DOWNLOADED_SUBTITLES,
            ArtifactKind.MEDIA_INFO,
        }
    ),
    ArtifactKind.SOURCE_CUES: frozenset(
        {
            ArtifactKind.WORD_TRANSCRIPT,
            ArtifactKind.INPUT_SUBTITLES,
            ArtifactKind.SUBTITLE_ALIGNMENT,
            ArtifactKind.TRANSLATION_RESEGMENTATION,
            ArtifactKind.DOWNLOADED_SUBTITLES,
            ArtifactKind.SOURCE_CANDIDATE_QA,
            ArtifactKind.SOURCE_REVIEW_REVISION,
        }
    ),
    # 选择报告在最终 source cues 写入后登记，记录可校验的最终选择与可选对齐结果。
    ArtifactKind.SOURCE_SELECTION: frozenset(
        {
            ArtifactKind.SOURCE_CUES,
            ArtifactKind.SOURCE_CANDIDATE_QA,
            ArtifactKind.SUBTITLE_ALIGNMENT,
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
    # Manifest 另以 fingerprint 绑定当前 translated_cues；不能在图中反向依赖它，
    # 否则会与 batches -> response -> translated_cues 形成环。回填时会重建并校验 manifest。
    ArtifactKind.CUE_RETRANSLATION_BATCHES: frozenset({ArtifactKind.SOURCE_CUES}),
    ArtifactKind.CUE_RETRANSLATION_RESPONSE: frozenset({ArtifactKind.CUE_RETRANSLATION_BATCHES}),
    ArtifactKind.TRANSLATION_DRAFT_CUES: frozenset(
        {
            ArtifactKind.SOURCE_CUES,
            ArtifactKind.TRANSLATION_RESPONSE,
            ArtifactKind.TRANSLATION_MAPPING,
            ArtifactKind.TRANSLATION_PROGRESS,
            ArtifactKind.CUE_RETRANSLATION_RESPONSE,
        }
    ),
    ArtifactKind.TRANSLATION_POLISH_BATCHES: frozenset(
        {ArtifactKind.SOURCE_CUES, ArtifactKind.TRANSLATION_DRAFT_CUES}
    ),
    ArtifactKind.TRANSLATION_POLISH_PROGRESS: frozenset(
        {ArtifactKind.TRANSLATION_BATCHES, ArtifactKind.TRANSLATION_POLISH_BATCHES}
    ),
    ArtifactKind.TRANSLATION_POLISHED_CUES: frozenset(
        {ArtifactKind.SOURCE_CUES, ArtifactKind.TRANSLATION_POLISH_PROGRESS}
    ),
    # 具体候选通过报告内 checksum 绑定；图上只依赖 source，避免 polished/repaired/
    # reviewed 三条互斥候选路径把当前报告错误地视作彼此的下游。
    ArtifactKind.TRANSLATION_CANDIDATE_QA: frozenset({ArtifactKind.SOURCE_CUES}),
    ArtifactKind.TRANSLATION_SEMANTIC_QA: frozenset(
        {ArtifactKind.SOURCE_CUES, ArtifactKind.TRANSLATION_POLISHED_CUES}
    ),
    ArtifactKind.TRANSLATION_SEMANTIC_REPAIR: frozenset(
        {
            ArtifactKind.SOURCE_CUES,
            ArtifactKind.TRANSLATION_POLISHED_CUES,
            ArtifactKind.TRANSLATION_SEMANTIC_QA,
        }
    ),
    ArtifactKind.TRANSLATION_REPAIRED_CUES: frozenset(
        {ArtifactKind.TRANSLATION_POLISHED_CUES, ArtifactKind.TRANSLATION_SEMANTIC_REPAIR}
    ),
    ArtifactKind.TRANSLATION_SEMANTIC_QA_FINAL: frozenset(
        {ArtifactKind.SOURCE_CUES, ArtifactKind.TRANSLATION_REPAIRED_CUES}
    ),
    ArtifactKind.TRANSLATION_REVIEWED_CUES: frozenset(
        {
            ArtifactKind.SOURCE_CUES,
            ArtifactKind.TRANSLATION_POLISHED_CUES,
            ArtifactKind.TRANSLATION_REVIEW_REVISION,
        }
    ),
    ArtifactKind.TRANSLATION_SEMANTIC_QA_REVIEWED: frozenset(
        {ArtifactKind.SOURCE_CUES, ArtifactKind.TRANSLATION_REVIEWED_CUES}
    ),
    ArtifactKind.TRANSLATED_CUES: frozenset(
        {
            ArtifactKind.SOURCE_CUES,
            ArtifactKind.TRANSLATION_RESPONSE,
            ArtifactKind.TRANSLATION_MAPPING,
            ArtifactKind.TRANSLATION_PROGRESS,
            ArtifactKind.CUE_RETRANSLATION_RESPONSE,
            ArtifactKind.TRANSLATION_POLISHED_CUES,
            ArtifactKind.TRANSLATION_SEMANTIC_QA,
            ArtifactKind.TRANSLATION_REPAIRED_CUES,
            ArtifactKind.TRANSLATION_SEMANTIC_QA_FINAL,
            ArtifactKind.TRANSLATION_REVIEWED_CUES,
            ArtifactKind.TRANSLATION_SEMANTIC_QA_REVIEWED,
        }
    ),
    # 渲染与烧录链。
    ArtifactKind.TRANSLATED_SUBTITLE_QA: frozenset({ArtifactKind.TRANSLATED_CUES}),
    ArtifactKind.TRANSLATED_SRT: frozenset({ArtifactKind.TRANSLATED_CUES}),
    ArtifactKind.BILINGUAL_SRT: frozenset({ArtifactKind.TRANSLATED_CUES}),
    ArtifactKind.BILINGUAL_ASS: frozenset({ArtifactKind.TRANSLATED_CUES}),
    ArtifactKind.BURNED_VIDEO: frozenset({ArtifactKind.BILINGUAL_ASS}),
    # 烧录成功后登记的派生指纹；失效随 burned_video 传递闭包。
    ArtifactKind.BURN_FINGERPRINT: frozenset({ArtifactKind.BURNED_VIDEO}),
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

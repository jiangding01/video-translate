"""Artifact 注册表与依赖图的行为钉子测试。

这些测试把三处失效集合的历史硬编码值作为常量钉住，确保由依赖图闭包推导出的
集合与旧行为逐一相等；同时校验枚举登记的完整性与依赖图的无环性。
"""

from __future__ import annotations

import re
from pathlib import Path

from xvt.artifacts import DERIVES_FROM, ArtifactKind, downstream_of

SRC_DIR = Path(__file__).resolve().parent.parent / "src" / "xvt"

# 历史硬编码集合（P6a 重构前的原值），作为行为基线钉死。
LEGACY_RENDERED_INVALIDATION = {
    "translated_subtitle_qa",
    "translated_srt",
    "bilingual_srt",
    "bilingual_ass",
    "burned_video",
    "media_qa",
    "preview_ass",
    "preview_video",
}
LEGACY_PREVIEW_INVALIDATION = {"preview_ass", "preview_video"}
LEGACY_RESEGMENT_INVALIDATION = {
    "input_translation_subtitles",
    "translation_mapping",
    "translation_batches",
    "translation_progress",
    "translation_response",
    "translated_cues",
    "translated_subtitle_qa",
    "translated_srt",
    "bilingual_srt",
    "bilingual_ass",
    "burned_video",
    "media_qa",
    "preview_ass",
    "preview_video",
}


def _kinds_used_in_source() -> set[str]:
    """扫描源码，收集所有实际登记为 artifact 的 kind 字符串。"""

    patterns = (
        re.compile(r'_add_artifact\(\s*job,\s*"([^"]+)"'),
        re.compile(r'Artifact\(\s*"([^"]+)"'),
    )
    used: set[str] = set()
    for path in SRC_DIR.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            used.update(pattern.findall(text))
    return used


def test_enum_covers_every_kind_used_in_source() -> None:
    used = _kinds_used_in_source()
    registered = {kind.value for kind in ArtifactKind}
    assert used, "expected to discover artifact kinds in source"
    unregistered = used - registered
    assert not unregistered, f"kinds used in source but missing from ArtifactKind: {unregistered}"


def test_derives_from_covers_every_kind() -> None:
    assert set(DERIVES_FROM) == set(ArtifactKind)
    known = set(ArtifactKind)
    for kind, upstreams in DERIVES_FROM.items():
        assert upstreams <= known, f"{kind} references unknown upstream: {upstreams - known}"


def test_rendered_invalidation_matches_legacy() -> None:
    computed = {kind.value for kind in downstream_of(ArtifactKind.TRANSLATED_CUES)}
    assert computed == LEGACY_RENDERED_INVALIDATION


def test_preview_invalidation_matches_legacy() -> None:
    computed = {
        kind.value for kind in downstream_of(ArtifactKind.PREVIEW_ASS) | {ArtifactKind.PREVIEW_ASS}
    }
    assert computed == LEGACY_PREVIEW_INVALIDATION


def test_resegment_invalidation_matches_legacy() -> None:
    computed = {
        kind.value
        for kind in (
            downstream_of(ArtifactKind.SOURCE_CUES)
            - {
                ArtifactKind.SOURCE_SRT,
                ArtifactKind.SUBTITLE_QA,
                ArtifactKind.CUE_RETRANSLATION_BATCHES,
                ArtifactKind.CUE_RETRANSLATION_RESPONSE,
            }
        )
        | {ArtifactKind.INPUT_TRANSLATION_SUBTITLES}
    }
    assert computed == LEGACY_RESEGMENT_INVALIDATION


def test_dependency_graph_is_acyclic() -> None:
    # Kahn 拓扑排序：若无法排完全部节点，则存在环。
    indegree = {kind: len(upstreams) for kind, upstreams in DERIVES_FROM.items()}
    dependents: dict[ArtifactKind, list[ArtifactKind]] = {kind: [] for kind in DERIVES_FROM}
    for kind, upstreams in DERIVES_FROM.items():
        for upstream in upstreams:
            dependents[upstream].append(kind)
    queue = [kind for kind, degree in indegree.items() if degree == 0]
    visited = 0
    while queue:
        node = queue.pop()
        visited += 1
        for dependent in dependents[node]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                queue.append(dependent)
    assert visited == len(DERIVES_FROM), "dependency graph contains a cycle"

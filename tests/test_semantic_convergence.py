import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from xvt.models import Cue
from xvt.pipeline.semantic import _merge_issues
from xvt.qa import check_subtitles
from xvt.translate import (
    apply_semantic_repairs,
    build_semantic_repair_batches,
    build_semantic_review_batches,
    parse_semantic_repair_response,
    parse_semantic_review_response,
)

SAMPLES = json.loads(
    (Path(__file__).parent / "fixtures" / "q62-semantic-samples.json").read_text(encoding="utf-8")
)


def _cues(sample: dict[str, Any]) -> tuple[list[Cue], list[Cue]]:
    source = [
        Cue(index, (index - 1) * 4000, index * 4000, text, source_language="en")
        for index, text in enumerate(sample["source"], 1)
    ]
    candidate = [
        replace(cue, target_text=text, target_language="zh-CN")
        for cue, text in zip(source, sample["candidate"], strict=True)
    ]
    return source, candidate


@pytest.mark.parametrize("variant", SAMPLES["environment"]["variants"])
def test_different_omissions_on_the_same_cue_remain_actionable(variant: dict[str, str]) -> None:
    sample = SAMPLES["environment"]
    source, candidate = _cues(sample)
    candidate[1] = replace(candidate[1], target_text=variant["text"])
    batch = build_semantic_review_batches(
        source, candidate, source_language="en", target_language="zh-CN"
    )[0]
    response = deepcopy(sample["issue"])
    response["message"] = f"Missing {variant['missing']}: source span {variant['evidence']}"
    issues = parse_semantic_review_response(batch, [response])

    assert len(issues) == 1
    assert issues[0].category == "omission"
    assert issues[0].cue_ids == (2,)
    assert variant["evidence"] in source[1].source_text
    assert issues[0].suggested_items[0].text == "或用CLAUDE_CODE_SUBAGENT_MODEL环境变量"


def test_repair_and_scoped_review_include_the_complete_historical_utterance() -> None:
    sample = SAMPLES["join"]
    source, candidate = _cues(sample)
    neighbor = Cue(3, 8000, 12000, "Next sentence.", source_language="en")
    source.append(neighbor)
    candidate.append(replace(neighbor, target_text="下一句", target_language="zh-CN"))
    batch = build_semantic_review_batches(
        source,
        candidate,
        source_language="en",
        target_language="zh-CN",
        review_cue_ids=frozenset({1}),
    )[0]
    assert [item.id for item in batch.items] == [1, 2]
    assert [item.id for item in batch.context_after] == [3]
    issues = parse_semantic_review_response(batch, [sample["issue"]])
    repair = build_semantic_repair_batches(
        source, candidate, issues, source_language="en", target_language="zh-CN"
    )[0]
    assert [item.id for item in repair.items] == [1, 2]
    with pytest.raises(ValueError, match="cover its complete semantic group"):
        parse_semantic_repair_response(repair, sample["issue"]["suggested_items"][:1])
    repaired = parse_semantic_repair_response(repair, sample["issue"]["suggested_items"])
    updated = apply_semantic_repairs(candidate, repaired, {1, 2}, target_language="zh-CN")

    assert updated[0].target_text == "换句话说 这是一个传统程序"
    assert updated[1].target_text == "一个只加了一点点AI的确定性程序"
    assert updated[2] == candidate[2]
    assert [(c.id, c.start_ms, c.end_ms, c.source_text) for c in updated] == [
        (c.id, c.start_ms, c.end_ms, c.source_text) for c in source
    ]
    assert candidate[0].target_text == sample["candidate"][0]


def test_source_suspect_survives_an_empty_recheck_without_authorizing_source_repair() -> None:
    sample = SAMPLES["source_suspect"]
    source, candidate = _cues(sample)
    batch = build_semantic_review_batches(
        source, candidate, source_language="en", target_language="zh-CN"
    )[0]
    preserved = parse_semantic_review_response(batch, [sample["issue"]])
    empty_recheck = parse_semantic_review_response(batch, [])

    assert _merge_issues(empty_recheck, preserved) == preserved
    assert preserved[0].suggested_items == ()
    assert (
        build_semantic_repair_batches(
            source, candidate, preserved, source_language="en", target_language="zh-CN"
        )
        == []
    )
    invalid = deepcopy(sample["issue"])
    invalid["suggested_items"] = [{"id": 3, "text": "模组渲染只改变屏幕上显示的内容"}]
    with pytest.raises(ValueError, match="must be empty"):
        parse_semantic_review_response(batch, [invalid])


@pytest.mark.parametrize("category", ["meaning_error", "omission"])
def test_message_wording_does_not_discard_a_structured_error(category: str) -> None:
    source, candidate = _cues(SAMPLES["environment"])
    batch = build_semantic_review_batches(
        source, candidate, source_language="en", target_language="zh-CN"
    )[0]
    issue = deepcopy(SAMPLES["environment"]["issue"])
    issue["category"] = category
    issue["message"] = "其他表达可接受 此处仍缺少源文的 or 选择关系"
    parsed = parse_semantic_review_response(batch, [issue])
    assert len(parsed) == 1
    assert parsed[0].category == category


def test_historical_unchanged_suggestion_is_rejected() -> None:
    sample = SAMPLES["unchanged_suggestion"]
    source, candidate = _cues(sample)
    batch = build_semantic_review_batches(
        source, candidate, source_language="en", target_language="zh-CN"
    )[0]
    with pytest.raises(ValueError, match="must change the candidate text"):
        parse_semantic_review_response(batch, [sample["issue"]])


def test_missing_environment_qualifier_still_fails_deterministic_glossary_qa() -> None:
    source, candidate = _cues(SAMPLES["environment"])
    candidate[1] = replace(candidate[1], target_text="或用CLAUDE_CODE_SUBAGENT_MODEL变量")
    batch = build_semantic_review_batches(
        source, candidate, source_language="en", target_language="zh-CN"
    )[0]
    assert parse_semantic_review_response(batch, []) == ()
    report = check_subtitles(
        candidate,
        require_translation=True,
        required_terms=(("environment variable", "环境变量"),),
    )
    assert report.status == "failed"
    assert ("glossary_violation", "error", 2) in {
        (issue.code, issue.severity, issue.cue_id) for issue in report.issues
    }

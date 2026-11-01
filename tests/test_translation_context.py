"""翻译上下文候选包（Q7a）：抽取判据、降级契约与 artifact 登记。

规则 D 的基线是**实测**而不是构造：``tests/fixtures/source-en-mazda-mx5.srt`` 是一份
真实的英文人工字幕（52 个 cue），人工判定的真专名恰好 6 个。
``test_rule_d_partition_matches_measured_baseline`` 把「6 收 6 弃」逐词钉住，改判据
时它会立刻变红。被实测否决的两条替代判据（「出现 ≥2 次才收」漏 4/6、「只收形态
可辨的」漏 4/6）各有一条用例说明为什么不能捡回来。
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from xvt.cli import main
from xvt.job_store import create_job, load_job, save_job
from xvt.models import Artifact, Cue
from xvt.pipeline import build_translation_context
from xvt.recovery import file_checksum
from xvt.subtitles import load_subtitles
from xvt.translate import load_translation_policy
from xvt.translation_context import (
    DEGRADED_NO_CASE_SIGNAL,
    DEGRADED_NO_SENTENCE_BOUNDARY,
    KIND_CAPITALIZED_RUN,
    KIND_PROPER_NAME,
    KIND_SHAPED_TERM,
    build_text_stream,
    extract_translation_context,
    glossary_skeleton_terms,
    rule_d_partition,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "source-en-mazda-mx5.srt"

# 人工判定的真专名（产品名、品牌、平台名）与被规则 D 弃掉的句首功能词。
_MEASURED_PROPER_NAMES = ("Android", "Apple", "Auto", "CarPlay", "MX-5", "Mazda")
_MEASURED_SENTENCE_INITIAL = ("At", "But", "No", "Perhaps", "Plus", "Whichever")


def _fixture_cues() -> list[Cue]:
    return load_subtitles(_FIXTURE)


def _cues(*texts: str) -> list[Cue]:
    return [
        Cue(id=index, start_ms=index * 1000, end_ms=index * 1000 + 900, source_text=text)
        for index, text in enumerate(texts, 1)
    ]


class RuleDTest(unittest.TestCase):
    def test_rule_d_partition_matches_measured_baseline(self) -> None:
        collected, rejected = rule_d_partition(_fixture_cues())
        self.assertEqual(collected, _MEASURED_PROPER_NAMES)
        self.assertEqual(rejected, _MEASURED_SENTENCE_INITIAL)

    def test_occurrence_threshold_would_lose_most_proper_names(self) -> None:
        """钉住「出现 ≥2 次才收」为什么被否决：它漏掉 4/6 真专名。

        这条不是在测产品行为，而是把否决理由留成可执行的证据——有人想重新加频次
        门槛时，这里显示代价是多少。
        """

        cues = _fixture_cues()
        report = extract_translation_context(cues, source_language="en")
        counts = {
            candidate["source"]: candidate["occurrence_count"] for candidate in report["candidates"]
        }
        single_occurrence = [name for name in _MEASURED_PROPER_NAMES if counts.get(name, 0) < 2]
        self.assertEqual(sorted(single_occurrence), ["Android", "Apple", "Auto", "CarPlay"])

    def test_sentence_boundary_is_not_cue_boundary(self) -> None:
        """cue 切在句中时，下一个 cue 的首词不能被当成句首。

        这是 fixture 里真实存在的形态（``...for the seventh year. And`` / ``here's
        why...``）。逐 cue 判句首会把 ``Mazda`` 一类词误判成句首词而整条漏掉。
        """

        cues = _cues("I drove the", "Mazda yesterday. It was quick.")
        stream, spans = build_text_stream(cues)
        self.assertEqual(stream, "I drove the Mazda yesterday. It was quick.")
        self.assertEqual(len(spans), 2)
        collected, rejected = rule_d_partition(cues)
        self.assertIn("Mazda", collected)
        self.assertNotIn("Mazda", rejected)


class RealSampleTest(unittest.TestCase):
    def test_real_sample_is_not_degraded_and_labels_proper_names(self) -> None:
        report = extract_translation_context(_fixture_cues(), source_language="en")
        self.assertIsNone(report["extractor_degraded"])
        kinds = {candidate["source"]: candidate["kind"] for candidate in report["candidates"]}
        for name in _MEASURED_PROPER_NAMES:
            self.assertEqual(kinds.get(name), KIND_PROPER_NAME, name)
        for word in _MEASURED_SENTENCE_INITIAL:
            self.assertNotIn(word, kinds)
        # 多词串单独成候选，并且永远不是 proper_name。
        self.assertEqual(kinds.get("Apple CarPlay"), KIND_CAPITALIZED_RUN)

    def test_skeleton_contains_exactly_the_measured_proper_names(self) -> None:
        report = extract_translation_context(_fixture_cues(), source_language="en")
        terms = glossary_skeleton_terms(report)
        self.assertEqual(sorted(term["source"] for term in terms), sorted(_MEASURED_PROPER_NAMES))
        self.assertTrue(all(term["target"] == term["source"] for term in terms))

    def test_lowercase_hyphenated_compounds_stay_out_of_the_skeleton(self) -> None:
        """``low-slung`` / ``two-seater`` 是构词法，不是术语。

        它们在 fixture 里真实出现，早一版只看连字符的形态规则把它们收成了高置信
        候选并导进骨架——带着恒等映射就是要求译文里出现英文原词，踩中 error 级
        ``glossary_violation``（没有 ``--acknowledge`` 出口）。
        """

        report = extract_translation_context(_fixture_cues(), source_language="en")
        sources = {candidate["source"] for candidate in report["candidates"]}
        self.assertNotIn("low-slung", sources)
        self.assertNotIn("two-seater", sources)

    def test_platform_title_marks_metadata_evidence(self) -> None:
        report = extract_translation_context(
            _fixture_cues(), source_language="en", metadata_title="2026 Mazda MX-5 review"
        )
        evidence = {
            candidate["source"]: candidate["evidence"] for candidate in report["candidates"]
        }
        self.assertEqual(evidence["Mazda"]["metadata"], ["title"])
        self.assertEqual(evidence["Android"]["metadata"], [])
        # 证据要指得回原文，否则「可审计」是空话。
        self.assertTrue(evidence["Mazda"]["cue_ids"])


class NonSpeechMarkerTest(unittest.TestCase):
    def test_markers_do_not_become_candidates(self) -> None:
        cues = _cues(
            ">> The engine sounds great. [Music]",
            ">> [Applause] The Mazda handles well. [Music]",
        )
        report = extract_translation_context(cues, source_language="en")
        sources = {candidate["source"] for candidate in report["candidates"]}
        self.assertNotIn("Music", sources)
        self.assertNotIn("Applause", sources)
        self.assertIn("Mazda", sources)


class DegradationTest(unittest.TestCase):
    def test_cjk_reports_no_case_signal(self) -> None:
        """CJK 不是「候选为空就算对」，而是必须说清为什么放弃专名抽取。"""

        cues = _cues("马自达 MX-5 是一台轻量跑车。", "它的操控非常灵活。")
        report = extract_translation_context(cues, source_language="zh-CN")
        self.assertEqual(report["extractor_degraded"]["reason"], DEGRADED_NO_CASE_SIGNAL)
        kinds = {candidate["source"]: candidate["kind"] for candidate in report["candidates"]}
        self.assertNotIn(KIND_PROPER_NAME, kinds.values())
        # 形态信号与语言无关，降级也该照旧产出。
        self.assertEqual(kinds.get("MX-5"), KIND_SHAPED_TERM)

    def test_german_reports_no_case_signal(self) -> None:
        cues = _cues("Der Wagen hat eine gute Lenkung.", "Das Getriebe ist sehr praezise.")
        report = extract_translation_context(cues, source_language="de")
        self.assertEqual(report["extractor_degraded"]["reason"], DEGRADED_NO_CASE_SIGNAL)
        self.assertEqual(report["candidates"], [])

    def test_unknown_language_reports_no_case_signal(self) -> None:
        report = extract_translation_context(_fixture_cues(), source_language="auto")
        self.assertEqual(report["extractor_degraded"]["reason"], DEGRADED_NO_CASE_SIGNAL)

    def test_unpunctuated_corpus_reports_no_sentence_boundary(self) -> None:
        cues = _cues("the Mazda MX-5 is a light sports car", "Apple CarPlay works well here")
        report = extract_translation_context(cues, source_language="en")
        self.assertEqual(report["extractor_degraded"]["reason"], DEGRADED_NO_SENTENCE_BOUNDARY)
        kinds = {candidate["kind"] for candidate in report["candidates"]}
        self.assertNotIn(KIND_PROPER_NAME, kinds)

    def test_single_case_corpus_reports_no_case_signal(self) -> None:
        cues = _cues("the mazda mx is a light sports car.", "it handles well.")
        report = extract_translation_context(cues, source_language="en")
        self.assertEqual(report["extractor_degraded"]["reason"], DEGRADED_NO_CASE_SIGNAL)


class SkeletonLoadsTest(unittest.TestCase):
    def test_exported_skeleton_is_accepted_by_load_translation_policy(self) -> None:
        """闭环验证：导出的骨架必须真能当 ``--glossary`` 用，不是只长得像。"""

        report = extract_translation_context(_fixture_cues(), source_language="en")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "glossary.json"
            path.write_text(
                json.dumps({"_note": "review me", "terms": glossary_skeleton_terms(report)}),
                encoding="utf-8",
            )
            policy = load_translation_policy(path)
        self.assertEqual(
            sorted(term.source for term in policy.terms), sorted(_MEASURED_PROPER_NAMES)
        )
        self.assertTrue(all(term.target == term.source for term in policy.terms))

    def test_case_variants_are_deduped_so_the_skeleton_still_loads(self) -> None:
        """``load_translation_policy`` 按 casefold 判重复并直接抛，所以必须去重。"""

        cues = _cues(
            "I used the Auto mode first. Then the AUTO switch failed.",
            "The Auto mode came back.",
        )
        report = extract_translation_context(cues, source_language="en")
        sources = [candidate["source"] for candidate in report["candidates"]]
        self.assertIn("Auto", sources)
        self.assertIn("AUTO", sources)
        terms = glossary_skeleton_terms(report)
        folded = [term["source"].casefold() for term in terms]
        self.assertEqual(len(folded), len(set(folded)))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "glossary.json"
            path.write_text(json.dumps({"terms": terms}), encoding="utf-8")
            policy = load_translation_policy(path)
        self.assertTrue(policy.terms)

    def test_spelling_variants_are_flagged_high_risk(self) -> None:
        cues = _cues(
            "I tried OpenAI first. Then Open AI changed the name.",
            "The OpenAI result was better.",
        )
        report = extract_translation_context(cues, source_language="en")
        by_source = {candidate["source"]: candidate for candidate in report["candidates"]}
        self.assertEqual(by_source["OpenAI"]["risk"], "high")
        self.assertIn("Open AI", by_source["OpenAI"]["variants"])


# 抄自 2026-11-01 真实端到端验收的候选包（Claude 官方频道「Coding on the go with
# Claude Code」，242 秒、en-US）。原始数值：Claude = proper_name / high / 18 次 /
# 命中 title；Claude Code = capitalized_run / **medium**；Claude GitHub 与
# Remote Control = capitalized_run / high；Remote、Control、GitHub、Slack =
# proper_name / high。medium 那条是关键——覆盖关系若按 confidence 过滤就会漏掉它。
_ACCEPTANCE_CANDIDATES: list[dict[str, object]] = [
    {
        "source": "Claude",
        "kind": "proper_name",
        "confidence": "high",
        "occurrence_count": 18,
        "evidence": {"cue_ids": [1, 4, 24], "metadata": ["title"]},
    },
    {
        "source": "GitHub",
        "kind": "proper_name",
        "confidence": "high",
        "occurrence_count": 6,
        "evidence": {"cue_ids": [28], "metadata": []},
    },
    {
        "source": "Claude Code",
        "kind": "capitalized_run",
        "confidence": "medium",
        "occurrence_count": 1,
        "evidence": {"cue_ids": [53], "metadata": []},
    },
    {
        "source": "Claude GitHub",
        "kind": "capitalized_run",
        "confidence": "high",
        "occurrence_count": 1,
        "evidence": {"cue_ids": [33], "metadata": []},
    },
    {
        "source": "Remote Control",
        "kind": "capitalized_run",
        "confidence": "high",
        "occurrence_count": 1,
        "evidence": {"cue_ids": [70], "metadata": []},
    },
    {
        "source": "Remote",
        "kind": "proper_name",
        "confidence": "high",
        "occurrence_count": 1,
        "evidence": {"cue_ids": [70], "metadata": []},
    },
    {
        "source": "Control",
        "kind": "proper_name",
        "confidence": "high",
        "occurrence_count": 1,
        "evidence": {"cue_ids": [70], "metadata": []},
    },
    {
        "source": "Slack",
        "kind": "proper_name",
        "confidence": "high",
        "occurrence_count": 1,
        "evidence": {"cue_ids": [71], "metadata": []},
    },
]


class SkeletonCoveredByTest(unittest.TestCase):
    """多词专名拆出的单词：标注而不排除。"""

    def _terms(self) -> dict[str, dict]:
        return {
            term["source"]: term
            for term in glossary_skeleton_terms({"candidates": _ACCEPTANCE_CANDIDATES})
        }

    def test_claude_stays_in_the_skeleton_with_both_covering_phrases(self) -> None:
        """真实反例，这条是方向锚：`Claude` 被两个短语覆盖，却必须留在骨架里。

        它出现 18 次、命中平台标题，是整份语料最核心的专名——「被多词覆盖就排除」
        的实现会把最该固定译名的词丢出骨架，那条测试会在这里红。
        `Claude Code` 的 confidence 是 medium，所以覆盖关系不能按 confidence 过滤，
        否则 `_covered_by` 只剩 `Claude GitHub`，而用户最关心的恰是 `Claude Code`。
        """

        claude = self._terms()["Claude"]
        self.assertEqual(claude["_covered_by"], ["Claude Code", "Claude GitHub"])
        self.assertEqual(claude["target"], "Claude")

    def test_fragments_carry_the_phrase_that_covers_them(self) -> None:
        """真实正例：`Remote` / `Control` 各自不是专名，标注让用户判断得出来。"""

        terms = self._terms()
        self.assertEqual(terms["Remote"]["_covered_by"], ["Remote Control"])
        self.assertEqual(terms["Control"]["_covered_by"], ["Remote Control"])
        # 多词本身仍不进骨架：短语级恒等映射是强主张，见模块 docstring。
        self.assertNotIn("Remote Control", terms)

    def test_uncovered_terms_have_no_covered_by_key(self) -> None:
        """不该红时不红：没被覆盖的条目不带这个键，而不是带个空数组。"""

        self.assertNotIn("_covered_by", self._terms()["Slack"])

    def test_skeleton_with_covered_by_still_loads(self) -> None:
        """闭环：额外注释键不能破坏 `load_translation_policy`。"""

        terms = glossary_skeleton_terms({"candidates": _ACCEPTANCE_CANDIDATES})
        self.assertTrue(any("_covered_by" in term for term in terms))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "glossary.json"
            path.write_text(json.dumps({"terms": terms}), encoding="utf-8")
            policy = load_translation_policy(path)
        self.assertIn("Claude", [term.source for term in policy.terms])


def _job_with_source_cues(root: Path, cues: list[Cue]) -> tuple[Path, Path]:
    job = create_job("/tmp/input.mp4", root)
    artifacts = root / "jobs" / job.id / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    source_path = artifacts / "source-cues.json"
    source_path.write_text(
        json.dumps([asdict(cue) for cue in cues]),
        encoding="utf-8",
    )
    job.artifacts.append(Artifact("source_cues", str(source_path), file_checksum(source_path)))
    job.config["source_language"] = "en"
    save_job(job)
    return root / "jobs" / job.id / "job.json", source_path


class PipelineRegistrationTest(unittest.TestCase):
    def test_registers_artifact_without_touching_any_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, _ = _job_with_source_cues(root, _fixture_cues())
            before = {
                name: (record.status, record.error)
                for name, record in load_job(job_file).stages.items()
            }
            events: list[str] = []
            result = build_translation_context(
                job_file, event_sink=lambda event: events.append(event.type)
            )
            job = load_job(job_file)
            registered = {artifact.kind: artifact for artifact in job.artifacts}
            self.assertIn("translation_context", registered)
            artifact = registered["translation_context"]
            self.assertEqual(artifact.checksum, file_checksum(Path(artifact.path)))
            self.assertEqual(
                result.candidate_count,
                len(json.loads(Path(artifact.path).read_text(encoding="utf-8"))["candidates"]),
            )
            self.assertIsNone(result.degraded_reason)
            # 不改 stage、不发新事件类型：候选包不参与推进判定。
            after = {name: (record.status, record.error) for name, record in job.stages.items()}
            self.assertEqual(before, after)
            self.assertEqual(set(events), {"artifact_created"})

    def test_report_records_source_cues_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, source_path = _job_with_source_cues(root, _fixture_cues())
            result = build_translation_context(job_file)
            report = json.loads(result.report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["source_cues_checksum"], file_checksum(source_path))
            self.assertIsNone(report["remote_info_checksum"])
            self.assertIn("topic_hints", report)

    def test_export_glossary_writes_reviewable_skeleton(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, _ = _job_with_source_cues(root, _fixture_cues())
            glossary = root / "glossary.json"
            result = build_translation_context(job_file, export_glossary=glossary)
            self.assertEqual(result.glossary_path, glossary.resolve())
            data = json.loads(glossary.read_text(encoding="utf-8"))
            self.assertIn("_note", data)
            self.assertEqual(result.glossary_term_count, len(data["terms"]))
            self.assertTrue(load_translation_policy(glossary).terms)


class PipelineRejectionTest(unittest.TestCase):
    def test_missing_source_cues_is_rejected_without_writing_an_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            save_job(job)
            job_file = root / "jobs" / job.id / "job.json"
            with self.assertRaises(ValueError):
                build_translation_context(job_file)
            self.assertEqual(load_job(job_file).artifacts, [])
            self.assertEqual(main(["build-translation-context", str(job_file)]), 2)
            self.assertEqual(load_job(job_file).artifacts, [])

    def test_invalid_source_cues_checksum_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, source_path = _job_with_source_cues(root, _fixture_cues())
            source_path.write_text("[]", encoding="utf-8")
            self.assertEqual(main(["build-translation-context", str(job_file)]), 2)
            kinds = {artifact.kind for artifact in load_job(job_file).artifacts}
            self.assertNotIn("translation_context", kinds)

    def test_glossary_export_must_not_overwrite_a_registered_artifact(self) -> None:
        """骨架导出是新开的用户指定写盘出口，走与烧录输出同一道覆盖保护。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, source_path = _job_with_source_cues(root, _fixture_cues())
            before = source_path.read_bytes()
            self.assertEqual(
                main(
                    [
                        "build-translation-context",
                        str(job_file),
                        "--export-glossary",
                        str(source_path),
                    ]
                ),
                2,
            )
            # 断言字节未变，否则证明不了「拒绝」而不只是「报了个错」。
            self.assertEqual(source_path.read_bytes(), before)
            kinds = {artifact.kind for artifact in load_job(job_file).artifacts}
            self.assertNotIn("translation_context", kinds)

    def test_glossary_export_must_not_overwrite_the_job_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, _ = _job_with_source_cues(root, _fixture_cues())
            before = job_file.read_bytes()
            self.assertEqual(
                main(
                    [
                        "build-translation-context",
                        str(job_file),
                        "--export-glossary",
                        str(job_file),
                    ]
                ),
                2,
            )
            self.assertEqual(job_file.read_bytes(), before)

    def test_missing_glossary_directory_is_rejected_before_anything_is_written(self) -> None:
        """目录不存在是用户输入错误（2），不是依赖故障（3），且不留半份产物。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, _ = _job_with_source_cues(root, _fixture_cues())
            self.assertEqual(
                main(
                    [
                        "build-translation-context",
                        str(job_file),
                        "--export-glossary",
                        str(root / "no-such-dir" / "glossary.json"),
                    ]
                ),
                2,
            )
            kinds = {artifact.kind for artifact in load_job(job_file).artifacts}
            self.assertNotIn("translation_context", kinds)
            self.assertFalse((root / "no-such-dir").exists())


class CliTest(unittest.TestCase):
    def test_command_succeeds_on_degraded_corpus(self) -> None:
        """降级不是失败：退出码仍为 0，产物与原因都落盘。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, _ = _job_with_source_cues(
                root, _cues("马自达 MX-5 很灵活。", "它的转向很准。")
            )
            job = load_job(job_file)
            job.config["source_language"] = "zh-CN"
            save_job(job)
            self.assertEqual(main(["build-translation-context", str(job_file)]), 0)
            job = load_job(job_file)
            artifact = next(item for item in job.artifacts if item.kind == "translation_context")
            report = json.loads(Path(artifact.path).read_text(encoding="utf-8"))
            self.assertEqual(report["extractor_degraded"]["reason"], DEGRADED_NO_CASE_SIGNAL)

    def test_export_hint_names_the_covered_fragments(self) -> None:
        """骨架里有多词碎片时，CLI 当场把覆盖关系打出来，不留在 JSON 里等人翻。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, _ = _job_with_source_cues(
                root,
                _cues(
                    "The Remote Control feature shipped today.",
                    "Our Remote Control works offline.",
                ),
            )
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = main(
                    [
                        "build-translation-context",
                        str(job_file),
                        "--export-glossary",
                        str(root / "glossary.json"),
                    ]
                )
            self.assertEqual(code, 0)
            output = buffer.getvalue()
            self.assertIn("多词专名的组成部分", output)
            self.assertIn("⊂ Remote Control", output)

    def test_no_covered_hint_when_nothing_is_covered(self) -> None:
        """不该红时不红：没有多词串就不打那一行。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, _ = _job_with_source_cues(
                root,
                _cues("We tested Mazda last week.", "Our Mazda was quick."),
            )
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = main(
                    [
                        "build-translation-context",
                        str(job_file),
                        "--export-glossary",
                        str(root / "glossary.json"),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertNotIn("多词专名的组成部分", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()

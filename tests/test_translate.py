import json
import tempfile
import unittest
from pathlib import Path

from xvt.models import Cue
from xvt.translate import (
    GlossaryTerm,
    TranslationPolicy,
    apply_translations,
    build_translation_batches,
    load_translation_policy,
    map_subtitle_translations,
    parse_translation_response,
)


class TranslateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cues = [
            Cue(1, 0, 1000, "One"),
            Cue(2, 1000, 2000, "Two"),
            Cue(3, 2000, 3000, "Three"),
        ]

    def test_builds_batches_with_read_only_context(self) -> None:
        batches = build_translation_batches(
            self.cues,
            source_language="en",
            target_language="zh-CN",
            batch_size=2,
            context_size=1,
        )

        self.assertEqual([item.id for item in batches[0].items], [1, 2])
        self.assertEqual([item.id for item in batches[0].context_after], [3])
        self.assertEqual([item.id for item in batches[1].context_before], [2])

    def test_batches_include_only_matching_glossary_terms(self) -> None:
        batches = build_translation_batches(
            self.cues,
            source_language="en",
            target_language="zh-CN",
            batch_size=1,
            policy=TranslationPolicy(
                style="natural",
                terms=(GlossaryTerm("One", "一"), GlossaryTerm("Unused", "未使用")),
            ),
        )

        self.assertEqual(batches[0].policy.style, "natural")
        self.assertEqual(batches[0].policy.terms, (GlossaryTerm("One", "一"),))
        self.assertEqual(batches[1].policy.terms, ())

    def test_glossary_rejects_duplicate_source_terms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            glossary = Path(directory) / "glossary.json"
            glossary.write_text(
                json.dumps(
                    {
                        "terms": [
                            {"source": "OpenAI", "target": "OpenAI"},
                            {"source": "openai", "target": "开放人工智能"},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "duplicate source term"):
                load_translation_policy(glossary, style="concise")

    def test_translation_response_requires_exact_ids(self) -> None:
        batch = build_translation_batches(
            self.cues[:2], source_language="en", target_language="zh-CN"
        )[0]

        with self.assertRaisesRegex(ValueError, r"missing=\[2\]"):
            parse_translation_response(batch, [{"id": 1, "text": "一"}])

    def test_applies_translations_without_changing_timeline(self) -> None:
        translated = apply_translations(
            self.cues,
            {1: "一", 2: "二", 3: "三"},
            target_language="zh-CN",
        )

        self.assertEqual([cue.target_text for cue in translated], ["一", "二", "三"])
        self.assertEqual(
            [(cue.start_ms, cue.end_ms) for cue in translated],
            [(0, 1000), (1000, 2000), (2000, 3000)],
        )

    def test_maps_equal_cue_counts_by_order(self) -> None:
        translated = [
            Cue(1, 9000, 10000, "", target_text="一"),
            Cue(2, 10000, 11000, "", target_text="二"),
            Cue(3, 11000, 12000, "", target_text="三"),
        ]

        translations, report = map_subtitle_translations(self.cues, translated)

        self.assertEqual(translations, {1: "一", 2: "二", 3: "三"})
        self.assertEqual(report["method"], "strict_order")

    def test_maps_multiple_translated_cues_by_timeline_overlap(self) -> None:
        source = [Cue(1, 0, 2000, "One")]
        translated = [
            Cue(1, 0, 1000, "", target_text="第一个"),
            Cue(2, 1000, 2000, "", target_text="第二个"),
        ]

        translations, report = map_subtitle_translations(source, translated)

        self.assertEqual(translations, {1: "第一个\n第二个"})
        self.assertEqual(report["method"], "timeline_overlap")
        self.assertEqual(report["confidence"], 1.0)

    def test_timeline_mapping_rejects_uncovered_source_cues(self) -> None:
        translated = [Cue(1, 0, 1000, "", target_text="一")]

        with self.assertRaisesRegex(ValueError, "do not cover source cues"):
            map_subtitle_translations(self.cues, translated)


if __name__ == "__main__":
    unittest.main()

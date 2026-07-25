import json
import tempfile
import unittest
from pathlib import Path

from xvt.errors import InternalError, UserInputError
from xvt.models import Cue
from xvt.translate import (
    GlossaryTerm,
    TranslationPolicy,
    agent_translation_manifest_fingerprint,
    apply_polish_translations,
    apply_semantic_repairs,
    apply_translations,
    build_cue_retranslation_batches,
    build_polish_batches,
    build_semantic_repair_batches,
    build_semantic_review_batches,
    build_translation_batches,
    load_translation_policy,
    map_subtitle_translations,
    parse_semantic_repair_response,
    parse_semantic_review_response,
    parse_translation_response,
    translation_policy_from_dict,
)


class TranslateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cues = [
            Cue(1, 0, 1000, "One"),
            Cue(2, 1000, 2000, "Two"),
            Cue(3, 2000, 3000, "Three"),
        ]

    def test_agent_manifest_fingerprint_excludes_only_its_own_field(self) -> None:
        payload = {
            "job_id": "job",
            "task": "translate_and_polish",
            "batches": [{"id": 1, "items": [{"id": 1, "source_text": "One"}]}],
        }
        fingerprint = agent_translation_manifest_fingerprint(payload)

        self.assertEqual(
            agent_translation_manifest_fingerprint({**payload, "fingerprint": fingerprint}),
            fingerprint,
        )
        changed = {**payload, "batches": [{"id": 1, "items": []}]}
        self.assertNotEqual(agent_translation_manifest_fingerprint(changed), fingerprint)

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
        self.assertEqual([item.id for item in batches[1].context_before], [1, 2])

    def test_semantic_groups_end_at_sentence_boundary_or_long_gap(self) -> None:
        cues = [
            Cue(1, 0, 500, "First sentence."),
            Cue(2, 500, 1000, "Second clause"),
            Cue(3, 1650, 2150, "After a pause"),
            Cue(4, 2150, 2650, "continues."),
        ]

        batches = build_translation_batches(cues, source_language="en", target_language="zh-CN")

        self.assertEqual(batches[0].semantic_groups, ((1,), (2,), (3, 4)))

    def test_semantic_groups_recognize_non_latin_sentence_endings(self) -> None:
        cues = [
            Cue(1, 0, 500, "هل هذا صحيح؟"),
            Cue(2, 500, 1000, "यह सही है।"),
            Cue(3, 1000, 1500, "「終わり。」"),
            Cue(4, 1500, 2000, "Այո։"),
        ]

        batches = build_translation_batches(cues, source_language="auto", target_language="zh-CN")

        self.assertEqual(batches[0].semantic_groups, ((1,), (2,), (3,), (4,)))

    def test_semantic_group_limit_prefers_latest_soft_punctuation(self) -> None:
        cues = [
            Cue(index, (index - 1) * 1000, index * 1000, f"Cue {index}") for index in range(1, 22)
        ]
        cues[5] = Cue(6, 5000, 6000, "Earlier clause,")
        cues[11] = Cue(12, 11000, 12000, "Later clause;")
        cues[16] = Cue(17, 16000, 17000, "and a connector")

        batches = build_translation_batches(cues, source_language="en", target_language="zh-CN")

        self.assertEqual(batches[0].semantic_groups[0], tuple(range(1, 13)))

    def test_semantic_group_limit_prefers_connector_then_largest_latest_gap(self) -> None:
        connector_cues = [
            Cue(index, (index - 1) * 1000, index * 1000, f"Cue {index}") for index in range(1, 22)
        ]
        connector_cues[4] = Cue(5, 4000, 5000, "and earlier")
        connector_cues[9] = Cue(10, 9000, 10000, "but later")
        gap_cues = [
            Cue(index, (index - 1) * 1000, index * 1000, f"Cue {index}") for index in range(1, 22)
        ]
        gap_cues[7] = Cue(8, 7300, 8300, "Smaller gap")
        gap_cues[15] = Cue(16, 15500, 16500, "Largest gap")
        tied_gap_cues = [
            Cue(index, (index - 1) * 1000, index * 1000, f"Cue {index}") for index in range(1, 22)
        ]
        tied_gap_cues[7] = Cue(8, 7500, 8500, "First tied gap")
        tied_gap_cues[11] = Cue(12, 11500, 12500, "Later tied gap")

        connector_batches = build_translation_batches(
            connector_cues, source_language="en", target_language="zh-CN"
        )
        gap_batches = build_translation_batches(
            gap_cues, source_language="en", target_language="zh-CN"
        )
        tied_gap_batches = build_translation_batches(
            tied_gap_cues, source_language="en", target_language="zh-CN"
        )

        self.assertEqual(connector_batches[0].semantic_groups[0], tuple(range(1, 10)))
        self.assertEqual(gap_batches[0].semantic_groups[0], tuple(range(1, 16)))
        self.assertEqual(tied_gap_batches[0].semantic_groups[0], tuple(range(1, 12)))

    def test_semantic_group_limit_uses_duration_and_keeps_single_long_cue(self) -> None:
        duration_limited = [
            Cue(1, 0, 10000, "One"),
            Cue(2, 10000, 20000, "Two"),
            Cue(3, 20000, 30000, "Three"),
            Cue(4, 30000, 40000, "Four"),
        ]
        long_cue = [Cue(1, 0, 31000, "An unbreakable long cue.")]

        duration_batches = build_translation_batches(
            duration_limited, source_language="en", target_language="zh-CN"
        )
        long_cue_batches = build_translation_batches(
            long_cue, source_language="en", target_language="zh-CN"
        )

        self.assertEqual(duration_batches[0].semantic_groups, ((1, 2, 3), (4,)))
        self.assertEqual(long_cue_batches[0].semantic_groups, ((1,),))

    def test_batches_pack_complete_semantic_groups_and_keep_group_context(self) -> None:
        cues = [
            Cue(1, 0, 500, "First."),
            Cue(2, 500, 1000, "Second."),
            Cue(3, 1000, 1500, "Third"),
            Cue(4, 1500, 2000, "continues."),
            Cue(5, 2000, 2500, "Fifth."),
        ]

        batches = build_translation_batches(
            cues,
            source_language="en",
            target_language="zh-CN",
            batch_size=2,
        )

        self.assertEqual(
            [[item.id for item in batch.items] for batch in batches],
            [[1, 2], [3, 4], [5]],
        )
        self.assertEqual(batches[0].semantic_groups, ((1,), (2,)))
        self.assertEqual(batches[1].semantic_groups, ((3, 4),))
        self.assertEqual([item.id for item in batches[1].context_before], [2])
        self.assertEqual([item.id for item in batches[1].context_after], [5])

    def test_small_batch_size_reduces_max_group_cues_without_splitting_a_group(self) -> None:
        cues = [
            Cue(1, 0, 500, "First"),
            Cue(2, 500, 1000, "continues"),
            Cue(3, 1000, 1500, "here."),
        ]

        batches = build_translation_batches(
            cues,
            source_language="en",
            target_language="zh-CN",
            batch_size=2,
        )

        self.assertEqual([[item.id for item in batch.items] for batch in batches], [[1, 2], [3]])
        self.assertEqual(batches[0].semantic_groups, ((1, 2),))

    def test_builds_cue_retranslation_batches_with_original_neighbors(self) -> None:
        batches = build_cue_retranslation_batches(
            self.cues,
            (2,),
            source_language="en",
            target_language="zh-CN",
            context_size=1,
        )

        self.assertEqual([item.id for item in batches[0].items], [2])
        self.assertEqual([item.id for item in batches[0].context_before], [1])
        self.assertEqual([item.id for item in batches[0].context_after], [3])

    def test_cue_retranslation_rejects_duplicate_source_ids(self) -> None:
        with self.assertRaisesRegex(InternalError, "duplicate cue ids"):
            build_cue_retranslation_batches(
                [self.cues[0], self.cues[0]],
                (1,),
                source_language="en",
                target_language="zh-CN",
            )

    def test_cue_retranslation_rejects_missing_source_id_as_user_error(self) -> None:
        with self.assertRaisesRegex(UserInputError, "do not contain ids"):
            build_cue_retranslation_batches(
                self.cues,
                (99,),
                source_language="en",
                target_language="zh-CN",
            )

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

    def test_policy_from_dict_normalizes_and_validates_terms(self) -> None:
        policy = translation_policy_from_dict(
            {
                "style": "natural",
                "terms": [
                    {"source": " Whisper ", "target": " Whisper "},
                    {"source": "OpenAI", "target": "OpenAI"},
                ],
            }
        )

        self.assertEqual(policy.style, "natural")
        self.assertEqual(
            policy.terms,
            (GlossaryTerm("OpenAI", "OpenAI"), GlossaryTerm("Whisper", "Whisper")),
        )
        with self.assertRaisesRegex(ValueError, "unsupported style"):
            translation_policy_from_dict({"style": "creative"})

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

    def test_polish_batches_include_draft_and_preserve_semantic_groups(self) -> None:
        draft = [
            Cue(
                cue.id,
                cue.start_ms,
                cue.end_ms,
                cue.source_text,
                target_text=f"draft {cue.id}",
                target_language="zh-CN",
            )
            for cue in self.cues
        ]

        batches = build_polish_batches(
            self.cues,
            draft,
            source_language="en",
            target_language="zh-CN",
            batch_size=2,
        )

        self.assertEqual([item.draft_text for item in batches[0].items], ["draft 1", "draft 2"])
        self.assertEqual(batches[0].semantic_groups, ((1, 2),))

    def test_polish_rejects_changed_timeline(self) -> None:
        draft = [
            Cue(1, 1, 1000, "One", target_text="一", target_language="zh-CN"),
            Cue(2, 1000, 2000, "Two", target_text="二", target_language="zh-CN"),
            Cue(3, 2000, 3000, "Three", target_text="三", target_language="zh-CN"),
        ]

        with self.assertRaisesRegex(ValueError, "preserve source ids, timeline"):
            build_polish_batches(
                self.cues,
                draft,
                source_language="en",
                target_language="zh-CN",
            )

    def test_applies_polish_without_changing_cue_metadata(self) -> None:
        draft = [
            Cue(
                cue.id,
                cue.start_ms,
                cue.end_ms,
                cue.source_text,
                target_text=f"draft {cue.id}",
                target_language="zh-CN",
            )
            for cue in self.cues
        ]

        polished = apply_polish_translations(draft, {1: "一", 2: "二", 3: "三"})

        self.assertEqual([cue.target_text for cue in polished], ["一", "二", "三"])
        self.assertEqual(
            [(cue.id, cue.start_ms, cue.end_ms, cue.source_text) for cue in polished],
            [(cue.id, cue.start_ms, cue.end_ms, cue.source_text) for cue in draft],
        )

    def test_semantic_review_rejects_unknown_duplicate_and_unowned_issues(self) -> None:
        candidate = [
            Cue(
                cue.id,
                cue.start_ms,
                cue.end_ms,
                cue.source_text,
                target_text=f"译文{cue.id}",
                target_language="zh-CN",
            )
            for cue in self.cues
        ]
        batch = build_semantic_review_batches(
            self.cues,
            candidate,
            source_language="en",
            target_language="zh-CN",
            batch_size=2,
        )[0]
        valid = {
            "category": "meaning_error",
            "cue_ids": [1],
            "severity": "review",
            "message": "meaning changed",
            "suggested_items": [{"id": 1, "text": "修订"}],
        }

        with self.assertRaisesRegex(ValueError, "unsupported category"):
            parse_semantic_review_response(batch, [{**valid, "category": "style"}])
        with self.assertRaisesRegex(ValueError, "duplicate issue"):
            parse_semantic_review_response(batch, [valid, valid])
        with self.assertRaisesRegex(ValueError, "outside its owned batch"):
            parse_semantic_review_response(batch, [{**valid, "cue_ids": [3]}])

    def test_semantic_repair_covers_complete_group_and_preserves_timeline(self) -> None:
        candidate = [
            Cue(
                cue.id,
                cue.start_ms,
                cue.end_ms,
                cue.source_text,
                target_text=f"译文{cue.id}",
                target_language="zh-CN",
            )
            for cue in self.cues
        ]
        review_batch = build_semantic_review_batches(
            self.cues,
            candidate,
            source_language="en",
            target_language="zh-CN",
        )[0]
        issues = parse_semantic_review_response(
            review_batch,
            [
                {
                    "category": "omission",
                    "cue_ids": [2],
                    "severity": "review",
                    "message": "missing meaning",
                    "suggested_items": [],
                }
            ],
        )
        repair_batch = build_semantic_repair_batches(
            self.cues,
            candidate,
            issues,
            source_language="en",
            target_language="zh-CN",
        )[0]

        with self.assertRaisesRegex(ValueError, "complete semantic group"):
            parse_semantic_repair_response(repair_batch, [{"id": 2, "text": "修订二"}])
        repaired = parse_semantic_repair_response(
            repair_batch,
            [
                {"id": 1, "text": "译文1"},
                {"id": 2, "text": "修订二"},
                {"id": 3, "text": "译文3"},
            ],
        )
        result = apply_semantic_repairs(candidate, repaired, {1, 2, 3})
        self.assertEqual([cue.target_text for cue in result], ["译文1", "修订二", "译文3"])
        self.assertEqual(
            [(cue.id, cue.start_ms, cue.end_ms) for cue in result],
            [(cue.id, cue.start_ms, cue.end_ms) for cue in candidate],
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
    (parse_semantic_repair_response,)
    (parse_semantic_review_response,)

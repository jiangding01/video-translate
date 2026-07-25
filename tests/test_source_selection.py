from __future__ import annotations

import unittest

from xvt.models import Cue
from xvt.source_selection import (
    SourceCandidateKind,
    SourceSelectionAction,
    SourceStrategy,
    evaluate_source_candidate,
    invalid_source_candidate,
    plan_source_selection,
)


class SourceSelectionTest(unittest.TestCase):
    def test_platform_tail_overshoot_is_clipped_at_500ms_boundary(self) -> None:
        cue = Cue(1, 9000, 10500, "Hello", source_language="en", provenance="manual")

        evaluated = evaluate_source_candidate(
            [cue],
            kind=SourceCandidateKind.MANUAL,
            media_duration_ms=10000,
        )

        self.assertEqual(cue.end_ms, 10500)
        self.assertEqual(evaluated.cues[0].end_ms, 10000)
        self.assertEqual([repair.code for repair in evaluated.repairs], ["clip_last_cue_end"])
        self.assertFalse(evaluated.has_errors)

    def test_user_subtitle_timeline_is_never_clipped(self) -> None:
        cue = Cue(1, 9000, 10200, "Hello", source_language="en", provenance="user")

        evaluated = evaluate_source_candidate(
            [cue],
            kind=SourceCandidateKind.USER,
            media_duration_ms=10000,
        )

        self.assertEqual(evaluated.cues[0].end_ms, 10200)
        self.assertEqual(evaluated.repairs, ())
        self.assertTrue(evaluated.has_errors)

    def test_platform_tail_overshoot_above_500ms_is_not_repaired(self) -> None:
        cue = Cue(1, 9000, 10501, "Hello", source_language="en")

        evaluated = evaluate_source_candidate(
            [cue],
            kind=SourceCandidateKind.AUTOMATIC,
            media_duration_ms=10000,
        )

        self.assertEqual(evaluated.cues[0].end_ms, 10501)
        self.assertEqual(evaluated.repairs, ())
        self.assertTrue(evaluated.has_errors)

    def test_overlap_is_not_silently_repaired(self) -> None:
        cues = [
            Cue(1, 0, 2000, "Hello", source_language="en"),
            Cue(2, 1500, 3000, "world", source_language="en"),
        ]

        evaluated = evaluate_source_candidate(
            cues,
            kind=SourceCandidateKind.MANUAL,
            media_duration_ms=5000,
        )

        self.assertTrue(evaluated.has_errors)
        self.assertIn("overlap", {issue.code for issue in evaluated.report.issues})

    def test_auto_manual_candidate_uses_or_aligns_by_qa(self) -> None:
        passed = plan_source_selection(
            SourceStrategy.AUTO,
            candidate_kind=SourceCandidateKind.MANUAL,
            candidate_has_errors=False,
            has_translation_goal=True,
        )
        failed = plan_source_selection(
            SourceStrategy.AUTO,
            candidate_kind=SourceCandidateKind.MANUAL,
            candidate_has_errors=True,
            has_translation_goal=True,
        )

        self.assertEqual(passed.action, SourceSelectionAction.SUBTITLES)
        self.assertEqual(passed.reason, "manual_subtitles_passed")
        self.assertEqual(failed.action, SourceSelectionAction.ALIGN)

    def test_auto_automatic_candidate_depends_on_workflow_goal(self) -> None:
        source_only = plan_source_selection(
            SourceStrategy.AUTO,
            candidate_kind=SourceCandidateKind.AUTOMATIC,
            candidate_has_errors=False,
            has_translation_goal=False,
        )
        translated = plan_source_selection(
            SourceStrategy.AUTO,
            candidate_kind=SourceCandidateKind.AUTOMATIC,
            candidate_has_errors=False,
            has_translation_goal=True,
        )

        self.assertEqual(source_only.action, SourceSelectionAction.SUBTITLES)
        self.assertEqual(source_only.reason, "automatic_subtitles_source_only")
        self.assertEqual(translated.action, SourceSelectionAction.ASR)
        self.assertTrue(translated.reference_allowed)

    def test_auto_rejects_automatic_candidate_errors_as_reference(self) -> None:
        plan = plan_source_selection(
            SourceStrategy.AUTO,
            candidate_kind=SourceCandidateKind.AUTOMATIC,
            candidate_has_errors=True,
            has_translation_goal=True,
        )

        self.assertEqual(plan.action, SourceSelectionAction.ASR)
        self.assertEqual(plan.reason, "candidate_qa_failed")
        self.assertFalse(plan.reference_allowed)

    def test_forced_strategies_are_explicit(self) -> None:
        missing = plan_source_selection(
            SourceStrategy.SUBTITLES,
            candidate_kind=SourceCandidateKind.NONE,
            candidate_has_errors=False,
            has_translation_goal=False,
        )
        forced_asr = plan_source_selection(
            SourceStrategy.ASR,
            candidate_kind=SourceCandidateKind.MANUAL,
            candidate_has_errors=False,
            has_translation_goal=False,
        )

        self.assertEqual(missing.action, SourceSelectionAction.FAIL)
        self.assertEqual(missing.reason, "platform_subtitles_missing")
        self.assertEqual(forced_asr.action, SourceSelectionAction.ASR)
        self.assertTrue(forced_asr.reference_allowed)

    def test_invalid_candidate_is_a_failed_diagnostic(self) -> None:
        evaluation = invalid_source_candidate(
            SourceCandidateKind.AUTOMATIC,
            "invalid subtitle",
        )

        self.assertTrue(evaluation.has_errors)
        self.assertEqual(evaluation.report.status, "failed")
        self.assertEqual(evaluation.report.issues[0].code, "invalid_candidate")


if __name__ == "__main__":
    unittest.main()

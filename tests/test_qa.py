import unittest

from xvt.models import Cue
from xvt.qa import check_subtitles, target_overflow_cue_ids


class SubtitleQaTest(unittest.TestCase):
    def test_target_overflow_ids_only_include_hard_limit_violations(self) -> None:
        cues = [
            Cue(1, 0, 2000, "Short", target_text="短" * 18, target_language="zh-CN"),
            Cue(2, 2000, 4000, "Long", target_text="长" * 19, target_language="zh-CN"),
        ]

        overflow_ids = target_overflow_cue_ids(cues)

        self.assertEqual(overflow_ids, (2,))

    def test_valid_subtitles_pass(self) -> None:
        cues = [Cue(1, 0, 2000, "Hello world", timing_confidence=0.9)]

        report = check_subtitles(cues, media_duration_ms=3000)

        self.assertEqual(report.status, "pass")
        self.assertEqual(report.issues, ())

    def test_overlap_and_missing_translation_fail(self) -> None:
        cues = [
            Cue(1, 0, 1000, "One", target_text="一", target_language="zh-CN"),
            Cue(2, 900, 1800, "Two", target_language="zh-CN"),
        ]

        report = check_subtitles(cues, require_translation=True)

        self.assertEqual(report.status, "failed")
        self.assertEqual(
            {issue.code for issue in report.issues},
            {"overlap", "missing_translation"},
        )

    def test_low_confidence_requires_review(self) -> None:
        cues = [Cue(1, 0, 2000, "Hello", timing_confidence=0.2)]

        report = check_subtitles(cues)

        self.assertEqual(report.status, "needs_review")
        self.assertEqual(report.issues[0].code, "low_timing_confidence")

    def test_trimmed_hallucination_is_recorded_as_warning(self) -> None:
        cues = [
            Cue(
                1,
                0,
                2000,
                "Hello",
                timing_confidence=0.9,
                warnings=["trimmed trailing repeated ASR output with unreliable timestamps"],
            )
        ]

        report = check_subtitles(cues)

        self.assertEqual(report.status, "warning")
        self.assertEqual(report.issues[0].code, "tail_hallucination_trimmed")

    def test_bilingual_line_breaks_do_not_require_review(self) -> None:
        cues = [
            Cue(
                1,
                0,
                3000,
                "Codex and ChatGPT are now\ncoming together into one app.",
                target_text="Codex 与 ChatGPT 合为一应用",
                source_language="en",
                target_language="zh-CN",
            )
        ]

        report = check_subtitles(cues, require_translation=True)

        self.assertNotIn("bilingual_overflow_risk", {issue.code for issue in report.issues})

    def test_horizontal_video_uses_warning_and_review_width_limits(self) -> None:
        cues = [
            Cue(1, 0, 4000, "x" * 65, source_language="en"),
            Cue(2, 4000, 8000, "x" * 73, source_language="en"),
        ]

        report = check_subtitles(cues, video_width=1920, video_height=1080)

        self.assertEqual(report.status, "needs_review")
        self.assertEqual(
            [(issue.cue_id, issue.severity) for issue in report.issues],
            [(1, "warning"), (2, "review")],
        )

    def test_narrow_video_uses_a_stricter_review_width_limit(self) -> None:
        report = check_subtitles(
            [Cue(1, 0, 4000, "x" * 57, source_language="en")],
            video_width=1080,
            video_height=1920,
        )

        self.assertEqual(report.status, "needs_review")


if __name__ == "__main__":
    unittest.main()

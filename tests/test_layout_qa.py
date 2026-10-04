import unittest
from unittest.mock import patch

from xvt.models import Cue
from xvt.process import CommandError, CommandResult
from xvt.qa import check_subtitles
from xvt.render import AssStyleOptions, measure_bilingual_line_widths


class LayoutQaTest(unittest.TestCase):
    def test_long_identifier_that_fits_is_only_a_warning(self):
        cue = Cue(
            1,
            0,
            4000,
            "Hello",
            target_text="或用CLAUDE_CODE_SUBAGENT_MODEL环境变量",
            target_language="zh-CN",
        )
        with patch(
            "xvt.qa.measure_bilingual_line_widths",
            return_value={(1, "target"): (450, 492), (1, "source"): (27, 492)},
        ):
            report = check_subtitles(
                [cue],
                video_width=640,
                video_height=360,
                require_translation=True,
                measure_layout=True,
            )
        self.assertEqual(report.status, "warning")
        self.assertTrue(all(i.severity == "warning" for i in report.issues))
        self.assertIsNotNone(report.layout)

    def test_short_but_wide_text_still_blocks(self):
        cue = Cue(1, 0, 4000, "Hello", target_text="W" * 30, target_language="en")
        with patch(
            "xvt.qa.measure_bilingual_line_widths", return_value={(1, "target"): (530, 492)}
        ):
            report = check_subtitles(
                [cue],
                video_width=640,
                video_height=360,
                require_translation=True,
                measure_layout=True,
            )
        self.assertEqual(report.status, "needs_review")
        self.assertIn("530 exceeds 492 ASS pixels", report.issues[0].message)

    def test_measurement_failure_keeps_conservative_blocker(self):
        cue = Cue(1, 0, 4000, "Hello", target_text="字幕" * 15, target_language="zh-CN")
        with patch(
            "xvt.qa.measure_bilingual_line_widths",
            side_effect=CommandError(CommandResult(("ffmpeg",), 127, "", "missing")),
        ):
            report = check_subtitles(
                [cue],
                video_width=640,
                video_height=360,
                require_translation=True,
                measure_layout=True,
            )
        self.assertEqual(report.status, "needs_review")
        self.assertEqual(report.layout["measurement"], "unavailable")
        self.assertIn("layout_measurement_unavailable", {i.code for i in report.issues})

    def test_narrow_layout_and_custom_font_are_used(self):
        options = AssStyleOptions(target_font="Custom")
        cue = Cue(1, 0, 4000, "Hello", target_text="字幕", target_language="zh-CN")
        with patch(
            "xvt.qa.measure_bilingual_line_widths", return_value={(1, "target"): (100, 90)}
        ) as measure:
            report = check_subtitles(
                [cue],
                video_width=360,
                video_height=640,
                require_translation=True,
                measure_layout=True,
                render_options=options,
            )
        self.assertEqual(report.status, "needs_review")
        self.assertEqual(measure.call_args.kwargs["options"], options)
        self.assertEqual(report.layout["target_font"], "Custom")

    def test_renderer_measures_both_styles_in_one_process(self):
        cue = Cue(1, 0, 4000, "Hello", target_text="字幕", target_language="zh-CN")
        stderr = (
            "n:0 pts:0 pts_time:0 x1:32 x2:71 y1:1 y2:2 w:40 h:2\n"
            "n:1 pts:1 pts_time:1 x1:32 x2:51 y1:1 y2:2 w:20 h:2"
        )
        with patch("xvt.render.run_command", return_value=CommandResult((), 0, "", stderr)) as run:
            result = measure_bilingual_line_widths([cue], video_width=640, video_height=360)
        self.assertEqual(result, {(1, "target"): (40, 492), (1, "source"): (20, 492)})
        self.assertEqual(run.call_count, 1)
        self.assertIn("alpha=1", run.call_args.args[0][run.call_args.args[0].index("-vf") + 1])

    def test_incomplete_measurement_is_rejected(self):
        cue = Cue(1, 0, 4000, "Hello", target_text="字幕")
        with patch("xvt.render.run_command", return_value=CommandResult((), 0, "", "")):
            with self.assertRaisesRegex(ValueError, "incomplete"):
                measure_bilingual_line_widths([cue], video_width=640, video_height=360)

    def test_measurement_touching_canvas_edge_cannot_pass(self):
        cue = Cue(1, 0, 4000, "Hello", target_text="字幕")
        stderr = (
            "n:0 pts:0 x1:4094 x2:4095 y1:1 y2:2 w:2 h:2\nn:1 pts:1 x1:32 x2:51 y1:1 y2:2 w:20 h:2"
        )
        with patch("xvt.render.run_command", return_value=CommandResult((), 0, "", stderr)):
            widths = measure_bilingual_line_widths([cue], video_width=640, video_height=360)
        self.assertGreater(widths[(1, "target")][0], widths[(1, "target")][1])

    def test_missing_glyph_cannot_qualify_for_relaxed_width(self):
        cue = Cue(1, 0, 4000, "Hello", target_text="测" * 25, target_language="zh-CN")
        stderr = (
            "fontselect: failed to find any fallback with glyph 0x6D4B\n"
            "n:0 pts:0 x1:32 x2:364 y1:1 y2:2 w:333 h:2\n"
            "n:1 pts:1 x1:32 x2:51 y1:1 y2:2 w:20 h:2"
        )
        with patch("xvt.render.run_command", return_value=CommandResult((), 0, "", stderr)):
            report = check_subtitles(
                [cue],
                require_translation=True,
                measure_layout=True,
                video_width=640,
                video_height=360,
            )
        self.assertEqual(report.status, "needs_review")
        self.assertEqual(report.layout["measurement"], "unavailable")
        self.assertIn("layout_measurement_unavailable", {i.code for i in report.issues})

    def test_duplicate_cue_ids_fail_even_when_measurement_falls_back(self):
        cues = [Cue(1, 0, 1000, "Hello", "W" * 70), Cue(1, 1000, 2000, "World", "OK")]
        with patch("xvt.qa.measure_bilingual_line_widths", side_effect=ValueError("duplicate")):
            report = check_subtitles(
                cues,
                require_translation=True,
                measure_layout=True,
                video_width=640,
                video_height=360,
            )
        self.assertEqual(report.status, "failed")
        self.assertIn("duplicate_cue_id", {i.code for i in report.issues})

    def test_duplicate_measurement_frame_is_rejected(self):
        cue = Cue(1, 0, 4000, "Hello", target_text="字幕")
        stderr = (
            "n:0 pts:0 x1:32 x2:831 y1:1 y2:2 w:800 h:2\n"
            "n:0 pts:0 x1:32 x2:71 y1:1 y2:2 w:40 h:2\n"
            "n:1 pts:1 x1:32 x2:51 y1:1 y2:2 w:20 h:2"
        )
        with patch("xvt.render.run_command", return_value=CommandResult((), 0, "", stderr)):
            with self.assertRaisesRegex(ValueError, "invalid"):
                measure_bilingual_line_widths([cue], video_width=640, video_height=360)

    def test_measurement_width_must_match_coordinate_span(self):
        cue = Cue(1, 0, 4000, "Hello", target_text="字幕")
        stderr = (
            "n:0 pts:0 x1:32 x2:831 y1:1 y2:2 w:40 h:2\nn:1 pts:1 x1:32 x2:51 y1:1 y2:2 w:20 h:2"
        )
        with patch("xvt.render.run_command", return_value=CommandResult((), 0, "", stderr)):
            with self.assertRaisesRegex(ValueError, "invalid"):
                measure_bilingual_line_widths([cue], video_width=640, video_height=360)

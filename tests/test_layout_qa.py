import unittest
from unittest.mock import patch

from xvt.models import Cue
from xvt.process import CommandError, CommandResult
from xvt.qa import check_subtitles
from xvt.render import AssStyleOptions, FontGlyphError, measure_bilingual_line_widths

# libass 实测输出（macOS / ffmpeg 链接的 libass.9）：逐帧各报一次，同一码位会重复。
_ADLAM_STDERR = (
    "[Parsed_ass_0 @ 0x7fb726c0be00] fontselect: failed to find any fallback with "
    "glyph 0x1E900 for font: (Hiragino Sans GB, 700, 0)\n"
    "[Parsed_ass_0 @ 0x7fb726c0be00] fontselect: failed to find any fallback with "
    "glyph 0x1E900 for font: (Hiragino Sans GB, 700, 0)\n"
)


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
        cue = Cue(1, 0, 4000, "Hello", target_text="字幕" * 20, target_language="zh-CN")
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

    def test_measurement_failure_still_wraps_by_display_units(self):
        # 测量失败回退显示单位规则时，换行照常按同一入口判定：两行放得下即 target_wrapped。
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
        self.assertEqual(report.status, "warning")
        self.assertEqual(report.layout["measurement"], "unavailable")
        self.assertEqual(
            {i.code for i in report.issues}, {"layout_measurement_unavailable", "target_wrapped"}
        )

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
        cue = Cue(1, 0, 4000, "Hello", target_text="测" * 40, target_language="zh-CN")
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

    def test_missing_glyph_names_the_character_and_the_cue(self):
        """缺字不能只说「测量不可用」：要点名哪个码位、哪条 cue、哪一腿。"""

        affected = Cue(
            1, 0, 4000, "Hello", target_text="测试\U0001e900文字", target_language="zh-CN"
        )
        clean = Cue(2, 4000, 8000, "World", target_text="没有问题", target_language="zh-CN")
        with patch("xvt.render.run_command", return_value=CommandResult((), 0, "", _ADLAM_STDERR)):
            report = check_subtitles(
                [affected, clean],
                require_translation=True,
                measure_layout=True,
                video_width=640,
                video_height=360,
            )
        glyph_issues = [i for i in report.issues if i.code == "font_missing_glyph"]
        # 逐帧重复报同一码位，去重后只留一条。
        self.assertEqual(len(glyph_issues), 1)
        issue = glyph_issues[0]
        self.assertEqual((issue.cue_id, issue.severity), (1, "review"))
        self.assertIn("U+1E900", issue.message)
        self.assertIn("target", issue.message)
        self.assertEqual(report.status, "needs_review")
        self.assertEqual(report.layout["measurement"], "unavailable")
        # 全局那条仍在（E2E 的降级断言依赖它），但文案要带上缺字与字体。
        fallback = next(i for i in report.issues if i.code == "layout_measurement_unavailable")
        self.assertIn("U+1E900", fallback.message)
        self.assertIn("Hiragino Sans GB", fallback.message)

    def test_invisible_missing_glyph_does_not_block(self):
        """零宽字符缺字形不影响观感，不能因此把烧录拦下来。"""

        # 零宽空格写成 chr() 而不是字面量：字面量在源码里是看不见的。
        cue = Cue(
            1, 0, 4000, "Hello", target_text=f"字幕{chr(0x200B)}文本", target_language="zh-CN"
        )
        stderr = (
            "fontselect: failed to find any fallback with glyph 0x200B "
            "for font: (Helvetica, 400, 0)"
        )
        with patch("xvt.render.run_command", return_value=CommandResult((), 0, "", stderr)):
            report = check_subtitles(
                [cue],
                require_translation=True,
                measure_layout=True,
                video_width=640,
                video_height=360,
            )
        issue = next(i for i in report.issues if i.code == "font_missing_glyph")
        self.assertEqual(issue.severity, "warning")
        self.assertEqual(report.status, "warning")

    def test_missing_glyph_outside_cue_text_only_warns_globally(self):
        """码位不在任何 cue 文本里（来自样式/头部）时不编归属，只留全局 warning。"""

        cue = Cue(1, 0, 4000, "Hello", target_text="字幕", target_language="zh-CN")
        stderr = (
            "fontselect: failed to find any fallback with glyph 0x17000 "
            "for font: (Helvetica, 400, 0)"
        )
        with patch("xvt.render.run_command", return_value=CommandResult((), 0, "", stderr)):
            report = check_subtitles(
                [cue],
                require_translation=True,
                measure_layout=True,
                video_width=640,
                video_height=360,
            )
        self.assertEqual([i.code for i in report.issues], ["layout_measurement_unavailable"])
        self.assertIn("U+17000", report.issues[0].message)
        self.assertEqual(report.status, "warning")

    def test_non_glyph_measurement_failure_keeps_the_legacy_warning(self):
        """缺字之外的测量失败（ffmpeg 缺失、bbox 解析不出）文案与严重度一字不变。"""

        cue = Cue(1, 0, 4000, "Hello", target_text="字幕", target_language="zh-CN")
        with patch(
            "xvt.qa.measure_bilingual_line_widths", side_effect=ValueError("measurement is invalid")
        ):
            report = check_subtitles(
                [cue],
                require_translation=True,
                measure_layout=True,
                video_width=640,
                video_height=360,
            )
        self.assertEqual([i.code for i in report.issues], ["layout_measurement_unavailable"])
        self.assertEqual(
            report.issues[0].message,
            "rendered width unavailable; conservative display-unit limits applied",
        )
        self.assertEqual(report.status, "warning")

    def test_glyph_error_carries_deduplicated_codepoints_and_fonts(self):
        """``FontGlyphError`` 带出码位与字体名；字体名那段在截断消息里可缺省。"""

        cue = Cue(1, 0, 4000, "Hello", target_text="字幕", target_language="zh-CN")
        stderr = _ADLAM_STDERR + "fontselect: failed to find any fallback with glyph 0x6D4B\n"
        with patch("xvt.render.run_command", return_value=CommandResult((), 0, "", stderr)):
            with self.assertRaises(FontGlyphError) as caught:
                measure_bilingual_line_widths([cue], video_width=640, video_height=360)
        self.assertEqual(caught.exception.codepoints, (0x1E900, 0x6D4B))
        self.assertEqual(caught.exception.fonts, ("Hiragino Sans GB",))

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

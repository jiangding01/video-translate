import json
import unittest
from dataclasses import asdict
from unittest.mock import patch

from xvt.candidate_qa import overflow_limits
from xvt.models import Cue
from xvt.qa import check_subtitles, target_overflow_cue_ids
from xvt.render import resolved_style_binding
from xvt.segment import target_wrap_binding


class SubtitleQaTest(unittest.TestCase):
    def test_target_overflow_ids_include_every_cue_that_entered_wrapping(self) -> None:
        cues = [
            Cue(1, 0, 2000, "Short", target_text="短" * 18, target_language="zh-CN"),
            # 40 字切不开仍是 review；19 字能换行成 target_wrapped。两者整段都超过
            # target_wrap_limit、进入过换行分支，都计入，否则「能切开但 libass 实测仍超
            # 像素」的 cue 在 QA 里是 review、在 resegment-translation 里却成了 no-op。
            Cue(2, 2000, 4000, "Long", target_text="长" * 40, target_language="zh-CN"),
            Cue(3, 4000, 6000, "Wraps", target_text="长" * 19, target_language="zh-CN"),
        ]

        overflow_ids = target_overflow_cue_ids(cues)

        self.assertEqual(overflow_ids, (2, 3))

    def test_reading_speed_ignores_punctuation_and_halves_latin_in_cjk(self) -> None:
        # 旧口径按去空白字符数计：「OpenAI GPT-5 发布了。」= 15，1.6s 内 9.4/s > 9，会报 warning。
        # 新口径 = 8 个单位（Latin 半字、连字符与句号免费），5.0/s，不该红。
        cues = [
            Cue(
                1,
                0,
                1600,
                "OpenAI released GPT-5.",
                source_language="en",
                target_text="OpenAI GPT-5 发布了。",
                target_language="zh-CN",
            )
        ]

        report = check_subtitles(cues, media_duration_ms=3000, require_translation=True)

        self.assertNotIn("reading_speed", [issue.code for issue in report.issues])

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

    def test_asr_low_confidence_is_warning(self) -> None:
        cues = [Cue(1, 0, 2000, "Hello", timing_confidence=0.2, provenance="asr")]

        report = check_subtitles(cues)

        self.assertEqual(report.status, "warning")
        self.assertEqual(report.issues[0].code, "low_timing_confidence")
        self.assertEqual(report.issues[0].severity, "warning")

    def test_aligned_low_confidence_requires_review(self) -> None:
        cues = [Cue(1, 0, 2000, "Hello", timing_confidence=0.2, provenance="aligned")]

        report = check_subtitles(cues)

        self.assertEqual(report.status, "needs_review")
        self.assertEqual(report.issues[0].code, "low_timing_confidence")
        self.assertEqual(report.issues[0].severity, "review")

    def test_user_source_low_confidence_requires_review(self) -> None:
        cues = [
            Cue(
                1,
                0,
                2000,
                "Hello",
                timing_confidence=0.0,
                provenance="user_source_subtitle",
            )
        ]

        report = check_subtitles(cues)

        self.assertEqual(report.status, "needs_review")
        self.assertEqual(report.issues[0].code, "low_timing_confidence")

    def test_glossary_terms_require_exact_target_text(self) -> None:
        report = check_subtitles(
            [
                Cue(
                    1,
                    0,
                    2000,
                    "OpenAI uses AI.",
                    target_text="开放人工智能使用人工智能。",
                    source_language="en",
                    target_language="zh-CN",
                )
            ],
            require_translation=True,
            required_terms=(("OpenAI", "OpenAI"), ("AI", "人工智能")),
        )

        self.assertEqual(report.status, "failed")
        self.assertEqual(
            [(issue.code, issue.cue_id) for issue in report.issues],
            [("glossary_violation", 1)],
        )

    def test_glossary_terms_do_not_match_inside_ascii_words(self) -> None:
        report = check_subtitles(
            [
                Cue(
                    1,
                    0,
                    2000,
                    "A chair is useful.",
                    target_text="一把椅子很有用。",
                    source_language="en",
                    target_language="zh-CN",
                ),
                Cue(
                    2,
                    2000,
                    4000,
                    "机器学习很有用。",
                    target_text="Machine learning is useful.",
                    source_language="zh-CN",
                    target_language="en",
                ),
            ],
            require_translation=True,
            required_terms=(("AI", "人工智能"), ("机器学习", "Machine learning")),
        )

        self.assertEqual(report.status, "pass")
        self.assertNotIn("glossary_violation", {issue.code for issue in report.issues})

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

    def test_source_and_target_overflow_are_reported_independently(self) -> None:
        """源文超宽不能吞掉译文超宽，否则下游拿不到 target 阻断项。"""

        cues = [
            Cue(
                1,
                0,
                4000,
                "x" * 120,
                target_text="长" * 60,
                source_language="en",
                target_language="zh-CN",
            )
        ]

        report = check_subtitles(cues, require_translation=True)

        overflow = [issue for issue in report.issues if issue.code == "bilingual_overflow_risk"]
        self.assertEqual(len(overflow), 2)
        self.assertTrue(overflow[0].message.startswith("source line width "))
        self.assertTrue(overflow[1].message.startswith("target line width "))
        self.assertEqual([issue.severity for issue in overflow], ["review", "review"])

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


def _bilingual_cue(target_text: str, *, source_text: str = "Short source", **kwargs) -> Cue:
    return Cue(
        1,
        0,
        6000,
        source_text,
        target_text=target_text,
        source_language="en",
        target_language=kwargs.pop("target_language", "zh-CN"),
        timing_confidence=0.9,
        **kwargs,
    )


class TargetWrapQaTest(unittest.TestCase):
    def test_wrappable_target_is_a_target_wrapped_warning(self) -> None:
        report = check_subtitles([_bilingual_cue("长" * 19)], require_translation=True)

        self.assertEqual(report.status, "warning")
        self.assertEqual(
            [(issue.code, issue.severity, issue.message) for issue in report.issues],
            [
                (
                    "target_wrapped",
                    "warning",
                    "target wrapped into 2 lines (widest 20 display units)",
                )
            ],
        )
        self.assertNotIn("bilingual_overflow_risk", {issue.code for issue in report.issues})

    def test_unfittable_target_stays_review_and_limits_still_parse(self) -> None:
        report = check_subtitles([_bilingual_cue("长" * 40)], require_translation=True)

        self.assertEqual(report.status, "needs_review")
        (issue,) = report.issues
        self.assertEqual(issue.code, "bilingual_overflow_risk")
        self.assertEqual(
            issue.message,
            "target line width 80 exceeds 36 display units after wrapping into 2 lines",
        )
        self.assertEqual(overflow_limits(issue.message), (80, 36))

    def test_rendered_overflow_after_wrapping_stays_review(self) -> None:
        with patch(
            "xvt.qa.measure_bilingual_line_widths",
            return_value={(1, "target"): (530, 492), (1, "source"): (27, 492)},
        ):
            report = check_subtitles(
                [_bilingual_cue("长" * 19)],
                video_width=1920,
                video_height=1080,
                require_translation=True,
                measure_layout=True,
            )

        self.assertEqual(report.status, "needs_review")
        (issue,) = report.issues
        self.assertEqual(issue.code, "bilingual_overflow_risk")
        self.assertEqual(
            issue.message,
            "target line width 530 exceeds 492 ASS pixels after wrapping into 2 lines",
        )
        self.assertEqual(overflow_limits(issue.message), (530, 492))
        self.assertEqual(report.layout["wrap"], target_wrap_binding())

    def test_layout_records_the_style_actually_used(self) -> None:
        """E2：layout 要记**实际生效**的字体字号，而不只是用户的显式覆盖。

        `style_options` 在未指定字体时跨平台逐字节相同，光绑它挡不住「macOS 测量、
        Linux 复用」；`resolved_style` 绑的是推导之后的输出，且与渲染腿共用一份推导。
        measurement 失败时照样记——样式本身是确定的，不依赖测量成功与否。
        """

        cues = [
            Cue(
                1,
                0,
                1000,
                "Hello",
                target_text="你好",
                source_language="en",
                target_language="zh-CN",
            )
        ]
        expected = resolved_style_binding(cues, video_height=1080)
        with patch(
            "xvt.qa.measure_bilingual_line_widths",
            return_value={(1, "target"): (40, 492), (1, "source"): (27, 492)},
        ):
            report = check_subtitles(
                cues,
                media_duration_ms=1000,
                video_width=1920,
                video_height=1080,
                require_translation=True,
                measure_layout=True,
            )
        self.assertEqual(report.layout["resolved_style"], expected)
        # 用户没指定字体时 style_options 里仍是 None，正是它挡不住跨机错配的原因。
        self.assertIsNone(report.layout["style_options"]["target_font"])

        with patch("xvt.qa.measure_bilingual_line_widths", side_effect=ValueError("no font")):
            degraded = check_subtitles(
                cues,
                media_duration_ms=1000,
                video_width=1920,
                video_height=1080,
                require_translation=True,
                measure_layout=True,
            )
        self.assertEqual(degraded.layout["measurement"], "unavailable")
        self.assertEqual(degraded.layout["resolved_style"], expected)

    def test_rendered_fit_after_wrapping_is_a_target_wrapped_warning(self) -> None:
        with patch(
            "xvt.qa.measure_bilingual_line_widths",
            return_value={(1, "target"): (300, 492), (1, "source"): (27, 492)},
        ):
            report = check_subtitles(
                [_bilingual_cue("长" * 19)],
                video_width=1920,
                video_height=1080,
                require_translation=True,
                measure_layout=True,
            )

        self.assertEqual(report.status, "warning")
        self.assertEqual([issue.code for issue in report.issues], ["target_wrapped"])
        self.assertEqual(report.layout["wrap"], {"version": 2, "max_lines": 2})

    def test_kinsoku_violating_only_break_stays_review(self) -> None:
        # 两行各 18 字的上限下唯一放得下的切点落在 `」` 之前：不切，维持 review。
        text = "长" * 18 + "」" + "长" * 17
        report = check_subtitles([_bilingual_cue(text)], require_translation=True)

        self.assertEqual(report.status, "needs_review")
        (issue,) = report.issues
        self.assertEqual(issue.code, "bilingual_overflow_risk")
        self.assertEqual(overflow_limits(issue.message), (72, 36))
        self.assertTrue(issue.message.endswith(" after wrapping into 2 lines"))

    def test_source_overflow_never_wraps_and_keeps_its_message(self) -> None:
        source = "word " * 20 + "end"
        bilingual = check_subtitles(
            [_bilingual_cue("你好", source_text=source)], require_translation=True
        )
        self.assertEqual(
            [(issue.code, issue.severity, issue.message) for issue in bilingual.issues],
            [
                (
                    "bilingual_overflow_risk",
                    "review",
                    "source line width 103 exceeds 72 display units",
                )
            ],
        )

        monolingual = check_subtitles(
            [Cue(1, 0, 6000, source, source_language="en", timing_confidence=0.9)]
        )
        self.assertEqual(
            [(issue.code, issue.severity, issue.message) for issue in monolingual.issues],
            [("line_too_wide", "review", "source line width 103 exceeds 72 display units")],
        )

    def test_report_bytes_for_cues_within_limit_are_unchanged(self) -> None:
        """D6 回归守卫：整段不超限的 cue 不进换行，报告字节与换行引入前逐字相同。"""

        report = check_subtitles([_bilingual_cue("你好世界")], require_translation=True)

        self.assertEqual(report.issues, ())
        self.assertIsNone(report.layout)
        self.assertEqual(
            json.dumps(asdict(report), sort_keys=True),
            '{"cue_count": 1, "issues": [], "layout": null, "status": "pass"}',
        )

        preferred = check_subtitles(
            [_bilingual_cue("x" * 70, target_language="en")], require_translation=True
        )
        self.assertEqual(
            json.dumps(asdict(preferred), sort_keys=True),
            '{"cue_count": 1, "issues": [{"code": "bilingual_overflow_risk", '
            '"cue_id": 1, "message": "target line width 70 exceeds preferred 64 display units", '
            '"severity": "warning"}], "layout": null, "status": "warning"}',
        )


if __name__ == "__main__":
    unittest.main()

import unittest

from xvt.segment import (
    target_wrap_binding,
    target_wrap_limit,
    wrap_overflowing_target,
    wrap_target_lines,
)
from xvt.textwidth import display_width, display_width_review_limit


class WrapTargetLinesTest(unittest.TestCase):
    def test_latin_text_breaks_between_words(self) -> None:
        text = "The quick brown fox jumps over the lazy dog and keeps running far away"
        lines = wrap_target_lines(text, "en", max_width=40)

        self.assertIsNotNone(lines)
        assert lines is not None
        self.assertEqual(len(lines), 2)
        self.assertEqual(" ".join(lines), text)
        words = set(text.split())
        for line in lines:
            self.assertEqual(line, line.strip())
            self.assertLessEqual(display_width(line), 40)
            self.assertTrue(set(line.split()) <= words)

    def test_cjk_text_breaks_between_characters(self) -> None:
        text = "长" * 30
        lines = wrap_target_lines(text, "zh-CN", max_width=36)

        self.assertIsNotNone(lines)
        assert lines is not None
        self.assertEqual("".join(lines), text)
        self.assertEqual([display_width(line) for line in lines], [30, 30])

    def test_cjk_text_keeps_latin_and_digit_runs_whole(self) -> None:
        text = "我们今天要讨论的是OpenAI GPT-5模型在长文本理解上的表现和局限性问题"
        lines = wrap_target_lines(text, "zh-CN", max_width=36)

        self.assertIsNotNone(lines)
        assert lines is not None
        self.assertEqual("".join(lines), text)
        self.assertEqual(sum("OpenAI GPT-5" in line for line in lines), 1)
        for line in lines:
            self.assertLessEqual(display_width(line), 36)

    def test_korean_text_breaks_on_whitespace(self) -> None:
        text = "오늘은 우리가 함께 자막 줄바꿈 기능에 대해서 이야기를 해요"
        lines = wrap_target_lines(text, "ko", max_width=display_width(text) * 3 // 4)

        self.assertIsNotNone(lines)
        assert lines is not None
        self.assertEqual(len(lines), 2)
        self.assertEqual(" ".join(lines), text)

    def test_cjk_soft_punctuation_is_preferred_break(self) -> None:
        text = "我们需要保留每个字幕的含义，但是也要保证每一行都清晰可读"
        lines = wrap_target_lines(text, "zh-CN", max_width=36)

        self.assertEqual(lines, ("我们需要保留每个字幕的含义，", "但是也要保证每一行都清晰可读"))

    def test_forbidden_only_break_returns_none(self) -> None:
        # 两行各 18 字的上限下唯一放得下的切点落在 `」` 之前，违反行首禁则，不切。
        text = "长" * 18 + "」" + "长" * 17
        self.assertIsNone(wrap_target_lines(text, "zh-CN", max_width=36))

    def test_single_token_returns_none(self) -> None:
        self.assertIsNone(wrap_target_lines("Supercalifragilistic" * 4, "en", max_width=40))
        self.assertIsNone(wrap_target_lines("OpenAIGPT5" * 8, "zh-CN", max_width=36))

    def test_unfittable_text_returns_none(self) -> None:
        self.assertIsNone(wrap_target_lines("长" * 40, "zh-CN", max_width=36))

    def test_max_lines_below_two_returns_none(self) -> None:
        self.assertIsNone(wrap_target_lines("长" * 30, "zh-CN", max_width=36, max_lines=1))

    def test_every_line_fits_max_width(self) -> None:
        cases = (
            ("en", "word " * 25, 72),
            ("en", "a fairly long sentence, with commas; and clauses " * 2, 64),
            ("zh-CN", "这是一条明显超过单行显示宽度限制的中文字幕需要换行", 36),
            ("ja", "これは字幕の幅を超える長い日本語の文章です。改行が必要です", 36),
        )
        for language, text, max_width in cases:
            with self.subTest(language=language, text=text):
                lines = wrap_target_lines(text, language, max_width=max_width)
                self.assertIsNotNone(lines)
                assert lines is not None
                for line in lines:
                    self.assertTrue(line)
                    self.assertLessEqual(display_width(line), max_width)


class WrapOverflowingTargetTest(unittest.TestCase):
    def test_limit_is_the_review_limit(self) -> None:
        for language, width, height in (
            ("zh-CN", None, None),
            ("en", 1080, 1920),
            ("en", 1920, 1080),
        ):
            with self.subTest(language=language, width=width, height=height):
                self.assertEqual(
                    target_wrap_limit(language, video_width=width, video_height=height),
                    display_width_review_limit(language, video_width=width, video_height=height),
                )

    def test_text_within_limit_is_not_wrapped(self) -> None:
        self.assertIsNone(wrap_overflowing_target("长" * 18, "zh-CN"))

    def test_text_over_limit_is_wrapped(self) -> None:
        self.assertEqual(wrap_overflowing_target("长" * 19, "zh-CN"), ("长" * 9, "长" * 10))

    def test_binding_is_a_fresh_copy(self) -> None:
        binding = target_wrap_binding()
        binding["max_lines"] = 3
        self.assertEqual(target_wrap_binding(), {"version": 1, "max_lines": 2})


if __name__ == "__main__":
    unittest.main()

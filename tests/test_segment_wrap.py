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


class WrapKatakanaRunTest(unittest.TestCase):
    """片假名连续段整体算一个 token（与 Latin / 数字连续段同口径）。

    禁则表挡不住这类切点：小字假名与长音符在 ``NO_LINE_START`` 里，但
    ``グラフィッ|クス`` 这种普通片假名之间的位置照样放行，所以必须在分词层保住。
    """

    def test_katakana_run_is_not_split(self) -> None:
        self.assertEqual(
            wrap_target_lines(
                "新しいグラフィックスエンジンの設計について話します", "ja", max_width=36
            ),
            ("新しいグラフィックスエンジン", "の設計について話します"),
        )
        self.assertEqual(
            wrap_target_lines(
                "これはユーザーインターフェースの設計に関する重要な話です", "ja", max_width=36
            ),
            ("これはユーザーインターフェース", "の設計に関する重要な話です"),
        )

    def test_middle_dot_stays_a_break_point(self) -> None:
        # `・`(U+30FB) 刻意排除在片假名段外：并进去 `アイス・コーヒー` 就再也切不开，
        # 而 `・` 后换行本来合法（`・` 在 NO_LINE_START，所以也不会被切到行首）。
        self.assertEqual(
            wrap_target_lines(
                "アイス・コーヒーとホット・コーヒーのどちらがお好みですか", "ja", max_width=36
            ),
            ("アイス・コーヒーとホット・", "コーヒーのどちらがお好みですか"),
        )

    def test_halfwidth_katakana_run_is_not_split(self) -> None:
        # normalize_display_text 只折叠空白、不做半角转全角，所以半角也要整段保住，
        # 否则同一个词会因半角全角而行为不一致。
        self.assertEqual(
            wrap_target_lines("これはｸﾞﾗﾌｨｯｸｽｴﾝｼﾞﾝの設計について話しますよ", "ja", max_width=36),
            ("これはｸﾞﾗﾌｨｯｸｽｴﾝｼﾞﾝの", "設計について話しますよ"),
        )

    def test_latin_and_katakana_runs_both_stay_whole(self) -> None:
        lines = wrap_target_lines(
            "AIグラフィックスの処理速度が大幅に向上したという話です", "ja", max_width=36
        )

        self.assertIsNotNone(lines)
        assert lines is not None
        for run in ("AI", "グラフィックス"):
            self.assertEqual(sum(run in line for line in lines), 1, run)

    def test_oversized_katakana_run_returns_none(self) -> None:
        # 硬形态：段整体放不下就维持 review，与 Latin 单 token 放不下时同口径
        # （test_single_token_returns_none）。段宽 32 + 前缀 6 与 + 后缀 10 都超过 36，
        # 而前后缀不相邻无法合并成一行，所以无解。
        text = "これは" + "ア" * 16 + "の設計です"
        self.assertEqual(display_width(text), 48)
        self.assertIsNone(wrap_target_lines(text, "ja", max_width=36))

    def test_japanese_without_katakana_is_unchanged(self) -> None:
        # 「不该红时不红」：不含片假名的日文句子切分逐字不变。
        self.assertEqual(
            wrap_target_lines(
                "これは字幕の幅を超える長い日本語の文章です。改行が必要です", "ja", max_width=36
            ),
            ("これは字幕の幅を超える長い日", "本語の文章です。改行が必要です"),
        )

    def test_hiragana_and_kanji_are_still_split_by_character(self) -> None:
        # 平假名与汉字刻意不收：那是词法边界，要分词器才挡得住（P7 ②），这里只保证
        # 它们仍按字切、不会被片假名段的区间误吞。
        lines = wrap_target_lines(
            "ともだちといっしょにこうえんまでさんぽにいきました", "ja", max_width=36
        )

        self.assertIsNotNone(lines)
        assert lines is not None
        self.assertEqual("".join(lines), "ともだちといっしょにこうえんまでさんぽにいきました")


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
        self.assertEqual(target_wrap_binding(), {"version": 2, "max_lines": 2})


if __name__ == "__main__":
    unittest.main()

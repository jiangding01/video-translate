import unittest

from xvt.models import Cue
from xvt.textwidth import is_japanese_or_korean, reading_speed_limit, reading_units
from xvt.translate import build_translation_batches


class ReadingSpeedLimitTest(unittest.TestCase):
    def test_limit_per_language(self) -> None:
        self.assertEqual(reading_speed_limit("ja"), 13)
        self.assertEqual(reading_speed_limit("ko-KR"), 13)
        self.assertEqual(reading_speed_limit("zh-CN"), 9)
        self.assertEqual(reading_speed_limit("en"), 20)
        self.assertEqual(reading_speed_limit(None), 20)

    def test_is_japanese_or_korean(self) -> None:
        self.assertTrue(is_japanese_or_korean("JA"))
        self.assertTrue(is_japanese_or_korean("ko"))
        self.assertFalse(is_japanese_or_korean("zh-CN"))
        self.assertFalse(is_japanese_or_korean(None))

    def _budget(self, duration_ms: int) -> int:
        cues = [Cue(1, 0, duration_ms, "Hello")]
        batches = build_translation_batches(cues, source_language="en", target_language="ja")
        return batches[0].items[0].max_target_characters

    def test_short_ja_cue_budget_is_relaxed(self) -> None:
        # 1.5s * 13 = 19 -> 封顶 18（旧值 1.5s * 9 = 13）。
        self.assertEqual(self._budget(1500), 18)

    def test_long_ja_cue_budget_is_still_capped(self) -> None:
        # 2.5s 时 9/s 与 13/s 都超过 18，B 只影响 <2s 的 cue。
        self.assertEqual(self._budget(2500), 18)


class ReadingUnitsTest(unittest.TestCase):
    def test_pure_cjk_matches_non_space_character_count(self) -> None:
        self.assertEqual(reading_units("你好世界", "zh-CN"), 4)
        self.assertEqual(reading_units("こんにちは 世界", "ja"), 7)

    def test_latin_inside_cjk_counts_half(self) -> None:
        # OpenAI(6) GPT(3) 5(1) = 10 个 ASCII 字母数字 -> 5；发布了 -> 3；连字符、空格、句号免费。
        # 字符数（去空白）为 15。
        self.assertEqual(reading_units("OpenAI GPT-5 发布了。", "zh-CN"), 8)

    def test_half_units_round_up(self) -> None:
        self.assertEqual(reading_units("A", "zh-CN"), 1)
        self.assertEqual(reading_units("AB你", "zh-CN"), 2)

    def test_non_cjk_only_drops_punctuation_and_space(self) -> None:
        self.assertEqual(reading_units("Hello, world!", "en"), 10)
        self.assertEqual(reading_units("Hello\nworld", "en"), 10)

    def test_all_punctuation_is_zero(self) -> None:
        self.assertEqual(reading_units("……，。！？ - ", "zh-CN"), 0)
        self.assertEqual(reading_units("...!?", "en"), 0)


if __name__ == "__main__":
    unittest.main()

import unittest

from xvt.models import Cue
from xvt.textwidth import is_japanese_or_korean, reading_speed_limit
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


if __name__ == "__main__":
    unittest.main()

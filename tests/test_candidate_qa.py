import unittest

from xvt.candidate_qa import overflow_limits


class OverflowLimitsTest(unittest.TestCase):
    def test_extracts_width_and_hard_limit_from_review_message(self) -> None:
        self.assertEqual(
            overflow_limits("target line width 41 exceeds 36 display units"),
            (41, 36),
        )

    def test_ignores_soft_limit_warning_message(self) -> None:
        """``exceeds preferred N`` 是 warning 形态，不是阻断上限，不能当硬上限报出去。"""

        self.assertIsNone(
            overflow_limits("target line width 41 exceeds preferred 36 display units")
        )

    def test_returns_none_for_unrelated_message(self) -> None:
        self.assertIsNone(overflow_limits("translated text is missing"))


if __name__ == "__main__":
    unittest.main()

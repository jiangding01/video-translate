import unittest

from xvt.align import align_source_cues, normalize_text
from xvt.models import Cue, Transcript, Word
from xvt.qa import check_subtitles


class AlignTest(unittest.TestCase):
    def test_normalize_text_removes_spacing_case_and_punctuation(self) -> None:
        self.assertEqual(normalize_text(" Hello, WORLD! "), "helloworld")

    def test_aligns_source_cues_to_word_boundaries(self) -> None:
        transcript = Transcript(
            language="en",
            engine="mlx",
            words=(
                Word("Hello", 1000, 1300),
                Word(" world", 1320, 1700),
                Word("Next", 2500, 2800),
                Word(" line", 2820, 3200),
            ),
        )
        cues = [
            Cue(1, 0, 600, "Hello world"),
            Cue(2, 1500, 2200, "Next line"),
        ]

        result = align_source_cues(cues, transcript)

        self.assertEqual(
            [(cue.start_ms, cue.end_ms) for cue in result.cues], [(1000, 1700), (2500, 3200)]
        )
        self.assertEqual(len(result.matches), 2)
        self.assertGreater(result.confidence, 0.9)

    def test_unmatched_cue_is_shifted_by_neighboring_anchor(self) -> None:
        transcript = Transcript(
            language="en",
            engine="mlx",
            words=(Word("Hello", 1000, 1500),),
        )
        cues = [Cue(1, 0, 500, "Hello"), Cue(2, 1000, 1500, "Unknown phrase")]

        result = align_source_cues(cues, transcript)

        self.assertEqual((result.cues[1].start_ms, result.cues[1].end_ms), (2000, 2500))
        self.assertIn("inferred", result.cues[1].warnings[0])

    def test_unmatched_cue_does_not_alias_the_input_word_list(self) -> None:
        # 守卫：unmatched 分支的 replace() 必须复制 words，否则对齐结果与调用方的
        # 原始 cue 共享同一个列表对象（matched 分支因重新切片天然不共享）。
        transcript = Transcript(
            language="en",
            engine="mlx",
            words=(Word("Hello", 1000, 1500),),
        )
        original = Cue(2, 1000, 1500, "Unknown phrase", words=[Word("Unknown", 0, 500)])
        cues = [Cue(1, 0, 500, "Hello"), original]

        result = align_source_cues(cues, transcript)

        self.assertEqual(result.cues[1].words, original.words)
        self.assertIsNot(result.cues[1].words, original.words)

    def test_nested_overlap_is_pushed_after_the_previous_cue(self) -> None:
        # 源字幕 cue1 可匹配 ASR，cue2 与 cue1 重叠且匹配不上：平移后完全嵌套，
        # 旧的中点归一在这里会产出 end <= start 的 cue。
        transcript = Transcript(
            language="en",
            engine="test",
            words=(Word("hello", 10000, 11000, 0.9), Word("world", 11000, 12000, 0.9)),
        )
        cues = [Cue(1, 0, 1000, "hello world"), Cue(2, 0, 500, "zzqqxx unmatched gibberish")]

        result = align_source_cues(cues, transcript)

        self.assertEqual(
            [(cue.start_ms, cue.end_ms) for cue in result.cues], [(10000, 12000), (12000, 12001)]
        )
        for index, cue in enumerate(result.cues):
            self.assertGreater(cue.end_ms, cue.start_ms)
            if index:
                self.assertGreaterEqual(cue.start_ms, result.cues[index - 1].end_ms)
        self.assertIn(
            "shifted cue start past the previous cue to resolve a degenerate overlap",
            result.cues[1].warnings,
        )
        # 入参 cue 的 warnings 不得被就地污染。
        self.assertEqual(cues[0].warnings, [])

    def test_reports_no_error_level_issues_after_clamping(self) -> None:
        transcript = Transcript(
            language="en",
            engine="test",
            words=(Word("hello", 10000, 11000, 0.9), Word("world", 11000, 12000, 0.9)),
        )
        cues = [Cue(1, 0, 1000, "hello world"), Cue(2, 0, 500, "zzqqxx unmatched gibberish")]

        result = align_source_cues(cues, transcript)
        report = check_subtitles(list(result.cues))

        self.assertEqual([issue for issue in report.issues if issue.severity == "error"], [])


if __name__ == "__main__":
    unittest.main()

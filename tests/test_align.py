import unittest

from xvt.align import align_source_cues, normalize_text
from xvt.models import Cue, Transcript, Word


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


if __name__ == "__main__":
    unittest.main()

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from xvt.models import Transcript, Word
from xvt.segment import SegmentationConfig, segment_transcript
from xvt.transcribe import transcribe_mlx, words_from_faster_segments, words_from_mlx_segments


class TranscriptionTest(unittest.TestCase):
    def test_mlx_uses_builtin_hallucination_guards(self) -> None:
        mlx_whisper = SimpleNamespace(
            transcribe=lambda *_args, **_kwargs: {
                "language": "en",
                "segments": [
                    {"words": [{"word": " Hello", "start": 0.0, "end": 0.5, "probability": 0.9}]}
                ],
            }
        )
        with patch("xvt.transcribe.importlib.import_module", return_value=mlx_whisper):
            with patch.object(
                mlx_whisper, "transcribe", wraps=mlx_whisper.transcribe
            ) as transcribe:
                transcribe_mlx(Path("audio.wav"))

        options = transcribe.call_args.kwargs
        self.assertFalse(options["condition_on_previous_text"])
        self.assertEqual(options["hallucination_silence_threshold"], 1.0)

    def test_converts_mlx_word_timestamps_to_milliseconds(self) -> None:
        words = words_from_mlx_segments(
            [{"words": [{"word": " Hello", "start": 0.125, "end": 0.75, "probability": 0.9}]}]
        )

        self.assertEqual(words, (Word(" Hello", 125, 750, 0.9),))

    def test_converts_faster_whisper_words(self) -> None:
        item = SimpleNamespace(word=" world", start=1.0, end=1.5, probability=0.8)
        segment = SimpleNamespace(words=[item])

        words = words_from_faster_segments([segment])

        self.assertEqual(words, (Word(" world", 1000, 1500, 0.8),))

    def test_segments_on_sentence_end_and_pause(self) -> None:
        transcript = Transcript(
            language="en",
            engine="mlx",
            words=(
                Word("Hello", 0, 300, 0.9),
                Word(" world.", 320, 700, 0.8),
                Word(" Next", 900, 1200, 0.7),
                Word(" thought", 1900, 2300, 0.9),
            ),
        )

        cues = segment_transcript(transcript)

        self.assertEqual([cue.source_text for cue in cues], ["Hello world.", "Next", "thought"])
        self.assertEqual([cue.id for cue in cues], [1, 2, 3])
        self.assertLessEqual(cues[0].end_ms, cues[1].start_ms)
        self.assertLessEqual(cues[1].end_ms, cues[2].start_ms)

    def test_long_cue_prefers_soft_punctuation(self) -> None:
        transcript = Transcript(
            language="en",
            engine="mlx",
            words=(
                Word("one", 0, 400),
                Word(", two", 500, 1000),
                Word(" three", 1100, 1600),
                Word(" four", 1700, 2200),
            ),
        )

        cues = segment_transcript(
            transcript,
            SegmentationConfig(max_duration_ms=1500, tail_padding_ms=0),
        )

        self.assertEqual([cue.source_text for cue in cues], ["one, two", "three four"])

    def test_overlapping_word_timestamps_produce_non_overlapping_cues(self) -> None:
        transcript = Transcript(
            language="en",
            engine="mlx",
            words=(
                Word("First.", 0, 1000),
                Word(" Second.", 900, 1600),
            ),
        )

        cues = segment_transcript(transcript, SegmentationConfig(tail_padding_ms=0))

        self.assertEqual(cues[0].end_ms, cues[1].start_ms)

    def test_trims_low_confidence_repeated_tail_and_records_warning(self) -> None:
        transcript = Transcript(
            language="en",
            engine="mlx",
            words=(
                Word("Done.", 0, 1000, 0.99),
                Word(" Maybe", 1100, 1300, 0.1),
                Word(" applications", 1300, 1400, 0.2),
                Word(" applications", 1400, 1500, 0.2),
                Word(" applications", 1500, 1600, 0.2),
            ),
        )

        cues = segment_transcript(transcript, media_duration_ms=1500)

        self.assertEqual([cue.source_text for cue in cues], ["Done."])
        self.assertIn(
            "trimmed trailing repeated ASR output with unreliable timestamps",
            cues[0].warnings,
        )
        self.assertEqual(cues[0].end_ms, 1150)

    def test_does_not_trim_high_confidence_repetition(self) -> None:
        transcript = Transcript(
            language="en",
            engine="mlx",
            words=(
                Word("Go", 0, 200, 0.9),
                Word(" go", 200, 400, 0.9),
                Word(" go", 400, 600, 0.9),
            ),
        )

        cues = segment_transcript(transcript)

        self.assertEqual(cues[0].source_text, "Go go go")

    def test_trims_pathological_high_confidence_zero_duration_repetition(self) -> None:
        repeated = tuple(
            Word(
                " podría",
                1300,
                1300 if index < 8 else 1500,
                0.85,
            )
            for index in range(9)
        )
        transcript = Transcript(
            language="en",
            engine="mlx",
            words=(
                Word("Done.", 0, 1000, 0.99),
                Word(" Just", 1200, 1240, 0.01),
                Word(" it", 1240, 1240, 0.8),
                Word(" the", 1240, 1300, 0.01),
                *repeated,
            ),
        )

        cues = segment_transcript(transcript)

        self.assertEqual([cue.source_text for cue in cues], ["Done."])
        self.assertIn(
            "trimmed trailing repeated ASR output with unreliable timestamps",
            cues[0].warnings,
        )

    def test_clamps_tail_padding_to_media_duration(self) -> None:
        transcript = Transcript(
            language="en",
            engine="mlx",
            words=(Word("Done.", 900, 1000, 0.9),),
        )

        cues = segment_transcript(transcript, media_duration_ms=1050)

        self.assertEqual(cues[0].end_ms, 1050)


if __name__ == "__main__":
    unittest.main()

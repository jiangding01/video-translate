import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from xvt.models import Cue, Transcript, Word
from xvt.qa import check_subtitles
from xvt.segment import (
    NO_LINE_END,
    NO_LINE_START,
    TRAILING_ZERO_DURATION_WARNING_PREFIX,
    SegmentationConfig,
    segment_transcript,
    segmentation_config_for_preset,
    split_cue_by_parts,
)
from xvt.transcribe import (
    TranscriptionError,
    transcribe_audio,
    transcribe_faster,
    transcribe_mlx,
    words_from_faster_segments,
    words_from_mlx_segments,
)


class TranscriptionTest(unittest.TestCase):
    def test_segmentation_presets_preserve_default_and_change_cue_density(self) -> None:
        transcript = Transcript(
            language="en",
            engine="mlx",
            words=tuple(
                Word(f" word{index}", index * 900, index * 900 + 300) for index in range(10)
            ),
        )

        conservative = segment_transcript(
            transcript, segmentation_config_for_preset("conservative")
        )
        balanced = segment_transcript(transcript, segmentation_config_for_preset("balanced"))
        compact = segment_transcript(transcript, segmentation_config_for_preset("compact"))

        self.assertEqual(segmentation_config_for_preset("balanced"), SegmentationConfig())
        self.assertLessEqual(len(conservative), len(balanced))
        self.assertLessEqual(len(balanced), len(compact))

    def test_rejects_unknown_segmentation_preset(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown segmentation preset"):
            segmentation_config_for_preset("unknown")

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

    def test_faster_whisper_emits_monotonic_progress(self) -> None:
        first = SimpleNamespace(
            words=[SimpleNamespace(word=" Hello", start=0.0, end=2.0, probability=0.9)],
            end=2.0,
        )
        second = SimpleNamespace(
            words=[SimpleNamespace(word=" world", start=2.0, end=5.0, probability=0.9)],
            end=5.0,
        )
        info = SimpleNamespace(language="en", language_probability=0.9, duration=5.0)
        model = SimpleNamespace(transcribe=lambda *_args, **_kwargs: (iter([first, second]), info))
        faster_whisper = SimpleNamespace(WhisperModel=lambda *_args, **_kwargs: model)
        events = []

        with patch("xvt.transcribe.importlib.import_module", return_value=faster_whisper):
            transcript = transcribe_faster(
                Path("audio.wav"), event_sink=events.append, total_ms=5000
            )

        progress = [event.data for event in events if event.type == "transcription_progress"]
        self.assertEqual(transcript.engine, "faster")
        self.assertEqual(progress[-1]["percent"], 100.0)
        self.assertEqual(progress[-1]["processed_ms"], 5000)
        self.assertTrue(
            all(left["percent"] <= right["percent"] for left, right in zip(progress, progress[1:]))
        )

    def test_mlx_does_not_emit_completion_when_words_are_missing(self) -> None:
        mlx_whisper = SimpleNamespace(transcribe=lambda *_args, **_kwargs: {"segments": []})
        events = []

        with patch("xvt.transcribe.importlib.import_module", return_value=mlx_whisper):
            with self.assertRaises(TranscriptionError):
                transcribe_mlx(Path("audio.wav"), event_sink=events.append, total_ms=5000)

        progress = [event.data for event in events if event.type == "transcription_progress"]
        self.assertEqual(len(progress), 1)
        self.assertIsNone(progress[0]["percent"])

    def test_engine_fallback_emits_a_visible_event(self) -> None:
        """后端缺失导致的引擎回退必须留痕，不得静默换引擎。"""

        events: list = []
        transcript = Transcript(language="en", engine="mlx", words=(Word(" Hi", 0, 500, 0.9),))

        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "audio.wav"
            audio.write_bytes(b"audio")
            with (
                patch(
                    "xvt.transcribe.transcribe_faster",
                    side_effect=TranscriptionError("faster-whisper is not installed"),
                ),
                patch("xvt.transcribe.transcribe_mlx", return_value=transcript),
            ):
                result = transcribe_audio(audio, engine="faster", event_sink=events.append)

        self.assertEqual(result.engine, "mlx")
        fallback = [event for event in events if event.type == "transcription_fallback"]
        self.assertEqual(len(fallback), 1)
        self.assertEqual(fallback[0].data["requested_engine"], "faster")
        self.assertEqual(fallback[0].data["effective_engine"], "mlx")
        self.assertIn("not installed", fallback[0].data["reason"])

    def test_engine_failure_other_than_missing_backend_is_not_a_fallback(self) -> None:
        events: list = []

        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "audio.wav"
            audio.write_bytes(b"audio")
            with (
                patch(
                    "xvt.transcribe.transcribe_faster",
                    side_effect=TranscriptionError("model weights are corrupt"),
                ),
                patch("xvt.transcribe.transcribe_mlx") as mlx,
            ):
                with self.assertRaisesRegex(TranscriptionError, "model weights are corrupt"):
                    transcribe_audio(audio, engine="faster", event_sink=events.append)

        mlx.assert_not_called()
        self.assertEqual([event for event in events if event.type == "transcription_fallback"], [])

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

    def _assert_monotonic(self, cues: list) -> None:
        for index, cue in enumerate(cues):
            self.assertGreater(cue.end_ms, cue.start_ms, f"cue {cue.id} has non-positive duration")
            if index:
                self.assertGreaterEqual(cue.start_ms, cues[index - 1].end_ms)

    def test_backwards_word_timestamps_do_not_produce_degenerate_cues(self) -> None:
        # 词时间戳严重回退（跨段），旧的中点归一会把两条 cue 都压成 end <= start。
        transcript = Transcript(
            language="en",
            engine="mlx",
            words=(Word("aaa. ", 2000, 3000, 0.9), Word("bbb. ", 0, 100, 0.9)),
        )

        cues = segment_transcript(transcript, SegmentationConfig())

        self._assert_monotonic(cues)
        self.assertEqual((cues[0].start_ms, cues[0].end_ms), (2000, 3000))
        self.assertEqual(cues[1].start_ms, 3000)
        self.assertIn(
            "shifted cue start past the previous cue to resolve a degenerate overlap",
            cues[1].warnings,
        )
        self.assertEqual(
            [issue for issue in check_subtitles(cues).issues if issue.severity == "error"], []
        )

    def test_media_duration_shorter_than_cue_keeps_positive_duration(self) -> None:
        # 探测时长短于 ASR 输出时，min(limits) 会把 end 压到 start 之前。
        transcript = Transcript(
            language="en",
            engine="mlx",
            words=(Word("hello. ", 0, 1000, 0.9), Word("tail. ", 9000, 9500, 0.9)),
        )

        cues = segment_transcript(transcript, SegmentationConfig(), media_duration_ms=8000)

        self._assert_monotonic(cues)
        self.assertEqual((cues[1].start_ms, cues[1].end_ms), (9000, 9001))
        self.assertIn("extended cue end to keep a positive duration", cues[1].warnings)

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

    def _tail_hallucination_transcript(self, *, include_zero_duration_tail: bool) -> Transcript:
        """The 2026-10-17 real sample: a zero-duration trailing word after real speech."""

        words = [
            Word("ご", 242020, 242280, 0.75830078125),
            Word("視", 242280, 243800, 0.978515625),
            Word("聴", 243800, 245140, 1.0),
        ]
        if include_zero_duration_tail:
            words.append(Word("ありがとうございました", 245140, 245140, 0.9990234375))
        return Transcript(language="ja", engine="mlx", words=tuple(words))

    def test_records_dropped_zero_duration_trailing_word(self) -> None:
        cues = segment_transcript(
            self._tail_hallucination_transcript(include_zero_duration_tail=True),
            media_duration_ms=245180,
        )
        control = segment_transcript(
            self._tail_hallucination_transcript(include_zero_duration_tail=False),
            media_duration_ms=245180,
        )

        # 约束 1：留痕不得改变 cue 数量、ID、时间轴或文本。
        self.assertEqual(
            [(cue.id, cue.start_ms, cue.end_ms, cue.source_text) for cue in cues],
            [(cue.id, cue.start_ms, cue.end_ms, cue.source_text) for cue in control],
        )
        self.assertEqual(control[-1].warnings, [])

        traces = [
            warning
            for warning in cues[-1].warnings
            if warning.startswith(TRAILING_ZERO_DURATION_WARNING_PREFIX)
        ]
        self.assertEqual(len(traces), 1, cues[-1].warnings)
        self.assertIn("245140", traces[0])
        self.assertIn("ありがとうございました", traces[0])
        self.assertIn("0.999", traces[0])

    def test_does_not_record_zero_duration_words_inside_the_transcript(self) -> None:
        transcript = Transcript(
            language="en",
            engine="mlx",
            words=(
                Word("Hello", 0, 400, 0.9),
                Word(" there", 400, 400, 0.99),
                Word(" world.", 400, 900, 0.9),
            ),
        )

        cues = segment_transcript(transcript)

        self.assertEqual([cue.source_text for cue in cues], ["Hello world."])
        self.assertEqual(
            [
                warning
                for cue in cues
                for warning in cue.warnings
                if warning.startswith(TRAILING_ZERO_DURATION_WARNING_PREFIX)
            ],
            [],
        )

    def test_dropped_tail_word_trace_does_not_change_subtitle_qa(self) -> None:
        cues = segment_transcript(
            self._tail_hallucination_transcript(include_zero_duration_tail=True),
            media_duration_ms=245180,
        )
        self.assertTrue(
            any(
                warning.startswith(TRAILING_ZERO_DURATION_WARNING_PREFIX)
                for warning in cues[-1].warnings
            )
        )

        traced = check_subtitles(cues)
        for cue in cues:
            cue.warnings = [
                warning
                for warning in cue.warnings
                if not warning.startswith(TRAILING_ZERO_DURATION_WARNING_PREFIX)
            ]
        untraced = check_subtitles(cues)

        self.assertEqual(traced.status, untraced.status)
        self.assertEqual(traced.issues, untraced.issues)
        self.assertEqual(traced.status, "pass")
        self.assertEqual(traced.issues, ())

    def test_imported_subtitle_cues_are_not_flagged_by_the_tail_trace(self) -> None:
        """用户导入字幕以口头致谢收尾是合法内容，留痕既不产生也不被 QA 读到。"""

        imported = [
            Cue(
                id=1,
                start_ms=0,
                end_ms=2000,
                source_text="ご視聴ありがとうございました",
                source_language="ja",
                provenance="user",
                timing_confidence=1.0,
            )
        ]
        baseline = check_subtitles(imported)

        imported[0].warnings.append(
            f"{TRAILING_ZERO_DURATION_WARNING_PREFIX}: 1 word at 0ms, "
            "max probability 0.999, text 'ありがとうございました'"
        )
        traced = check_subtitles(imported)

        self.assertEqual(baseline.status, "pass")
        self.assertEqual(traced.status, baseline.status)
        self.assertEqual(traced.issues, baseline.issues)

    def test_clamps_tail_padding_to_media_duration(self) -> None:
        transcript = Transcript(
            language="en",
            engine="mlx",
            words=(Word("Done.", 900, 1000, 0.9),),
        )

        cues = segment_transcript(transcript, media_duration_ms=1050)

        self.assertEqual(cues[0].end_ms, 1050)


class LineBreakProhibitionTest(unittest.TestCase):
    """禁則処理 (kinsoku shori) coverage for the width splitter.

    ``_partition_words`` cuts between ASR words, and mlx-whisper emits Japanese
    one character per "word", so in practice every character boundary is a
    candidate cut. Without a line-break rule the optimiser is free to open a
    line with a closing bracket, a small kana, an iteration mark or the
    prolonged sound mark. Every ``max_characters`` below is a setting that
    actually reproduced the violation before the rule existed.
    """

    @staticmethod
    def _per_character(text: str, language: str, *, step: int = 200) -> Transcript:
        return Transcript(
            language=language,
            engine="mlx",
            words=tuple(
                Word(character, index * step, index * step + step, 0.9)
                for index, character in enumerate(text)
            ),
        )

    def _assert_line_breaks_are_legal(self, cues: list[Cue], joined: str) -> None:
        texts = [cue.source_text for cue in cues]
        self.assertGreater(len(texts), 1)
        self.assertEqual("".join(texts), joined)
        self.assertTrue(all(texts))
        heads = [text.lstrip()[0] for text in texts if text.strip()]
        tails = [text.rstrip()[-1] for text in texts if text.strip()]
        self.assertEqual([head for head in heads if head in NO_LINE_START], [])
        self.assertEqual([tail for tail in tails if tail in NO_LINE_END], [])

    def _segment(self, text: str, language: str, max_characters: int) -> list[Cue]:
        return segment_transcript(
            self._per_character(text, language),
            SegmentationConfig(max_characters=max_characters),
        )

    def test_japanese_small_kana_never_starts_a_cue(self) -> None:
        # Previously split as "…なるはずだとき" | "っと思っているはずだ".
        text = "今日はもっと素敵な一日になるはずだときっと思っているはずだ"

        self._assert_line_breaks_are_legal(self._segment(text, "ja", 26), text)

    def test_prolonged_sound_mark_never_starts_a_cue(self) -> None:
        # Previously split as "ピリットとコーヒ" | "ーとデータベース".
        text = "ザヒューマンスピリットとコーヒーとデータベースとサーバールーム"

        self._assert_line_breaks_are_legal(self._segment(text, "ja", 20), text)

    def test_iteration_mark_never_starts_a_cue(self) -> None:
        # Previously split as "色々な国々の様々な人" | "々によって少々変わった".
        text = "人々の日々の暮らしは色々な国々の様々な人々によって少々変わった"

        self._assert_line_breaks_are_legal(self._segment(text, "ja", 24), text)

    def test_closing_bracket_never_starts_a_cue(self) -> None:
        # Previously split as "彼は人々に「ようこそ" | "」と言って静かに笑っ".
        text = "彼は人々に「ようこそ」と言って静かに笑ったそして帰っていった。"

        self._assert_line_breaks_are_legal(self._segment(text, "ja", 24), text)

    def test_opening_bracket_never_ends_a_cue(self) -> None:
        # Previously split as "た声で「" | "来月の予定" and "は未定です" | "」と静かに".
        text = "担当者が落ち着いた声で「来月の予定は未定です」と静かに答えました"

        self._assert_line_breaks_are_legal(self._segment(text, "ja", 10), text)

    def test_chinese_punctuation_never_starts_or_ends_a_cue(self) -> None:
        # Previously split as "这个方案（第二版" | "）已经通过了评审，".
        text = "这个方案（第二版）已经通过了评审，下周一就可以正式对外发布了。"

        self._assert_line_breaks_are_legal(self._segment(text, "zh-CN", 20), text)

    def test_real_katakana_run_no_longer_breaks_before_the_prolonged_sound_mark(self) -> None:
        """Replay of a real mlx-whisper slice; provenance is in the fixture's ``comment``."""

        fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "asr-words-ja-katakana-run.json").read_text(
                encoding="utf-8"
            )
        )
        transcript = Transcript(
            language=fixture["language"],
            engine=fixture["engine"],
            words=tuple(
                Word(entry["text"], entry["start_ms"], entry["end_ms"], entry["probability"])
                for entry in fixture["words"]
            ),
        )

        cues = segment_transcript(transcript, SegmentationConfig(max_characters=16))

        # Previously split as "松田アップリフティングザヒュ" | "ーマンスピリット".
        self._assert_line_breaks_are_legal(cues, "松田アップリフティングザヒューマンスピリット")
        self.assertEqual([cue.source_text for cue in cues if cue.source_text.startswith("ー")], [])

    def test_sentence_end_cut_defers_past_a_closing_bracket(self) -> None:
        """A cue boundary has three producers; the sentence-end one obeys the rule too.

        ``。`` + ``」`` is the single most ordinary kinsoku violation in Japanese:
        the main loop flushes on ``。`` and the next cue would open with ``」``.
        """

        text = "彼は静かにそうだ。」と答えてからゆっくり立ち上がって部屋を出た"
        cues = segment_transcript(
            self._per_character(text, "ja"),
            SegmentationConfig(max_duration_ms=10**9, pause_ms=10**9, max_characters=30),
        )

        self._assert_line_breaks_are_legal(cues, text)

    def test_duration_forced_cut_defers_past_prohibited_punctuation(self) -> None:
        """Previously split as "…一早就出发" | "，”然后转身离开了房"."""

        text = "他轻声说：“我们明天一早就出发，”然后转身离开了房间再也没有回头看一眼"
        cues = segment_transcript(
            self._per_character(text, "zh-CN", step=400),
            SegmentationConfig(max_duration_ms=4000, pause_ms=10**9, max_characters=30),
        )

        self._assert_line_breaks_are_legal(cues, text)

    def test_rule_holds_when_a_cue_splits_into_many_parts(self) -> None:
        """The penalty must outweigh width imbalance even across many chunks."""

        text = (
            "あいうえおかきくけこさしすせそたちつてと" * 2 + "もっと" + "なにぬねのはひふへほ" * 2
        )

        cues = segment_transcript(
            self._per_character(text, "ja"),
            SegmentationConfig(max_characters=6, max_duration_ms=10**9, pause_ms=10**9),
        )

        self.assertGreater(len(cues), 10)
        self._assert_line_breaks_are_legal(cues, text)

    def test_still_splits_when_every_position_is_prohibited(self) -> None:
        """The rule is a penalty, not a veto: a degenerate line must still split."""

        cues = self._segment("ー" * 20, "ja", 8)

        self.assertGreater(len(cues), 1)
        self.assertTrue(all(cue.source_text for cue in cues))
        self.assertEqual("".join(cue.source_text for cue in cues), "ー" * 20)

    def test_legal_break_points_are_still_used(self) -> None:
        """Reverse guard: the rule must not stop an ordinary, legal split."""

        text = "我们需要保留每个字幕的含义，但是也要保证每行都清晰。"
        cues = segment_transcript(
            self._per_character(text, "zh-CN", step=300),
            SegmentationConfig(max_characters=28),
        )

        self.assertEqual(
            [cue.source_text for cue in cues],
            ["我们需要保留每个字幕的含义，", "但是也要保证每行都清晰。"],
        )

    def test_split_cue_by_parts_also_respects_the_rule(self) -> None:
        text = "今日はもっと素敵な一日になるはずだときっと思っているはずだ"
        cue = self._segment(text, "ja", 100)[0]

        parts = split_cue_by_parts(cue, 3)

        self._assert_line_breaks_are_legal(parts, text)


if __name__ == "__main__":
    unittest.main()

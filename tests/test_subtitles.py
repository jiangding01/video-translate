import tempfile
import unittest
from pathlib import Path

import pysubs2

from xvt.models import Cue
from xvt.subtitles import load_subtitles, save_subtitles


class SubtitlesTest(unittest.TestCase):
    def test_loads_source_srt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.srt"
            path.write_text(
                "1\n00:00:01,000 --> 00:00:02,500\nHello world\n\n",
                encoding="utf-8",
            )

            cues = load_subtitles(path, source_language="en")

        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0].source_text, "Hello world")
        self.assertEqual((cues[0].start_ms, cues[0].end_ms), (1000, 2500))
        self.assertEqual(cues[0].provenance, "user_source_subtitle")

    def test_loads_translated_vtt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "translated.vtt"
            path.write_text(
                "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n你好\n",
                encoding="utf-8",
            )

            cues = load_subtitles(path, role="translated", target_language="zh-CN")

        self.assertEqual(cues[0].source_text, "")
        self.assertEqual(cues[0].target_text, "你好")
        self.assertEqual(cues[0].provenance, "user_translated_subtitle")

    def test_ass_tags_are_removed_on_import(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "styled.ass"
            subtitles = pysubs2.SSAFile()
            subtitles.events.append(
                pysubs2.SSAEvent(start=0, end=1000, text=r"{\i1}Hello{\i0}\Nworld")
            )
            subtitles.save(str(path))

            cues = load_subtitles(path)

        self.assertEqual(cues[0].source_text, "Hello\nworld")

    def test_saves_bilingual_srt_with_target_first(self) -> None:
        cue = Cue(
            id=1,
            start_ms=1000,
            end_ms=2500,
            source_text="Hello\nworld",
            target_text="你 好",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = save_subtitles([cue], Path(directory) / "bilingual.srt", mode="bilingual")
            saved = pysubs2.load(str(path))

        self.assertEqual(saved[0].plaintext.splitlines(), ["你 好", "Hello world"])

    def test_srt_export_keeps_literal_braces(self) -> None:
        cue = Cue(
            id=1,
            start_ms=1000,
            end_ms=2500,
            source_text="set {\\an8} here",
            target_text="变量 {x} 的值",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            translated = save_subtitles([cue], root / "translated.srt", mode="translated")
            bilingual = save_subtitles([cue], root / "bilingual.srt", mode="bilingual")
            # 导入端会剥除花括号，这里必须断言写出的原始文件文本。
            translated_text = translated.read_text(encoding="utf-8")
            bilingual_text = bilingual.read_text(encoding="utf-8")

        self.assertIn("变量 {x} 的值", translated_text)
        self.assertIn("变量 {x} 的值", bilingual_text)
        self.assertIn("set {\\an8} here", bilingual_text)
        # `\N` 仍然拆成真实换行，不留字面标签。
        self.assertNotIn("\\N", bilingual_text)
        self.assertIn("变量 {x} 的值\nset {\\an8} here", bilingual_text)

    def test_ass_export_keeps_literal_braces(self) -> None:
        cue = Cue(id=1, start_ms=0, end_ms=1000, source_text="变量 {x} 的值")
        with tempfile.TemporaryDirectory() as directory:
            path = save_subtitles([cue], Path(directory) / "source.ass")
            content = path.read_text(encoding="utf-8")

        self.assertIn("变量 {x} 的值", content)

    def test_translated_export_rejects_missing_translation(self) -> None:
        cue = Cue(id=1, start_ms=0, end_ms=1000, source_text="Hello")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "no translated text"):
                save_subtitles([cue], Path(directory) / "translated.srt", mode="translated")


if __name__ == "__main__":
    unittest.main()

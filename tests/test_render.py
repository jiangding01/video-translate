import tempfile
import unittest
from pathlib import Path

import pysubs2

from xvt.models import Cue
from xvt.qa import display_width_limit
from xvt.render import AssStyleOptions, font_for_language, save_bilingual_ass
from xvt.segment import SegmentationConfig


class RenderTest(unittest.TestCase):
    def test_selects_fonts_for_supported_scripts(self) -> None:
        self.assertEqual(font_for_language("zh-CN", "darwin"), "Hiragino Sans GB")
        self.assertEqual(font_for_language("ja", "darwin"), "Hiragino Sans")
        self.assertEqual(font_for_language("ar", "darwin"), "Geeza Pro")
        self.assertEqual(font_for_language("zh-CN", "linux"), "Noto Sans CJK SC")
        self.assertEqual(font_for_language("en", "linux"), "DejaVu Sans")
        self.assertEqual(font_for_language("zh-CN", "win32"), "Microsoft YaHei")

    def test_balanced_segmentation_defaults(self) -> None:
        self.assertEqual(display_width_limit("en"), 64)
        self.assertEqual(display_width_limit("zh-CN"), 36)
        self.assertEqual(SegmentationConfig().pause_ms, 650)

    def test_saves_bilingual_ass_with_explicit_styles_and_playres(self) -> None:
        cue = Cue(
            id=1,
            start_ms=1000,
            end_ms=2500,
            source_text="Hello\nworld",
            target_text="你 好",
            source_language="en",
            target_language="zh-CN",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = save_bilingual_ass(
                [cue],
                Path(directory) / "bilingual.ass",
                video_width=1920,
                video_height=1080,
                options=AssStyleOptions(target_size=24),
            )
            subtitles = pysubs2.load(str(path))

        self.assertEqual(subtitles.info["PlayResY"], "288")
        self.assertEqual(subtitles.info["PlayResX"], "512")
        self.assertEqual(subtitles.styles["Target"].fontsize, 24)
        self.assertEqual(subtitles.styles["Source"].fontsize, 14)
        self.assertEqual(subtitles.events[0].style, "Target")
        self.assertIn(r"\N{\rSource}", subtitles.events[0].text)
        self.assertNotIn("Hello\\Nworld", subtitles.events[0].text)

    def test_escapes_literal_braces_in_cue_text(self) -> None:
        cue = Cue(
            id=1,
            start_ms=0,
            end_ms=1000,
            source_text="set {\\an8} here",
            target_text="变量 {x} 的值",
            source_language="en",
            target_language="zh-CN",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = save_bilingual_ass(
                [cue],
                Path(directory) / "bilingual.ass",
                video_width=1280,
                video_height=720,
            )
            content = path.read_text(encoding="utf-8")

        dialogue = next(line for line in content.splitlines() if line.startswith("Dialogue:"))
        self.assertIn(r"变量 \{x\} 的值", dialogue)
        self.assertIn(r"set \{\an8\} here", dialogue)
        # 刻意拼接的换行与样式切换结构必须保持原样。
        self.assertIn(r"\N{\rSource}", dialogue)
        self.assertNotIn(r"\N\{\rSource\}", dialogue)

    def test_rejects_missing_translation(self) -> None:
        cue = Cue(id=1, start_ms=0, end_ms=1000, source_text="Hello")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "no translated text"):
                save_bilingual_ass(
                    [cue],
                    Path(directory) / "bilingual.ass",
                    video_width=1280,
                    video_height=720,
                )


if __name__ == "__main__":
    unittest.main()

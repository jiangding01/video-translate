import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pysubs2

import xvt.qa
import xvt.render
from xvt.models import Cue
from xvt.process import CommandResult
from xvt.qa import display_width_limit
from xvt.render import (
    AssStyleOptions,
    _styled_subtitles,
    default_target_size,
    font_for_language,
    measure_bilingual_line_widths,
    resolved_style_binding,
    save_bilingual_ass,
)
from xvt.segment import SegmentationConfig


class ResolvedStyleBindingTest(unittest.TestCase):
    """`resolved_style_binding` 是 qa 报告与 ASS 渲染**共用**的样式推导。

    E2（2026-11-01 验收发现清单）：`layout` 此前只记 `AssStyleOptions` 里用户的显式
    覆盖，实际字体由 `font_for_language` 按语言与平台推导却从未落盘，于是同一个 Job
    在 macOS 与 Linux 上 `style_options` 逐字节相同、真实字体却不同，烧录腿的复用判定
    把两种环境混同了。
    """

    @staticmethod
    def _cues() -> list[Cue]:
        return [
            Cue(
                1,
                0,
                1000,
                "Hello",
                target_text="你好",
                source_language="en",
                target_language="zh-CN",
            )
        ]

    def test_platform_changes_the_resolved_fonts(self) -> None:
        """E2 要防的就是这个：同一份输入，换平台就该换字体。"""

        darwin = resolved_style_binding(self._cues(), video_height=1080, platform_name="darwin")
        linux = resolved_style_binding(self._cues(), video_height=1080, platform_name="linux")
        self.assertEqual(darwin["target_font"], "Hiragino Sans GB")
        self.assertEqual(linux["target_font"], "Noto Sans CJK SC")
        self.assertNotEqual(darwin, linux)

    def test_explicit_options_win_over_platform_defaults(self) -> None:
        """用户显式指定时不走 font_for_language，两个平台结果相同。"""

        options = AssStyleOptions(target_font="My Sans", source_font="My Serif", target_size=30)
        darwin = resolved_style_binding(
            self._cues(), video_height=1080, options=options, platform_name="darwin"
        )
        linux = resolved_style_binding(
            self._cues(), video_height=1080, options=options, platform_name="linux"
        )
        self.assertEqual(darwin, linux)
        self.assertEqual(darwin["target_font"], "My Sans")
        self.assertEqual(darwin["source_font"], "My Serif")
        self.assertEqual(darwin["target_size"], 30)

    def test_missing_languages_degrade_deterministically(self) -> None:
        """cue 没有语言字段时取值仍确定（走 font_for_language 的兜底分支）。"""

        bare = [Cue(1, 0, 1000, "Hello", target_text="你好")]
        first = resolved_style_binding(bare, video_height=1080, platform_name="linux")
        second = resolved_style_binding(bare, video_height=1080, platform_name="linux")
        self.assertEqual(first, second)
        self.assertEqual(first["target_font"], "DejaVu Sans")

    def test_sizes_follow_the_derivation_not_just_the_frame(self) -> None:
        """字号记的是推导**输出**：画幅相同也要跟着 default_target_size 的结果走。"""

        binding = resolved_style_binding(self._cues(), video_height=1080, platform_name="linux")
        self.assertEqual(binding["target_size"], default_target_size(1080))
        self.assertEqual(binding["source_size"], max(8, round(binding["target_size"] / 1.7)))

    def test_styled_subtitles_really_uses_the_shared_binding(self) -> None:
        """行为级同源钉子：ASS 样式里的字体字号必须等于 binding 的输出。

        光靠「qa 与 render import 同一个函数」钉不住 `_styled_subtitles` 内部是否真的
        走了它——内部完全可以自己再推一遍。这条比对真实产出的 SSAFile，漂移即红。
        """

        cues = self._cues()
        binding = resolved_style_binding(cues, video_height=1080)
        subtitles = _styled_subtitles(cues, video_width=1920, video_height=1080)
        self.assertEqual(subtitles.styles["Target"].fontname, binding["target_font"])
        self.assertEqual(subtitles.styles["Source"].fontname, binding["source_font"])
        self.assertEqual(subtitles.styles["Target"].fontsize, binding["target_size"])
        self.assertEqual(subtitles.styles["Source"].fontsize, binding["source_size"])

    def test_qa_and_render_share_one_implementation(self) -> None:
        """对象身份钉子：qa 侧不得自己复制一份推导。"""

        self.assertIs(xvt.qa.resolved_style_binding, xvt.render.resolved_style_binding)


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

    def _over_wide_cue(self) -> Cue:
        return Cue(
            id=1,
            start_ms=0,
            end_ms=4000,
            source_text="A short {source}",
            target_text="第一部分的译文内容{x}比较长，第二部分的译文内容{y}同样很长",
            source_language="en",
            target_language="zh-CN",
        )

    def test_over_wide_target_is_wrapped_into_two_escaped_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = save_bilingual_ass(
                [self._over_wide_cue()],
                Path(directory) / "bilingual.ass",
                video_width=1920,
                video_height=1080,
            )
            content = path.read_text(encoding="utf-8")

        dialogue = next(line for line in content.splitlines() if line.startswith("Dialogue:"))
        # 译文行间一个 `\N`、译文与 `{\rSource}` 之间一个 `\N`，源文不换行。
        self.assertEqual(dialogue.count(r"\N"), 2)
        self.assertIn(
            r"第一部分的译文内容\{x\}比较长，\N第二部分的译文内容\{y\}同样很长\N{\rSource}",
            dialogue,
        )
        self.assertIn(r"A short \{source\}", dialogue)

    def test_target_within_limit_is_not_wrapped(self) -> None:
        cue = Cue(1, 0, 1000, "Hi", target_text="你好", target_language="zh-CN")
        with tempfile.TemporaryDirectory() as directory:
            path = save_bilingual_ass(
                [cue], Path(directory) / "bilingual.ass", video_width=1280, video_height=720
            )
            content = path.read_text(encoding="utf-8")

        dialogue = next(line for line in content.splitlines() if line.startswith("Dialogue:"))
        self.assertEqual(dialogue.count(r"\N"), 1)
        self.assertTrue(dialogue.endswith(r"你好\N{\rSource}Hi"))

    def test_measurement_uses_the_wrapped_target_text(self) -> None:
        captured: list[str] = []

        def run(_command, *, cwd, timeout):
            captured.append((Path(cwd) / "measure.ass").read_text(encoding="utf-8"))
            stderr = (
                "n:0 pts:0 x1:32 x2:331 y1:1 y2:40 w:300 h:40\n"
                "n:1 pts:1 x1:32 x2:131 y1:1 y2:20 w:100 h:20"
            )
            return CommandResult((), 0, "", stderr)

        with patch("xvt.render.run_command", side_effect=run):
            widths = measure_bilingual_line_widths(
                [self._over_wide_cue()], video_width=1920, video_height=1080
            )

        # 返回值形状不变：{(cue_id, role): (width, limit)}。
        self.assertEqual(set(widths), {(1, "target"), (1, "source")})
        self.assertEqual(widths[(1, "target")][0], 300)
        (content,) = captured
        events = [line for line in content.splitlines() if line.startswith("Dialogue:")]
        self.assertEqual(len(events), 2)
        self.assertIn(
            r"第一部分的译文内容\{x\}比较长，\N第二部分的译文内容\{y\}同样很长", events[0]
        )
        self.assertNotIn(r"\N", events[1])

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

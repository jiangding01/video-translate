import tempfile
import unittest
from pathlib import Path

import pysubs2

from xvt.models import Cue
from xvt.subtitles import load_subtitles, save_subtitles
from xvt.translate import map_subtitle_translations


def _write_srt(path: Path, bodies: list[str]) -> Path:
    """写出每条正文为 ``bodies[i]`` 的 SRT，第 i 条占用第 i 秒。"""

    blocks = [
        f"{index}\n00:00:{index:02d},000 --> 00:00:{index:02d},500\n{body}\n"
        for index, body in enumerate(bodies, start=1)
    ]
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


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

    def test_srt_import_keeps_literal_braces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _write_srt(
                Path(directory) / "source.srt",
                ["variable {x} value", "{0} items", "open { brace", "a {b {c} d} e"],
            )

            cues = load_subtitles(path)

        self.assertEqual(
            [cue.source_text for cue in cues],
            ["variable {x} value", "{0} items", "open { brace", "a {b {c} d} e"],
        )

    def test_srt_import_still_strips_override_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _write_srt(
                Path(directory) / "source.srt",
                [
                    r"{\an8}top line",
                    # libass 忽略 `{` 与 `\` 之间的空白，这个形态同样是覆盖块。
                    r"{ \an8}spaced tag",
                    r"{\i1}italic{\i0}",
                    # SRT reader 会把 HTML 标签转成 SSA 覆盖块，不能漏进 cue 文本。
                    "<i>html italic</i>",
                    r"{\an8}keep {y} here",
                ],
            )

            cues = load_subtitles(path)

        self.assertEqual(
            [cue.source_text for cue in cues],
            ["top line", "spaced tag", "italic", "html italic", "keep {y} here"],
        )

    def test_srt_import_converts_whitespace_and_break_tags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _write_srt(
                Path(directory) / "source.srt",
                [r"line1\Nline2", r"soft\nbreak", r"hard\hspace"],
            )

            cues = load_subtitles(path)

        self.assertEqual(
            [cue.source_text for cue in cues],
            ["line1\nline2", "soft\nbreak", "hard space"],
        )

    def test_vtt_import_keeps_literal_braces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.vtt"
            path.write_text(
                "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nvar {x}\n\n"
                "00:00:03.000 --> 00:00:04.000\n<i>html italic</i>\n",
                encoding="utf-8",
            )

            cues = load_subtitles(path)

        self.assertEqual([cue.source_text for cue in cues], ["var {x}", "html italic"])

    def test_brace_only_cue_is_no_longer_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _write_srt(Path(directory) / "source.srt", ["{x}", "plain", r"{\an8}"])

            cues = load_subtitles(path)

        # 只含字面花括号的 cue 保留（并重新连续编号），只含覆盖标签的 cue 仍被丢。
        self.assertEqual([cue.source_text for cue in cues], ["{x}", "plain"])
        self.assertEqual([cue.id for cue in cues], [1, 2])

    def test_ass_content_behind_srt_suffix_still_strips_tags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subtitles = pysubs2.SSAFile()
            subtitles.events.append(
                pysubs2.SSAEvent(start=0, end=1000, text=r"{\i1}Hello{\i0}\Nworld")
            )
            subtitles.events.append(pysubs2.SSAEvent(start=2000, end=3000, text="{note}"))
            subtitles.save(str(root / "styled.ass"))
            disguised = root / "styled.srt"
            disguised.write_text(
                (root / "styled.ass").read_text(encoding="utf-8"), encoding="utf-8"
            )

            cues = load_subtitles(disguised)

        # 判定按 pysubs2 解析出的实际格式，不按文件后缀。
        self.assertEqual([cue.source_text for cue in cues], ["Hello\nworld"])

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
            # 导入端仍会剥除 `{\an8}` 这类覆盖块，这里必须断言写出的原始文件文本。
            translated_text = translated.read_text(encoding="utf-8")
            bilingual_text = bilingual.read_text(encoding="utf-8")

        self.assertIn("变量 {x} 的值", translated_text)
        self.assertIn("变量 {x} 的值", bilingual_text)
        self.assertIn("set {\\an8} here", bilingual_text)
        # `\N` 仍然拆成真实换行，不留字面标签。
        self.assertNotIn("\\N", bilingual_text)
        self.assertIn("变量 {x} 的值\nset {\\an8} here", bilingual_text)

    def test_ass_export_is_rejected(self) -> None:
        cue = Cue(id=1, start_ms=0, end_ms=1000, source_text="变量 {x} 的值")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("source.ass", "source.ssa", "source.SSA"):
                with self.subTest(name=name):
                    with self.assertRaisesRegex(ValueError, "unsupported subtitle output format"):
                        save_subtitles([cue], root / name)
                    self.assertFalse((root / name).exists())

    def test_round_trip_through_srt_preserves_literal_braces(self) -> None:
        source_cues = [
            Cue(id=1, start_ms=1000, end_ms=2500, source_text="keep {x} intact"),
            Cue(id=2, start_ms=3000, end_ms=4000, source_text="{0}"),
            Cue(id=3, start_ms=5000, end_ms=6000, source_text="plain line"),
        ]
        translated_cues = [
            Cue(
                id=cue.id,
                start_ms=cue.start_ms,
                end_ms=cue.end_ms,
                source_text=cue.source_text,
                target_text=f"保留 {{{index}}} 占位",
            )
            for index, cue in enumerate(source_cues, start=1)
        ]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # 导出 → 用字幕工具编辑（此处原样保存）→ import-translation 导回。
            exported = save_subtitles(translated_cues, root / "translated.srt", mode="translated")
            reimported = load_subtitles(exported, role="translated", target_language="zh-CN")
            translations, report = map_subtitle_translations(source_cues, reimported)

        self.assertEqual(
            [cue.target_text for cue in reimported],
            ["保留 {1} 占位", "保留 {2} 占位", "保留 {3} 占位"],
        )
        self.assertEqual(len(reimported), len(source_cues))
        self.assertEqual(report["method"], "strict_order")
        self.assertEqual(translations, {1: "保留 {1} 占位", 2: "保留 {2} 占位", 3: "保留 {3} 占位"})

    def test_translated_export_rejects_missing_translation(self) -> None:
        cue = Cue(id=1, start_ms=0, end_ms=1000, source_text="Hello")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "no translated text"):
                save_subtitles([cue], Path(directory) / "translated.srt", mode="translated")


if __name__ == "__main__":
    unittest.main()

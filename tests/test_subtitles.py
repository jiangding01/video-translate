import codecs
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pysubs2
from pysubs2 import Pysubs2Error

from xvt.errors import ExitCode, UserInputError
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

    def test_bom_srt_import_drops_byte_order_mark(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bom.srt"
            path.write_text(
                "1\n00:00:01,000 --> 00:00:02,500\nHello world\n",
                encoding="utf-8-sig",
            )

            cues = load_subtitles(path, source_language="en")

        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0].source_text, "Hello world")
        self.assertNotIn("﻿", cues[0].source_text)
        self.assertEqual((cues[0].start_ms, cues[0].end_ms), (1000, 2500))

    def test_bom_vtt_import_parses_both_timestamp_forms(self) -> None:
        """带 BOM 的 VTT：BOM 残留会让格式嗅探漏判 WEBVTT 头。

        hh:mm:ss.mmm 会被误当成 SRT 解析（内容错位），mm:ss.mmm 直接抛
        FormatAutodetectionError（裸 traceback）。两种都必须正常解析成 VTT。
        """

        bodies = {
            "hh:mm:ss.mmm": "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n你好\n",
            "mm:ss.mmm": "WEBVTT\n\n00:01.000 --> 00:02.000\n你好\n",
        }
        for label, body in bodies.items():
            with self.subTest(timestamps=label), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "bom.vtt"
                path.write_text(body, encoding="utf-8-sig")

                cues = load_subtitles(path, role="translated", target_language="zh-CN")

                self.assertEqual(len(cues), 1)
                self.assertEqual(cues[0].target_text, "你好")
                self.assertEqual((cues[0].start_ms, cues[0].end_ms), (1000, 2000))

    def test_non_utf8_subtitle_is_an_actionable_user_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gbk.srt"
            path.write_bytes("1\n00:00:01,000 --> 00:00:02,500\n你好世界\n".encode("gbk"))

            with self.assertRaises(UserInputError) as captured:
                load_subtitles(path)

        message = str(captured.exception)
        self.assertIn("not UTF-8 encoded", message)
        self.assertIn("convert it to UTF-8", message)
        self.assertIn(str(path), message)
        self.assertEqual(captured.exception.category, "invalid_input")
        self.assertEqual(captured.exception.exit_code, ExitCode.USER_ERROR)
        # 消息只含路径与建议，不回显文件内容。
        self.assertNotIn("你好世界", message)

    def test_utf16_and_utf32_subtitles_name_the_suspected_encoding(self) -> None:
        body = "1\n00:00:01,000 --> 00:00:02,500\nHello\n"
        # 显式拼 BE 的 BOM：`str.encode("utf-16-be")` 不带 BOM，而 ASCII 正文的
        # 高位字节全是 \x00，那样的字节流反而是合法 UTF-8，测不到本分支。
        for encoding, payload, family in (
            ("utf-16 (LE BOM)", body.encode("utf-16"), "UTF-16"),
            ("utf-16-be", codecs.BOM_UTF16_BE + body.encode("utf-16-be"), "UTF-16"),
            ("utf-32 (LE BOM)", body.encode("utf-32"), "UTF-32"),
            ("utf-32-be", codecs.BOM_UTF32_BE + body.encode("utf-32-be"), "UTF-32"),
        ):
            with self.subTest(encoding=encoding), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "wide.srt"
                path.write_bytes(payload)

                with self.assertRaises(UserInputError) as captured:
                    load_subtitles(path)

                self.assertIn(f"it looks like {family}", str(captured.exception))

    def test_utf8_decode_failure_without_bom_omits_the_encoding_guess(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gbk.srt"
            path.write_bytes("1\n00:00:01,000 --> 00:00:02,500\n你好\n".encode("gbk"))

            with self.assertRaises(UserInputError) as captured:
                load_subtitles(path)

        self.assertNotIn("it looks like", str(captured.exception))

    def test_encoding_error_reports_the_display_path(self) -> None:
        """调用方先 copy2 成 Job 内部副本时，报错必须指向用户传入的原始路径。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "用户字幕.srt"
            original.write_bytes("1\n00:00:01,000 --> 00:00:02,500\n你好\n".encode("gbk"))
            internal = root / "input-subtitles.srt"
            internal.write_bytes(original.read_bytes())

            with self.assertRaises(UserInputError) as captured:
                load_subtitles(internal, display_path=original)

        message = str(captured.exception)
        self.assertIn(str(original), message)
        self.assertNotIn("input-subtitles.srt", message)

    def test_unparsable_subtitle_becomes_user_input_error(self) -> None:
        """Pysubs2Error 直接继承 Exception，不包装会穿透 CLI 顶层兜底变成裸 traceback。"""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prose.srt"
            path.write_text("this is not a subtitle at all\njust prose\n", encoding="utf-8")

            with self.assertRaises(UserInputError) as captured:
                load_subtitles(path)

        self.assertNotIsInstance(captured.exception, Pysubs2Error)
        message = str(captured.exception)
        self.assertIn("could not be parsed as srt", message)
        self.assertIn(str(path), message)
        self.assertEqual(captured.exception.exit_code, ExitCode.USER_ERROR)
        self.assertNotIn("just prose", message)

    def test_non_pysubs2_parse_errors_become_user_input_errors_with_the_path(self) -> None:
        """各格式 parser 抛的是裸 KeyError/ValueError/NotImplementedError，不是 Pysubs2Error。

        pysubs2 按内容而非后缀嗅探格式，所以 `.srt` 后缀同样可能落到 sami/ttml parser 上；
        只捕获 Pysubs2Error 时这些异常会穿透（裸 traceback）或被错分成 workflow 错误。
        """

        cases = (
            # sami parser 的 `int(dict(attrs)["start"] or 0)`：缺 start 属性 → 裸 KeyError。
            ("sami-in-srt", "hi.srt", "<SAMI><BODY><SYNC><P>hi</SYNC></BODY></SAMI>"),
            # ttml parser 的时间表达式不认识时抛 NotImplementedError（RuntimeError 子类，
            # 只兜 Pysubs2Error/ValueError 时会被错分成 workflow 错误、退出码 4）。
            (
                "ttml-in-srt",
                "hi.srt",
                '<tt xmlns="http://www.w3.org/ns/ttml"><body><div>'
                '<p begin="banana" end="orange">hi</p></div></body></tt>',
            ),
            # substation parser 解析颜色的 int(..., 16) → 裸 ValueError，消息不含路径。
            (
                "bad-ass-color",
                "hi.ass",
                "[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\n"
                "Format: Name, Fontname, Fontsize, PrimaryColour\n"
                "Style: Default,Arial,20,&HZZZZZZ&\n\n[Events]\n"
                "Format: Layer, Start, End, Style, Text\n"
                "Dialogue: 0,0:00:01.00,0:00:02.00,Default,hi\n",
            ),
        )
        for label, name, body in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / name
                path.write_text(body, encoding="utf-8")

                with self.assertRaises(UserInputError) as captured:
                    load_subtitles(path)

                message = str(captured.exception)
                self.assertIn("could not be parsed as", message)
                self.assertIn(str(path), message)
                self.assertEqual(captured.exception.category, "invalid_input")
                self.assertEqual(captured.exception.exit_code, ExitCode.USER_ERROR)

    def test_load_subtitles_lets_os_errors_through(self) -> None:
        """读文件失败是依赖/环境错误，不能被兜成 invalid_input。"""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hi.srt"
            _write_srt(path, ["Hello"])
            with (
                patch("pysubs2.load", side_effect=PermissionError("denied")),
                self.assertRaises(PermissionError),
            ):
                load_subtitles(path)

    def test_bom_less_utf16_parse_failure_names_the_suspected_encoding(self) -> None:
        """无 BOM 的 UTF-16 能被 utf-8 解码「成功」，只会在 parser 里炸掉。"""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wide.srt"
            # utf-16-be 的 ASCII 正文高位字节全是 \x00，是合法 UTF-8，走不到编码报错分支。
            path.write_bytes(
                "1\n00:00:01,000 --> 00:00:02,500\nHello\n".encode("utf-16-be"),
            )

            with self.assertRaises(UserInputError) as captured:
                load_subtitles(path)

        message = str(captured.exception)
        self.assertIn("could not be parsed as srt", message)
        self.assertIn("NUL bytes", message)
        self.assertIn("UTF-16/32 without a BOM", message)

    def test_plain_parse_failure_omits_the_nul_hint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prose.srt"
            path.write_text("this is not a subtitle at all\n", encoding="utf-8")

            with self.assertRaises(UserInputError) as captured:
                load_subtitles(path)

        self.assertNotIn("NUL bytes", str(captured.exception))

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

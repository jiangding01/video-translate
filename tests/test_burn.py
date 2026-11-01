import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from ffmpeg_support import FFMPEG_REQUIREMENT, ffmpeg_can_render_ass

from xvt.burn import (
    build_burn_command,
    burn_subtitles,
    escape_filter_path,
    recommended_video_bitrate,
)
from xvt.events import EventType
from xvt.process import CommandError, CommandResult


class BurnTest(unittest.TestCase):
    def test_builds_compatible_hardware_encoding_command(self) -> None:
        command = build_burn_command(
            Path("/tmp/input.mp4"),
            Path("/tmp/subtitle.ass"),
            Path("/tmp/output.mp4"),
        )

        self.assertIn("h264_videotoolbox", command)
        self.assertIn("aac", command)
        self.assertIn("+faststart", command)
        self.assertIn("0:a?", command)

    def test_selects_video_bitrate_by_resolution(self) -> None:
        self.assertEqual(recommended_video_bitrate(480), "2M")
        self.assertEqual(recommended_video_bitrate(720), "3M")
        self.assertEqual(recommended_video_bitrate(1080), "5M")
        self.assertEqual(recommended_video_bitrate(1440), "9M")
        self.assertEqual(recommended_video_bitrate(2160), "16M")

    def test_builds_precise_preview_clip_command(self) -> None:
        command = build_burn_command(
            Path("/tmp/input.mp4"),
            Path("/tmp/subtitle.ass"),
            Path("/tmp/output.mp4"),
            start_ms=12500,
            clip_duration_ms=15000,
        )

        self.assertEqual(command[2:4], ["-ss", "12.500"])
        self.assertEqual(command[command.index("-t") + 1], "15.000")
        self.assertLess(command.index("-ss"), command.index("-i"))

    def test_escapes_ffmpeg_filter_path(self) -> None:
        # 两层转义：先 filter 选项值层（`\ : '`），再 filtergraph 层（`\ ' , ; [ ]`）。
        self.assertEqual(escape_filter_path(Path("/tmp/a:b's.ass")), r"/tmp/a\\:b\\\'s.ass")
        self.assertEqual(escape_filter_path(Path("/tmp/plain.ass")), "/tmp/plain.ass")
        self.assertEqual(escape_filter_path(Path("/tmp/a,b;c[d].ass")), r"/tmp/a\,b\;c\[d\].ass")

    def test_subtitle_filter_is_not_quote_wrapped(self) -> None:
        command = build_burn_command(
            Path("/tmp/input.mp4"),
            Path("/tmp/Tom's Videos/subtitle.ass"),
            Path("/tmp/output.mp4"),
        )
        subtitle_filter = command[command.index("-vf") + 1]

        # 单引号包裹结构无法表达路径里的单引号，必须保持裸值形式。
        self.assertEqual(subtitle_filter, r"ass=filename=/tmp/Tom\\\'s Videos/subtitle.ass")

    def test_burn_invokes_runner_and_returns_output(self) -> None:
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            subtitles = root / "subtitle.ass"
            output = root / "result" / "output.mp4"
            media.write_bytes(b"video")
            subtitles.write_text("subtitle", encoding="utf-8")

            def runner(args):
                calls.append(args)
                return CommandResult(tuple(args), 0, "", "")

            result = burn_subtitles(media, subtitles, output, runner=runner)

        self.assertEqual(result, output)
        self.assertEqual(calls[0][-1], str(output))

    def test_falls_back_only_for_hardware_encoder_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            subtitles = root / "subtitle.ass"
            media.write_bytes(b"video")
            subtitles.write_text("subtitle", encoding="utf-8")
            calls = []

            def runner(args):
                calls.append(args)
                if len(calls) == 1:
                    result = CommandResult(
                        tuple(args),
                        187,
                        "",
                        "Cannot create compression session: -12908",
                    )
                    raise CommandError(result)
                return CommandResult(tuple(args), 0, "", "")

            burn_subtitles(media, subtitles, root / "output.mp4", runner=runner)

        self.assertIn("h264_videotoolbox", calls[0])
        self.assertIn("libx264", calls[1])

    # 2026-11-01 实测抓到的真实块：`ffmpeg -f lavfi -i testsrc=size=3840x2160 -preset placebo
    # -progress pipe:1`，首帧还没输出完的那 0.5 秒里 out_time / speed / bitrate 全是字面量
    # `N/A`。不手编格式，照抄实测输出。
    _NA_BLOCK = (
        "frame=0",
        "fps=0.00",
        "stream_0_0_q=0.0",
        "bitrate=N/A",
        "total_size=0",
        "out_time_us=N/A",
        "out_time_ms=N/A",
        "out_time=N/A",
        "dup_frames=0",
        "drop_frames=0",
        "speed=N/A",
        "progress=continue",
    )
    _VALID_BLOCK = (
        "frame=750",
        "fps=307.11",
        "bitrate=  83.3kbits/s",
        "out_time_us=990000",
        "out_time_ms=990000",
        "dup_frames=0",
        "speed=10.2x",
        "progress=continue",
    )

    def _burn_with_lines(self, lines, *, duration_ms=1000):
        events = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            subtitles = root / "subtitle.ass"
            media.write_bytes(b"video")
            subtitles.write_text("subtitle", encoding="utf-8")

            def runner(args, **kwargs):
                for line in lines:
                    kwargs["line_callback"]("stdout", line)
                return CommandResult(tuple(args), 0, "", "")

            burn_subtitles(
                media,
                subtitles,
                root / "output.mp4",
                duration_ms=duration_ms,
                event_sink=events.append,
                runner=runner,
            )
        return events

    def test_progress_does_not_fall_back_to_zero_on_an_unavailable_block(self) -> None:
        """ffmpeg 的 N/A 块不得被兜底成 0%——那正是「99% 之后回退到 0% N/A」的来源。

        把解析失败当成「已输出 0 毫秒」是在用最差的那个值兜底：进度条当场倒退，用户看着
        像失败重跑。拿不到就沿用本次编码上一个已知值，一次都没拿到过就什么都不报。
        """

        events = self._burn_with_lines([*self._NA_BLOCK, *self._VALID_BLOCK, *self._NA_BLOCK])

        first, second, third = (event.data for event in events[:3])
        # 开头那块一次都还没拿到过，报 None 而不是 0。
        self.assertIsNone(first["out_time_ms"])
        self.assertIsNone(first["percent"])
        self.assertIsNone(first["speed"], "N/A 不得作为展示值出门")
        # frame=0 是真值不是缺值，照原样出门；归一只针对字面量 N/A。
        self.assertEqual(first["frame"], "0")
        self.assertEqual(second["percent"], 99.0)
        self.assertEqual(second["speed"], "10.2x")
        # 中途再来一块 N/A：沿用 99%，绝不回退。
        self.assertEqual(third["out_time_ms"], 990)
        self.assertEqual(third["percent"], 99.0)

    def test_progress_state_does_not_leak_into_the_software_fallback(self) -> None:
        """硬件编码失败回退软件编码时，进度必须从本次编码重新算。

        两次 runner 调用共用一份状态的话，回退那次开头的 N/A 块会沿用到硬件那次的进度，
        于是显示「快完成了」而实际在从零重编——比原来显示 0% 更能骗人。
        """

        events = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            subtitles = root / "subtitle.ass"
            media.write_bytes(b"video")
            subtitles.write_text("subtitle", encoding="utf-8")
            calls = []

            def runner(args, **kwargs):
                calls.append(args)
                if len(calls) == 1:
                    for line in self._VALID_BLOCK:
                        kwargs["line_callback"]("stdout", line)
                    raise CommandError(
                        CommandResult(
                            tuple(args),
                            1,
                            "",
                            "Error while opening encoder for output stream",
                        )
                    )
                for line in self._NA_BLOCK:
                    kwargs["line_callback"]("stdout", line)
                return CommandResult(tuple(args), 0, "", "")

            burn_subtitles(
                media,
                subtitles,
                root / "output.mp4",
                duration_ms=1000,
                event_sink=events.append,
                runner=runner,
            )

        self.assertEqual(len(calls), 2, "本用例要走到软件回退那条腿")
        self.assertEqual(events[0].data["percent"], 99.0)
        self.assertIsNone(
            events[-1].data["percent"],
            "回退后第一块是 N/A，不得沿用上一次编码的 99%",
        )

    def test_burn_emits_structured_progress(self) -> None:
        events = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            subtitles = root / "subtitle.ass"
            media.write_bytes(b"video")
            subtitles.write_text("subtitle", encoding="utf-8")

            def runner(args, **kwargs):
                callback = kwargs["line_callback"]
                callback("stdout", "frame=12")
                callback("stdout", "out_time_us=1000000")
                callback("stdout", "speed=2.0x")
                callback("stdout", "progress=continue")
                return CommandResult(tuple(args), 0, "", "")

            burn_subtitles(
                media,
                subtitles,
                root / "output.mp4",
                duration_ms=2000,
                event_sink=events.append,
                runner=runner,
            )

        self.assertEqual(events[0].type, "burn_progress")
        self.assertEqual(events[0].data["percent"], 50.0)
        self.assertEqual(events[0].data["frame"], "12")

    def test_preview_can_use_a_distinct_progress_event(self) -> None:
        events = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            subtitles = root / "subtitle.ass"
            media.write_bytes(b"video")
            subtitles.write_text("subtitle", encoding="utf-8")

            def runner(args, **kwargs):
                callback = kwargs["line_callback"]
                callback("stdout", "out_time_us=1000000")
                callback("stdout", "progress=end")
                return CommandResult(tuple(args), 0, "", "")

            burn_subtitles(
                media,
                subtitles,
                root / "preview.mp4",
                duration_ms=1000,
                progress_event_type=EventType.PREVIEW_PROGRESS,
                event_sink=events.append,
                runner=runner,
            )

        self.assertEqual(events[0].type, "preview_progress")
        self.assertEqual(events[0].data["percent"], 100.0)

    def test_does_not_mask_non_encoder_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            subtitles = root / "subtitle.ass"
            media.write_bytes(b"video")
            subtitles.write_text("subtitle", encoding="utf-8")
            calls = []

            def runner(args):
                calls.append(args)
                result = CommandResult(tuple(args), 1, "", "Unable to parse subtitle file")
                raise CommandError(result)

            with self.assertRaises(CommandError):
                burn_subtitles(media, subtitles, root / "output.mp4", runner=runner)

        self.assertEqual(len(calls), 1)


_SMOKE_ASS = """[Script Info]
ScriptType: v4.00+
PlayResX: 320
PlayResY: 180

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, \
BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, \
BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,60,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,\
100,100,0,0,1,2,0,5,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,HELLO
"""


@unittest.skipUnless(ffmpeg_can_render_ass(), FFMPEG_REQUIREMENT)
class BurnFilterPathSmokeTest(unittest.TestCase):
    """用真实 ffmpeg 确认特殊字符路径既能烧录成功，字幕也真的渲染上去。"""

    def _create_video(self, path: Path) -> None:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x180:d=1:r=10",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(path),
            ],
            check=True,
            capture_output=True,
        )

    def _mean_luma(self, video: Path) -> float:
        # 输出路径本身含特殊字符，先复制到干净路径再用 movie= 探测，避免探测端也要转义。
        with tempfile.TemporaryDirectory() as probe_directory:
            probe = Path(probe_directory) / "probe.mp4"
            shutil.copyfile(video, probe)
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    f"movie={probe},signalstats",
                    "-show_entries",
                    "frame_tags=lavfi.signalstats.YAVG",
                    "-of",
                    "csv=p=0",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        values = [
            float(item.strip(",")) for item in result.stdout.split() if item.strip(",").strip()
        ]
        self.assertTrue(values, "ffprobe returned no luma samples")
        return sum(values) / len(values)

    def _burn_in_directory(self, root: Path, name: str) -> None:
        directory = root / name
        directory.mkdir(parents=True)
        subtitles = directory / "subtitle.ass"
        subtitles.write_text(_SMOKE_ASS, encoding="utf-8")
        media = directory / "input.mp4"
        self._create_video(media)
        output = directory / "output.mp4"

        burn_subtitles(media, subtitles, output, hardware_encoding=False)

        self.assertTrue(output.is_file())
        # 纯黑底片 YAVG 为 16.0；字幕渲染上去才会明显抬高平均亮度。
        self.assertGreater(self._mean_luma(output), 17.0, f"subtitles not rendered for {name!r}")

    def test_burns_with_special_characters_in_path(self) -> None:
        for name in ("plain", "Tom's Videos", "a,b comma", "hell'q,c;s[b]:x"):
            with self.subTest(directory=name), tempfile.TemporaryDirectory() as directory:
                self._burn_in_directory(Path(directory), name)


if __name__ == "__main__":
    unittest.main()

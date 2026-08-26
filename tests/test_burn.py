import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

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


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "FFmpeg integration tests require ffmpeg and ffprobe",
)
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

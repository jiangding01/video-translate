import tempfile
import unittest
from pathlib import Path

from xvt.burn import (
    build_burn_command,
    burn_subtitles,
    escape_filter_path,
    recommended_video_bitrate,
)
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
        escaped = escape_filter_path(Path("/tmp/a:b's.ass"))

        self.assertEqual(escaped, "/tmp/a\\:b\\'s.ass")

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
                progress_event_type="preview_progress",
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


if __name__ == "__main__":
    unittest.main()

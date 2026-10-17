import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pysubs2
from ffmpeg_support import FFMPEG_REQUIREMENT, ffmpeg_can_render_ass

from xvt.job_store import get_job_file, load_job
from xvt.media import probe_media
from xvt.models import Transcript, Word
from xvt.pipeline import (
    apply_subtitle_translation,
    burn_translated_job,
    preview_translated_job,
    run_local_transcription,
)


@unittest.skipUnless(ffmpeg_can_render_ass(), FFMPEG_REQUIREMENT)
class EndToEndTest(unittest.TestCase):
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
                "color=c=0x18202b:s=640x360:d=2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=2",
                "-shortest",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(path),
            ],
            capture_output=True,
            check=True,
        )

    def test_existing_subtitles_to_burned_bilingual_video(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            self._create_video(media)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,200 --> 00:00:01,500\nHello world.\n",
                encoding="utf-8",
            )
            translated = root / "translated.srt"
            translated.write_text(
                "1\n00:00:00,200 --> 00:00:01,500\n你好，世界。\n",
                encoding="utf-8",
            )

            job = run_local_transcription(media, root / "output", subtitles=source, language="en")
            apply_subtitle_translation(
                get_job_file(job),
                translated,
                target_language="zh-CN",
            )
            burned = burn_translated_job(get_job_file(job), hardware_encoding=False)
            info = probe_media(burned)

            self.assertTrue(burned.is_file())
            self.assertTrue(info.has_video)
            self.assertTrue(info.has_audio)
            self.assertEqual(info.video_codec, "h264")
            self.assertEqual(info.audio_codec, "aac")
            self.assertLessEqual(abs(info.duration_ms - 2000), 500)

    def test_real_audio_normalization_with_mocked_asr_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            self._create_video(media)
            transcript = Transcript(
                language="en",
                engine="test",
                words=(Word("Hello.", 200, 1200, 1.0),),
            )

            with patch("xvt.pipeline.transcription.transcribe_audio", return_value=transcript):
                job = run_local_transcription(media, root / "output")

            artifacts = {artifact.kind: Path(artifact.path) for artifact in job.artifacts}
            audio = probe_media(artifacts["normalized_audio"])
            self.assertTrue(audio.has_audio)
            self.assertFalse(audio.has_video)
            self.assertEqual(audio.audio_codec, "pcm_s16le")
            self.assertTrue(artifacts["source_srt"].is_file())

    def test_nonzero_start_bilingual_preview_has_expected_duration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            self._create_video(media)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,200 --> 00:00:01,500\nHello world.\n",
                encoding="utf-8",
            )
            translated = root / "translated.srt"
            translated.write_text(
                "1\n00:00:00,200 --> 00:00:01,500\n你好，世界。\n",
                encoding="utf-8",
            )

            job = run_local_transcription(media, root / "output", subtitles=source, language="en")
            job_file = get_job_file(job)
            apply_subtitle_translation(job_file, translated, target_language="zh-CN")
            preview = preview_translated_job(
                job_file,
                start_seconds=0.5,
                duration_seconds=1,
                hardware_encoding=False,
            )
            info = probe_media(preview)
            persisted = load_job(job_file)
            preview_ass = pysubs2.load(
                next(
                    artifact.path
                    for artifact in persisted.artifacts
                    if artifact.kind == "preview_ass"
                )
            )

            self.assertTrue(info.has_video)
            self.assertTrue(info.has_audio)
            self.assertLessEqual(abs(info.duration_ms - 1000), 250)
            self.assertEqual(
                {(event.start, event.end) for event in preview_ass.events},
                {(0, 1000)},
            )
            self.assertIn("preview_ass", {artifact.kind for artifact in persisted.artifacts})
            self.assertIn("preview_video", {artifact.kind for artifact in persisted.artifacts})


if __name__ == "__main__":
    unittest.main()

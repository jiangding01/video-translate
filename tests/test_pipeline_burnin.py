"""预览与烧录（pipeline.burnin）的流程测试。"""

import json
import os
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from xvt.job_store import create_job, load_job, save_job
from xvt.media import MediaInfo
from xvt.models import Artifact, Cue
from xvt.pipeline import burn_translated_job, preview_translated_job
from xvt.recovery import file_checksum


class BurnPreviewPipelineTest(unittest.TestCase):
    def test_burn_translated_job_generates_ass_and_validates_media(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            job = create_job("https://youtu.be/abc", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            translated_path = artifacts / "translated-cues.json"
            translated_path.write_text(
                json.dumps(
                    [
                        asdict(
                            Cue(
                                1,
                                0,
                                1000,
                                "Hello",
                                target_text="你好",
                                source_language="en",
                                target_language="zh-CN",
                            )
                        )
                    ]
                ),
                encoding="utf-8",
            )
            job.artifacts.append(Artifact("translated_cues", str(translated_path)))
            job.artifacts.append(Artifact("source_video", str(media), file_checksum(media)))
            save_job(job)
            input_info = MediaInfo(
                str(media), 2000, 5, "mp4", 1920, 1080, 30.0, "h264", "aac", True, True
            )
            output_info = MediaInfo(
                str(artifacts / "burned.mp4"),
                2000,
                10,
                "mp4",
                1920,
                1080,
                30.0,
                "h264",
                "aac",
                True,
                True,
            )

            burn_options = {}

            def burn(_media, _subtitles, output, **kwargs):
                burn_options.update(kwargs)
                Path(output).write_bytes(b"burned")
                return Path(output)

            with (
                patch("xvt.pipeline.burnin.probe_media", side_effect=[input_info, output_info]),
                patch("xvt.pipeline.burnin.burn_subtitles", side_effect=burn),
                patch("xvt.pipeline.burnin.download_remote_media") as download,
            ):
                result = burn_translated_job(root / "jobs" / job.id / "job.json")

            self.assertTrue(result.exists())
            persisted = json.loads((root / "jobs" / job.id / "job.json").read_text())
            self.assertEqual(persisted["stages"]["media_qa"]["status"], "succeeded")
            self.assertTrue((artifacts / "bilingual.ass").exists())
            self.assertEqual(burn_options["video_bitrate"], "5M")
            download.assert_not_called()

    def test_preview_translated_job_renders_selected_cue_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            burn_media = root / "burn-input.mp4"
            burn_media.write_bytes(b"burn video")
            job = create_job(str(media), root)
            job.config["workflow"] = {
                "burn_media": str(burn_media),
                "hardware_encoding": False,
                "target_font": "Target Font",
                "source_font": "Source Font",
            }
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            translated_path = artifacts / "translated-cues.json"
            translated_path.write_text(
                json.dumps(
                    [
                        asdict(
                            Cue(
                                1,
                                1000,
                                2000,
                                "First",
                                target_text="第一条",
                                source_language="en",
                                target_language="zh-CN",
                            )
                        ),
                        asdict(
                            Cue(
                                2,
                                5000,
                                7000,
                                "Second",
                                target_text="第二条",
                                source_language="en",
                                target_language="zh-CN",
                            )
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            qa_path = artifacts / "qa-translated.json"
            qa_path.write_text(json.dumps({"status": "pass"}), encoding="utf-8")
            burned_path = artifacts / "burned.mp4"
            burned_path.write_bytes(b"burned")
            job.artifacts.extend(
                [
                    Artifact(
                        "translated_cues", str(translated_path), file_checksum(translated_path)
                    ),
                    Artifact(
                        "translated_subtitle_qa",
                        str(qa_path),
                        file_checksum(qa_path),
                    ),
                    Artifact("burned_video", str(burned_path), file_checksum(burned_path)),
                ]
            )
            job_file = save_job(job)
            source_info = MediaInfo(
                str(media), 30000, 5, "mp4", 1920, 1080, 30.0, "h264", "aac", True, True
            )
            preview_info = MediaInfo(
                str(artifacts / "preview.mp4"),
                10000,
                10,
                "mp4",
                1920,
                1080,
                30.0,
                "h264",
                "aac",
                True,
                True,
            )
            burn_options = {}
            burned_media = None

            def burn(_media, _subtitles, output, **kwargs):
                nonlocal burned_media
                burned_media = Path(_media)
                burn_options.update(kwargs)
                Path(output).write_bytes(b"preview")
                return Path(output)

            with (
                patch("xvt.pipeline.burnin.probe_media", side_effect=[source_info, preview_info]),
                patch("xvt.pipeline.burnin.burn_subtitles", side_effect=burn),
            ):
                result = preview_translated_job(
                    job_file,
                    cue_id=2,
                    duration_seconds=10,
                )

            persisted = load_job(job_file)
            preview_ass_text = (artifacts / "preview.ass").read_text(encoding="utf-8")
            with (
                patch("xvt.pipeline.burnin.probe_media", return_value=source_info),
                self.assertRaisesRegex(ValueError, "existing artifact"),
            ):
                preview_translated_job(job_file, output=burned_path)
            media_alias = root / "media-alias.mp4"
            os.link(burn_media, media_alias)
            with (
                patch("xvt.pipeline.burnin.probe_media", return_value=source_info),
                self.assertRaisesRegex(ValueError, "source media"),
            ):
                preview_translated_job(job_file, output=media_alias)

        self.assertEqual(result.name, "preview.mp4")
        self.assertEqual(burned_media, burn_media.resolve())
        self.assertEqual(burn_options["start_ms"], 3000)
        self.assertEqual(burn_options["clip_duration_ms"], 10000)
        self.assertEqual(burn_options["duration_ms"], 10000)
        self.assertEqual(burn_options["progress_event_type"], "preview_progress")
        self.assertFalse(burn_options["hardware_encoding"])
        self.assertIn("Target Font", preview_ass_text)
        self.assertIn("Source Font", preview_ass_text)
        self.assertEqual(
            {artifact.kind for artifact in persisted.artifacts} & {"preview_ass", "preview_video"},
            {"preview_ass", "preview_video"},
        )

    def test_preview_rejects_failed_translated_subtitle_qa(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            translated_path = artifacts / "translated-cues.json"
            translated_path.write_text(
                json.dumps([asdict(Cue(1, 0, 1000, "Hello", "你好"))]),
                encoding="utf-8",
            )
            qa_path = artifacts / "qa-translated.json"
            qa_path.write_text(json.dumps({"status": "failed"}), encoding="utf-8")
            job.artifacts.extend(
                [
                    Artifact(
                        "translated_cues", str(translated_path), file_checksum(translated_path)
                    ),
                    Artifact(
                        "translated_subtitle_qa",
                        str(qa_path),
                        file_checksum(qa_path),
                    ),
                ]
            )
            job_file = save_job(job)

            with self.assertRaisesRegex(RuntimeError, "QA must pass"):
                preview_translated_job(job_file)

    def test_preview_rejects_non_finite_time_values_before_loading_job(self) -> None:
        with self.assertRaisesRegex(ValueError, "--start"):
            preview_translated_job("/tmp/missing-job.json", start_seconds=float("nan"))
        with self.assertRaisesRegex(ValueError, "--duration"):
            preview_translated_job("/tmp/missing-job.json", duration_seconds=float("inf"))

    def test_burn_media_qa_rejects_missing_audio_and_changed_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            job = create_job(str(media), root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            translated_path = artifacts / "translated-cues.json"
            translated_path.write_text(
                json.dumps(
                    [
                        asdict(
                            Cue(
                                1,
                                0,
                                1000,
                                "Hello",
                                target_text="你好",
                                source_language="en",
                                target_language="zh-CN",
                            )
                        )
                    ]
                ),
                encoding="utf-8",
            )
            job.artifacts.append(Artifact("translated_cues", str(translated_path)))
            save_job(job)
            input_info = MediaInfo(
                str(media), 2000, 5, "mp4", 1920, 1080, 30.0, "h264", "aac", True, True
            )
            output_info = MediaInfo(
                str(artifacts / "burned.mp4"),
                2000,
                10,
                "mp4",
                1280,
                720,
                30.0,
                "h264",
                None,
                True,
                False,
            )

            def burn(_media, _subtitles, output, **_kwargs):
                Path(output).write_bytes(b"burned")
                return Path(output)

            with (
                patch("xvt.pipeline.burnin.probe_media", side_effect=[input_info, output_info]),
                patch("xvt.pipeline.burnin.burn_subtitles", side_effect=burn),
                self.assertRaisesRegex(RuntimeError, "failed media QA"),
            ):
                burn_translated_job(root / "jobs" / job.id / "job.json")

            report = json.loads((artifacts / "media-qa.json").read_text(encoding="utf-8"))

        self.assertEqual(report["status"], "failed")
        self.assertIn("output has no audio stream", report["issues"])
        self.assertIn("output dimensions differ from source video", report["issues"])


if __name__ == "__main__":
    unittest.main()

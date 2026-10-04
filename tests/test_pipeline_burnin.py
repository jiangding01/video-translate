"""预览与烧录（pipeline.burnin）的流程测试。"""

import json
import os
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from xvt.errors import UserInputError
from xvt.job_store import create_job, load_job, save_job
from xvt.media import MediaInfo
from xvt.models import Artifact, Cue, StageStatus
from xvt.pipeline import burn_translated_job, preview_translated_job
from xvt.recovery import file_checksum

_QA_KIND = "translated_subtitle_qa"


def _register_translated_qa(
    job,
    artifacts: Path,
    status: str = "pass",
    issues: list[dict] | None = None,
) -> Path:
    """为烧录/预览 fixture 登记一份 checksum 有效的译文 QA 报告。"""
    qa_path = artifacts / "qa-translated.json"
    qa_path.write_text(
        json.dumps({"status": status, "cue_count": 1, "issues": issues or []}), encoding="utf-8"
    )
    job.artifacts = [artifact for artifact in job.artifacts if artifact.kind != _QA_KIND]
    job.artifacts.append(Artifact(_QA_KIND, str(qa_path), file_checksum(qa_path)))
    return qa_path


def _acknowledge_review_issues(job, artifacts: Path, issues: list[list]) -> None:
    """写入一条对当前 source_cues 有效的人工确认记录（不改写 QA 报告 status）。"""
    source_cues = artifacts / "source-cues.json"
    source_cues.write_text(json.dumps([asdict(Cue(1, 0, 1000, "Hello"))]), encoding="utf-8")
    checksum = file_checksum(source_cues)
    job.artifacts.append(Artifact("source_cues", str(source_cues), checksum))
    job.config["subtitle_review_acknowledgment"] = {
        "report_kind": _QA_KIND,
        "report_checksum": None,
        "source_cues_checksum": checksum,
        "acknowledged_issues": issues,
        "acknowledged_at": "2026-01-01T00:00:00+00:00",
    }


class BurnPreviewPipelineTest(unittest.TestCase):
    def test_burn_translated_job_generates_ass_and_validates_media(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            job = create_job("https://youtu.be/abc", root)
            job.config["workflow"] = {"target_language": "zh-CN", "burn": True}
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
            job.artifacts.append(
                Artifact("translated_cues", str(translated_path), file_checksum(translated_path))
            )
            job.artifacts.append(Artifact("source_video", str(media), file_checksum(media)))
            _register_translated_qa(job, artifacts)
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
            self.assertFalse(
                any(artifact["kind"] == "burn_fingerprint" for artifact in persisted["artifacts"])
            )
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
                self.assertRaisesRegex(UserInputError, "existing artifact"),
            ):
                preview_translated_job(job_file, output=burned_path)
            media_alias = root / "media-alias.mp4"
            os.link(burn_media, media_alias)
            with (
                patch("xvt.pipeline.burnin.probe_media", return_value=source_info),
                self.assertRaisesRegex(UserInputError, "source media"),
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

    def _run_burn(self, job_file: Path, artifacts: Path, media: Path):
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

        def burn(_media, _subtitles, output, **_kwargs):
            Path(output).write_bytes(b"burned")
            return Path(output)

        with (
            patch("xvt.pipeline.burnin.probe_media", side_effect=[input_info, output_info]),
            patch("xvt.pipeline.burnin.burn_subtitles", side_effect=burn),
        ):
            return burn_translated_job(job_file)

    def test_burn_rejects_failed_translated_subtitle_qa(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            job, artifacts = self._burn_ready_job(root, media)
            _register_translated_qa(job, artifacts, status="failed")
            job_file = save_job(job)

            with (
                patch("xvt.pipeline.burnin.burn_subtitles") as burn,
                self.assertRaisesRegex(RuntimeError, "QA must pass before burn-in"),
            ):
                burn_translated_job(job_file)

            burn.assert_not_called()

    def test_atomic_burn_qa_gate_guidance_avoids_continue(self) -> None:
        # 原子 Job 没有持久化 workflow 目标，`xvt continue` 必然报错，
        # QA 门文案不得把用户导向这条死路。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            job, artifacts = self._burn_ready_job(root, media)
            self.assertNotIn("workflow", job.config)
            _register_translated_qa(job, artifacts, status="failed")
            job_file = save_job(job)

            with self.assertRaises(RuntimeError) as raised:
                burn_translated_job(job_file)

            message = str(raised.exception)
            # 不得推荐 `xvt continue JOB`（原子 Job 上必然报错），只能点明它不适用。
            self.assertNotIn("xvt continue JOB", message)
            self.assertIn("`xvt continue` does not apply", message)
            self.assertIn("xvt translate JOB --target-language LANG", message)
            self.assertIn("no persisted workflow goal", message)

    def test_workflow_burn_qa_gate_guidance_points_to_continue(self) -> None:
        # 端到端 Job 维持既有文案：`continue` 与 acknowledge 通道都可用。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            job, artifacts = self._burn_ready_job(root, media)
            job.config["workflow"] = {"target_language": "zh-CN", "burn": True}
            _register_translated_qa(job, artifacts, status="failed")
            job_file = save_job(job)

            with self.assertRaises(RuntimeError) as raised:
                burn_translated_job(job_file)

            message = str(raised.exception)
            self.assertIn("xvt continue JOB --acknowledge-subtitle-review", message)
            self.assertNotIn("xvt translate JOB", message)

    def test_atomic_preview_qa_gate_guidance_avoids_continue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            job_file = save_job(job)

            with self.assertRaises(RuntimeError) as raised:
                preview_translated_job(job_file)

            message = str(raised.exception)
            self.assertIn("requires checksum-valid", message)
            self.assertNotIn("xvt continue JOB", message)
            self.assertIn("xvt translate JOB --target-language LANG", message)

    def test_atomic_missing_qa_report_offers_the_zero_cost_rebuild(self) -> None:
        """报告缺失但译文字幕仍有效时，门必须给出零 API 成本的重建出口。

        门、`status` 与 CLI 三处共用同一条命令；此前门无条件说「重跑 xvt translate」，
        而重跑翻译是真金白银的全量重译。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            job, artifacts = self._burn_ready_job(root, media)
            job.config["target_language"] = "zh-CN"
            translated_srt = artifacts / "translated.srt"
            translated_srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n你好\n", encoding="utf-8")
            job.artifacts.append(
                Artifact("translated_srt", str(translated_srt), file_checksum(translated_srt))
            )
            job.artifacts = [artifact for artifact in job.artifacts if artifact.kind != _QA_KIND]
            job_file = save_job(job)

            with self.assertRaises(RuntimeError) as raised:
                burn_translated_job(job_file)

            message = str(raised.exception)
            self.assertIn("xvt import-translation", message)
            self.assertIn("translated.srt", message)
            self.assertIn("--target-language zh-CN", message)
            self.assertIn("zero", message)
            self.assertIn("imported translation", message)
            self.assertNotIn("xvt continue JOB", message)

    def test_atomic_failing_qa_report_keeps_the_translate_guidance(self) -> None:
        """报告存在且判定不放行时不能给重建出口：重建只会算出同一份阻断报告。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            job, artifacts = self._burn_ready_job(root, media)
            job.config["target_language"] = "zh-CN"
            translated_srt = artifacts / "translated.srt"
            translated_srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n你好\n", encoding="utf-8")
            job.artifacts.append(
                Artifact("translated_srt", str(translated_srt), file_checksum(translated_srt))
            )
            _register_translated_qa(job, artifacts, status="failed")
            job_file = save_job(job)

            with self.assertRaises(RuntimeError) as raised:
                burn_translated_job(job_file)

            message = str(raised.exception)
            self.assertNotIn("import-translation", message)
            self.assertIn("xvt translate JOB --target-language LANG", message)

    def test_burn_rejects_missing_translated_subtitle_qa(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            job, _artifacts = self._burn_ready_job(root, media)
            job.artifacts = [artifact for artifact in job.artifacts if artifact.kind != _QA_KIND]
            job_file = save_job(job)

            with (
                patch("xvt.pipeline.burnin.burn_subtitles") as burn,
                self.assertRaisesRegex(RuntimeError, "requires checksum-valid"),
            ):
                burn_translated_job(job_file)

            burn.assert_not_called()

    def test_burn_rejects_tampered_translated_cues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            job, artifacts = self._burn_ready_job(root, media)
            job_file = save_job(job)
            # 篡改译文文件而不更新 checksum：原子 burn 必须拒绝而不是照样烧录。
            (artifacts / "translated-cues.json").write_text(
                json.dumps([asdict(Cue(1, 0, 1000, "Hello", target_text="被篡改"))]),
                encoding="utf-8",
            )

            with (
                patch("xvt.pipeline.burnin.burn_subtitles") as burn,
                self.assertRaisesRegex(RuntimeError, "requires checksum-valid"),
            ):
                burn_translated_job(job_file)

            burn.assert_not_called()

    def test_burn_accepts_warning_translated_subtitle_qa(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            job, artifacts = self._burn_ready_job(root, media)
            _register_translated_qa(job, artifacts, status="warning")
            job_file = save_job(job)

            result = self._run_burn(job_file, artifacts, media)

            self.assertTrue(result.exists())

    def test_burn_accepts_acknowledged_needs_review_qa(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            job, artifacts = self._burn_ready_job(root, media)
            _register_translated_qa(
                job,
                artifacts,
                status="needs_review",
                issues=[
                    {
                        "code": "bilingual_overflow_risk",
                        "severity": "review",
                        "cue_id": 1,
                        "message": "line may overflow",
                    }
                ],
            )
            # 用户此前确认的是源字幕 QA 的 line_too_wide：同族问题只需确认一次。
            _acknowledge_review_issues(job, artifacts, [["line_too_wide", 1]])
            job_file = save_job(job)

            result = self._run_burn(job_file, artifacts, media)

            self.assertTrue(result.exists())
            qa_report = json.loads((artifacts / "qa-translated.json").read_text(encoding="utf-8"))
            self.assertEqual(qa_report["status"], "pass")

    def test_burn_rejects_needs_review_qa_without_acknowledgment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            job, artifacts = self._burn_ready_job(root, media)
            _register_translated_qa(
                job,
                artifacts,
                status="needs_review",
                issues=[
                    {
                        "code": "bilingual_overflow_risk",
                        "severity": "review",
                        "cue_id": 1,
                        "message": "line may overflow",
                    }
                ],
            )
            job_file = save_job(job)

            with (
                patch("xvt.pipeline.burnin.burn_subtitles") as burn,
                self.assertRaisesRegex(RuntimeError, "QA must pass before burn-in"),
            ):
                burn_translated_job(job_file)

            burn.assert_not_called()

    def test_burn_qa_gate_runs_before_remote_media_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("https://youtu.be/abc", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            translated_path = artifacts / "translated-cues.json"
            translated_path.write_text(
                json.dumps([asdict(Cue(1, 0, 1000, "Hello", target_text="你好"))]),
                encoding="utf-8",
            )
            job.artifacts.append(
                Artifact("translated_cues", str(translated_path), file_checksum(translated_path))
            )
            _register_translated_qa(job, artifacts, status="failed")
            job_file = save_job(job)

            with (
                patch("xvt.pipeline.burnin.download_remote_media") as download,
                patch("xvt.pipeline.burnin.probe_media") as probe,
                self.assertRaisesRegex(RuntimeError, "QA must pass before burn-in"),
            ):
                burn_translated_job(job_file)

            download.assert_not_called()
            probe.assert_not_called()

    def test_preview_accepts_acknowledged_needs_review_qa(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            job, artifacts = self._burn_ready_job(root, media)
            _register_translated_qa(
                job,
                artifacts,
                status="needs_review",
                issues=[
                    {
                        "code": "line_too_wide",
                        "severity": "review",
                        "cue_id": 1,
                        "message": "line may overflow",
                    }
                ],
            )
            _acknowledge_review_issues(job, artifacts, [["line_too_wide", 1]])
            job_file = save_job(job)
            source_info = MediaInfo(
                str(media), 5000, 5, "mp4", 1920, 1080, 30.0, "h264", "aac", True, True
            )

            def burn(_media, _subtitles, output, **_kwargs):
                Path(output).write_bytes(b"preview")
                return Path(output)

            with (
                patch("xvt.pipeline.burnin.probe_media", return_value=source_info),
                patch("xvt.pipeline.burnin.burn_subtitles", side_effect=burn),
            ):
                preview = preview_translated_job(job_file)

            self.assertTrue(preview.exists())

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
            job.artifacts.append(
                Artifact("translated_cues", str(translated_path), file_checksum(translated_path))
            )
            _register_translated_qa(job, artifacts)
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

    def _burn_ready_job(self, root: Path, media: Path):
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
        job.artifacts.append(
            Artifact("translated_cues", str(translated_path), file_checksum(translated_path))
        )
        _register_translated_qa(job, artifacts)
        save_job(job)
        return job, artifacts

    def test_burn_rejects_output_overwriting_source_media(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            job, _artifacts = self._burn_ready_job(root, media)
            input_info = MediaInfo(
                str(media), 2000, 5, "mp4", 1920, 1080, 30.0, "h264", "aac", True, True
            )
            with (
                patch("xvt.pipeline.burnin.probe_media", return_value=input_info),
                patch("xvt.pipeline.burnin.burn_subtitles") as burn,
                self.assertRaisesRegex(UserInputError, "source media"),
            ):
                burn_translated_job(root / "jobs" / job.id / "job.json", output=media)
            burn.assert_not_called()
            persisted = load_job(root / "jobs" / job.id / "job.json")
            self.assertEqual(persisted.stages["render"].status, StageStatus.PENDING)
            self.assertEqual(persisted.stages["burn"].status, StageStatus.PENDING)

    def test_burn_rejects_hardlink_output_to_source_media(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            job, _artifacts = self._burn_ready_job(root, media)
            media_alias = root / "media-alias.mp4"
            os.link(media, media_alias)
            input_info = MediaInfo(
                str(media), 2000, 5, "mp4", 1920, 1080, 30.0, "h264", "aac", True, True
            )
            with (
                patch("xvt.pipeline.burnin.probe_media", return_value=input_info),
                patch("xvt.pipeline.burnin.burn_subtitles") as burn,
                self.assertRaisesRegex(UserInputError, "source media"),
            ):
                burn_translated_job(root / "jobs" / job.id / "job.json", output=media_alias)
            burn.assert_not_called()

    def test_burn_rejects_output_overwriting_job_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            job, _artifacts = self._burn_ready_job(root, media)
            job_file = root / "jobs" / job.id / "job.json"
            input_info = MediaInfo(
                str(media), 2000, 5, "mp4", 1920, 1080, 30.0, "h264", "aac", True, True
            )
            with (
                patch("xvt.pipeline.burnin.probe_media", return_value=input_info),
                patch("xvt.pipeline.burnin.burn_subtitles") as burn,
                self.assertRaisesRegex(UserInputError, "job file"),
            ):
                burn_translated_job(job_file, output=job_file)
            burn.assert_not_called()

    def test_burn_rejects_output_overwriting_existing_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            job, artifacts = self._burn_ready_job(root, media)
            source_srt = artifacts / "source.srt"
            source_srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
            job.artifacts.append(Artifact("source_srt", str(source_srt), file_checksum(source_srt)))
            save_job(job)
            input_info = MediaInfo(
                str(media), 2000, 5, "mp4", 1920, 1080, 30.0, "h264", "aac", True, True
            )
            with (
                patch("xvt.pipeline.burnin.probe_media", return_value=input_info),
                patch("xvt.pipeline.burnin.burn_subtitles") as burn,
                self.assertRaisesRegex(UserInputError, "existing artifact"),
            ):
                burn_translated_job(root / "jobs" / job.id / "job.json", output=source_srt)
            burn.assert_not_called()

    def test_burn_allows_rewriting_same_burned_video_kind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            job, artifacts = self._burn_ready_job(root, media)
            burned = artifacts / "burned.mp4"
            burned.write_bytes(b"old")
            job.artifacts.append(Artifact("burned_video", str(burned), file_checksum(burned)))
            save_job(job)
            input_info = MediaInfo(
                str(media), 2000, 5, "mp4", 1920, 1080, 30.0, "h264", "aac", True, True
            )
            output_info = MediaInfo(
                str(burned), 2000, 10, "mp4", 1920, 1080, 30.0, "h264", "aac", True, True
            )

            def burn(_media, _subtitles, output, **_kwargs):
                Path(output).write_bytes(b"new")
                return Path(output)

            with (
                patch("xvt.pipeline.burnin.probe_media", side_effect=[input_info, output_info]),
                patch("xvt.pipeline.burnin.burn_subtitles", side_effect=burn),
            ):
                result = burn_translated_job(root / "jobs" / job.id / "job.json", output=burned)

            self.assertEqual(result.read_bytes(), b"new")
            persisted = load_job(root / "jobs" / job.id / "job.json")
            self.assertEqual(persisted.stages["media_qa"].status, StageStatus.SUCCEEDED)


if __name__ == "__main__":
    unittest.main()

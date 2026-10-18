"""Tests for rebuilding an existing Job's source timeline with ASR."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from xvt.job_store import create_job, get_job_file, load_job, save_job
from xvt.media import MediaInfo
from xvt.models import Artifact, StageName, StageStatus, Transcript, Word
from xvt.pipeline import rebuild_source
from xvt.recovery import file_checksum
from xvt.segment import segment_transcript, segmentation_config_for_preset
from xvt.workflow import continue_workflow


class RebuildSourceTest(unittest.TestCase):
    def _media_info(self, media: Path) -> MediaInfo:
        return MediaInfo(
            str(media),
            5_000,
            media.stat().st_size,
            "mp4",
            1920,
            1080,
            30.0,
            "h264",
            "aac",
            True,
            True,
        )

    def _transcript(self) -> Transcript:
        return Transcript("en", "mlx", (Word("Hello.", 0, 900, 0.9),))

    def _add_artifact(self, job, artifacts: Path, kind: str, content: str = "{}") -> None:
        path = artifacts / f"{kind}.json"
        path.write_text(content, encoding="utf-8")
        job.artifacts.append(Artifact(kind, str(path), file_checksum(path)))

    def _job_with_checkpoints(self, root: Path, *, with_words: bool) -> tuple[Path, Path]:
        media = root / "input.mp4"
        media.write_bytes(b"video")
        job = create_job(
            str(media),
            root / "output",
            {
                "engine": "mlx",
                "source_language": "en",
                "segmentation_preset": "compact",
                "source_strategy": "auto",
            },
        )
        artifacts = get_job_file(job).parent / "artifacts"
        artifacts.mkdir()
        media_path = artifacts / "media.json"
        media_path.write_text(json.dumps(asdict(self._media_info(media))), encoding="utf-8")
        job.artifacts.append(Artifact("media_info", str(media_path), file_checksum(media_path)))
        if with_words:
            words = artifacts / "words.json"
            words.write_text(json.dumps(asdict(self._transcript())), encoding="utf-8")
            job.artifacts.append(Artifact("word_transcript", str(words), file_checksum(words)))
        else:
            audio = artifacts / "audio.wav"
            audio.write_bytes(b"audio")
            job.artifacts.append(Artifact("normalized_audio", str(audio), file_checksum(audio)))
        return save_job(job), artifacts

    def test_reuses_words_with_persisted_preset_and_invalidates_downstream(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file, artifacts = self._job_with_checkpoints(Path(directory), with_words=True)
            job = load_job(job_file)
            for kind in (
                "source_candidate_qa",
                "source_cues",
                "source_selection",
                "source_srt",
                "subtitle_qa",
                "translation_resegmentation",
                "translated_cues",
                "translated_srt",
                "bilingual_srt",
                "bilingual_ass",
                "preview_ass",
                "preview_video",
                "burned_video",
                "burn_fingerprint",
                "media_qa",
            ):
                self._add_artifact(job, artifacts, kind)
            self._add_artifact(job, artifacts, "input_translation_subtitles")
            save_job(job)

            with (
                patch("xvt.pipeline.transcription.transcribe_audio") as transcribe,
                patch(
                    "xvt.pipeline.transcription.segment_transcript", wraps=segment_transcript
                ) as segment,
            ):
                rebuild_source(job_file, strategy="asr")

            rebuilt = load_job(job_file)
            artifact_kinds = {artifact.kind for artifact in rebuilt.artifacts}
            transcribe.assert_not_called()
            self.assertEqual(segment.call_args.args[1], segmentation_config_for_preset("compact"))
            self.assertEqual(rebuilt.config["source_strategy"], "asr")
            selection = next(item for item in rebuilt.artifacts if item.kind == "source_selection")
            selection_data = json.loads(Path(selection.path).read_text(encoding="utf-8"))
            self.assertEqual(selection_data["reason"], "forced_asr")
            self.assertIn("input_translation_subtitles", artifact_kinds)
            self.assertFalse(
                {
                    "source_candidate_qa",
                    "translation_resegmentation",
                    "translated_cues",
                    "preview_video",
                    "burn_fingerprint",
                }
                & artifact_kinds
            )

    def test_falls_back_to_asr_from_normalized_audio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, _ = self._job_with_checkpoints(root, with_words=False)
            (root / "input.mp4").unlink()
            with patch(
                "xvt.pipeline.transcription.transcribe_audio", return_value=self._transcript()
            ) as transcribe:
                rebuild_source(job_file, strategy="asr")

        transcribe.assert_called_once()

    def test_reuses_words_when_original_local_media_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, _ = self._job_with_checkpoints(root, with_words=True)
            (root / "input.mp4").unlink()

            with patch("xvt.pipeline.transcription.transcribe_audio") as transcribe:
                result = rebuild_source(job_file, strategy="asr")

            transcribe.assert_not_called()
            self.assertTrue(result.source_cues_path.is_file())
            self.assertTrue(result.selection_path.is_file())
            rebuilt = load_job(job_file)
            self.assertIs(rebuilt.stages[StageName.TRANSCRIBE].status, StageStatus.SUCCEEDED)
            self.assertIs(rebuilt.stages[StageName.NORMALIZE_AUDIO].status, StageStatus.SKIPPED)

    def test_remote_job_uses_remote_resume_when_no_local_checkpoint_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job(
                "https://youtu.be/example", Path(directory), {"source_strategy": "auto"}
            )
            job_file = get_job_file(job)
            with patch("xvt.pipeline.transcription.resume_remote_transcription") as remote:
                with self.assertRaisesRegex(RuntimeError, "required artifacts"):
                    rebuild_source(job_file, strategy="asr", cookies_from_browser="chrome")

        remote.assert_called_once_with(
            job_file,
            cookies_from_browser="chrome",
            proxy=None,
            event_sink=None,
        )

    def test_remote_job_reuses_downloaded_media_without_remote_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job(
                "https://youtu.be/example",
                root,
                {"engine": "mlx", "source_language": "en", "source_strategy": "auto"},
            )
            artifacts = get_job_file(job).parent / "artifacts"
            artifacts.mkdir()
            downloaded = artifacts / "downloaded.m4a"
            downloaded.write_bytes(b"audio")
            job.artifacts.append(
                Artifact("downloaded_media", str(downloaded), file_checksum(downloaded))
            )
            job.stages[StageName.INSPECT].status = StageStatus.FAILED
            job.stages[StageName.INSPECT].error = "old inspect failure"
            job.stages[StageName.ACQUIRE].status = StageStatus.FAILED
            job.stages[StageName.ACQUIRE].error = "old acquire failure"
            job_file = save_job(job)

            def create_audio(arguments, **_kwargs) -> None:
                Path(arguments[-1]).write_bytes(b"normalized")

            with (
                patch("xvt.pipeline.transcription.inspect_remote") as inspect,
                patch("xvt.pipeline.transcription.download_remote_media") as download,
                patch(
                    "xvt.pipeline.transcription.probe_media",
                    return_value=self._media_info(downloaded),
                ),
                patch("xvt.pipeline.transcription.run_command", side_effect=create_audio),
                patch(
                    "xvt.pipeline.transcription.transcribe_audio",
                    return_value=self._transcript(),
                ),
            ):
                result = rebuild_source(job_file, strategy="asr")

            inspect.assert_not_called()
            download.assert_not_called()
            self.assertTrue(result.source_cues_path.is_file())
            rebuilt = load_job(job_file)
            self.assertIs(rebuilt.stages[StageName.INSPECT].status, StageStatus.SUCCEEDED)
            self.assertIs(rebuilt.stages[StageName.ACQUIRE].status, StageStatus.SUCCEEDED)
            self.assertIsNone(rebuilt.stages[StageName.ACQUIRE].error)

    def test_rejects_job_file_that_does_not_match_persisted_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, _ = self._job_with_checkpoints(root, with_words=True)
            moved_job_file = root / "moved-job.json"
            moved_job_file.write_bytes(job_file.read_bytes())

            with self.assertRaisesRegex(ValueError, "persisted identity"):
                rebuild_source(moved_job_file, strategy="asr")

    def test_rejects_invalid_persisted_config_before_invalidating_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, artifacts = self._job_with_checkpoints(root, with_words=True)
            job = load_job(job_file)
            job.config["segmentation_preset"] = "invalid"
            self._add_artifact(job, artifacts, "source_cues")
            self._add_artifact(job, artifacts, "translated_cues")
            save_job(job)

            with self.assertRaisesRegex(ValueError, "unknown segmentation preset"):
                rebuild_source(job_file, strategy="asr")

            artifact_kinds = {artifact.kind for artifact in load_job(job_file).artifacts}
            self.assertIn("source_cues", artifact_kinds)
            self.assertIn("translated_cues", artifact_kinds)

    def test_rejects_invalid_persisted_config_types_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, artifacts = self._job_with_checkpoints(root, with_words=True)
            job = load_job(job_file)
            job.config["source_language"] = ["en"]
            self._add_artifact(job, artifacts, "source_cues")
            save_job(job)

            with self.assertRaisesRegex(ValueError, "source_language must be"):
                rebuild_source(job_file, strategy="asr")

            artifact_kinds = {artifact.kind for artifact in load_job(job_file).artifacts}
            self.assertIn("source_cues", artifact_kinds)

    def test_rejects_local_media_that_is_no_longer_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job(str(Path(directory) / "missing.mp4"), Path(directory), {})
            with self.assertRaisesRegex(ValueError, "available local media file"):
                rebuild_source(get_job_file(job), strategy="asr")

    def test_local_media_without_asr_checkpoint_is_reprobed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            job = create_job(
                str(media),
                root / "output",
                {"engine": "mlx", "source_language": "en", "source_strategy": "auto"},
            )
            artifacts = get_job_file(job).parent / "artifacts"
            artifacts.mkdir()
            stale_media_info = artifacts / "media.json"
            stale_media_info.write_text(
                json.dumps(asdict(self._media_info(media))), encoding="utf-8"
            )
            job.artifacts.append(
                Artifact("media_info", str(stale_media_info), file_checksum(stale_media_info))
            )
            job_file = save_job(job)
            fresh_media_info = self._media_info(media)

            def create_audio(arguments, **_kwargs) -> None:
                Path(arguments[-1]).write_bytes(b"normalized")

            with (
                patch(
                    "xvt.pipeline.transcription.probe_media",
                    return_value=fresh_media_info,
                ) as probe,
                patch("xvt.pipeline.transcription.run_command", side_effect=create_audio),
                patch(
                    "xvt.pipeline.transcription.transcribe_audio",
                    return_value=self._transcript(),
                ),
            ):
                rebuild_source(job_file, strategy="asr")

            probe.assert_called_once_with(media)

    def test_rejects_subtitle_only_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job("/tmp/source.srt", Path(directory), {"input_kind": "subtitle"})
            with self.assertRaisesRegex(ValueError, "subtitle-only jobs are unsupported"):
                rebuild_source(get_job_file(job), strategy="asr")

    def test_failure_does_not_leave_old_downstream_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file, artifacts = self._job_with_checkpoints(Path(directory), with_words=True)
            job = load_job(job_file)
            for kind in ("translated_cues", "bilingual_ass", "preview_video", "burn_fingerprint"):
                self._add_artifact(job, artifacts, kind)
            save_job(job)

            with patch(
                "xvt.pipeline.transcription.check_subtitles", side_effect=RuntimeError("qa failed")
            ):
                with self.assertRaisesRegex(RuntimeError, "qa failed"):
                    rebuild_source(job_file, strategy="asr")

            artifact_kinds = {artifact.kind for artifact in load_job(job_file).artifacts}
            self.assertFalse(
                {"translated_cues", "bilingual_ass", "preview_video", "burn_fingerprint"}
                & artifact_kinds
            )

    def test_continue_remaps_retained_translation_without_workflow_source_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, artifacts = self._job_with_checkpoints(root, with_words=True)
            original_translation = root / "original-translation.srt"
            original_translation.write_text(
                "1\n00:00:00,000 --> 00:00:00,900\n你好。\n",
                encoding="utf-8",
            )
            retained_translation = artifacts / "input-translation.srt"
            retained_translation.write_bytes(original_translation.read_bytes())

            job = load_job(job_file)
            job.config["workflow"] = {
                "target_language": "zh-CN",
                "burn": False,
            }
            job.artifacts.append(
                Artifact(
                    "input_translation_subtitles",
                    str(retained_translation),
                    file_checksum(retained_translation),
                )
            )
            save_job(job)
            original_translation.unlink()

            rebuild_source(job_file, strategy="asr")
            result = continue_workflow(job_file)

            rebuilt = load_job(job_file)
            artifact_kinds = {artifact.kind for artifact in rebuilt.artifacts}
            self.assertEqual(result.status, "completed")
            self.assertIn("translation_mapping", artifact_kinds)
            self.assertIn("translated_cues", artifact_kinds)

    def test_continue_refuses_retained_translation_that_misses_the_rebuilt_timeline(self) -> None:
        """重建后的源时间轴与已存译文对不上时，``continue`` 必须大声失败。

        cue 数恰好相等（这里 1v1）曾让按序硬配一路放行：译文**文本**错位不触碰
        时间轴、文本完整性、显示宽度任何一项确定性 QA，错译会进 ``bilingual.srt``
        并烧进成品。重建后的源 cue 没有任何人主张过与这份旧译文对应，等长在这里
        只是巧合。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, artifacts = self._job_with_checkpoints(root, with_words=True)
            # 已存副本停在 3.0–3.9s，而 words.json 只有 0–900ms 的一个词，
            # 重建出的源 cue 与它完全不相交。
            retained_translation = artifacts / "input-translation.srt"
            retained_translation.write_text(
                "1\n00:00:03,000 --> 00:00:03,900\n你好。\n",
                encoding="utf-8",
            )

            job = load_job(job_file)
            job.config["workflow"] = {"target_language": "zh-CN", "burn": False}
            job.artifacts.append(
                Artifact(
                    "input_translation_subtitles",
                    str(retained_translation),
                    file_checksum(retained_translation),
                )
            )
            save_job(job)

            rebuild_source(job_file, strategy="asr")

            with self.assertRaisesRegex(
                ValueError, "does not line up with the current source timeline"
            ):
                continue_workflow(job_file)

            rebuilt = load_job(job_file)
            artifact_kinds = {artifact.kind for artifact in rebuilt.artifacts}
            self.assertNotIn("translated_cues", artifact_kinds)
            self.assertNotIn("bilingual_srt", artifact_kinds)

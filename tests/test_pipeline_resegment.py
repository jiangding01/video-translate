"""重分段（pipeline.resegment）的流程测试。"""

import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from xvt.job_store import create_job, load_job, save_job
from xvt.models import Artifact, Cue, StageName, StageStatus, Word
from xvt.pipeline import resegment_translation
from xvt.recovery import file_checksum


class ResegmentTranslationTest(unittest.TestCase):
    def test_resegment_translation_replaces_source_cues_and_invalidates_downstream_artifacts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            words = [
                Word(("" if index == 0 else " ") + token, index * 500, index * 500 + 400, 0.9)
                for index, token in enumerate(("This", "subtitle", "needs", "splitting"))
            ]
            source_path = artifacts / "source-cues.json"
            source_path.write_text(
                json.dumps([asdict(Cue(1, 0, 2000, "This subtitle needs splitting", words=words))]),
                encoding="utf-8",
            )
            translated_path = artifacts / "translated-cues.json"
            translated_path.write_text(
                json.dumps(
                    [
                        asdict(
                            Cue(
                                1,
                                0,
                                2000,
                                "This subtitle needs splitting",
                                # 每个切点都违反行首禁则，展示换行切不开，仍是 review；
                                # 宽度只够切成两段，保持原用例的分段数。
                                target_text="ー" * 19,
                                target_language="zh-CN",
                            )
                        )
                    ]
                ),
                encoding="utf-8",
            )
            media_path = artifacts / "media.json"
            media_path.write_text(
                json.dumps({"duration_ms": 2000, "width": 1920, "height": 1080}),
                encoding="utf-8",
            )
            progress_path = artifacts / "translation-progress.json"
            progress_path.write_text("{}", encoding="utf-8")
            imported_translation_path = artifacts / "input-translation.srt"
            imported_translation_path.write_text("old translation", encoding="utf-8")
            mapping_path = artifacts / "translation-mapping.json"
            mapping_path.write_text("{}", encoding="utf-8")
            job.artifacts.extend(
                [
                    Artifact("source_cues", str(source_path), file_checksum(source_path)),
                    Artifact(
                        "translated_cues", str(translated_path), file_checksum(translated_path)
                    ),
                    Artifact("media_info", str(media_path), file_checksum(media_path)),
                    Artifact(
                        "translation_progress", str(progress_path), file_checksum(progress_path)
                    ),
                    Artifact(
                        "input_translation_subtitles",
                        str(imported_translation_path),
                        file_checksum(imported_translation_path),
                    ),
                    Artifact("translation_mapping", str(mapping_path), file_checksum(mapping_path)),
                ]
            )
            job.stages[StageName.SUBTITLE_QA].status = StageStatus.FAILED
            job_file = save_job(job)
            original_job_data = json.loads(job_file.read_text(encoding="utf-8"))
            original_source = source_path.read_text(encoding="utf-8")

            result = resegment_translation(job_file)

            self.assertTrue(result.changed)
            updated = load_job(job_file)
            updated_cues = json.loads(source_path.read_text(encoding="utf-8"))
            self.assertEqual([cue["id"] for cue in updated_cues], [1, 2])
            self.assertTrue(all(cue["provenance"] == "asr_resegmented" for cue in updated_cues))
            artifact_kinds = {artifact.kind for artifact in updated.artifacts}
            self.assertIn("source_srt", artifact_kinds)
            self.assertNotIn("translated_cues", artifact_kinds)
            self.assertNotIn("translation_progress", artifact_kinds)
            self.assertNotIn("input_translation_subtitles", artifact_kinds)
            self.assertNotIn("translation_mapping", artifact_kinds)
            self.assertIn("translation_resegmentation", artifact_kinds)
            self.assertEqual(updated.stages[StageName.TRANSLATE].status, StageStatus.PENDING)
            self.assertEqual(updated.stages[StageName.SUBTITLE_QA].status, StageStatus.PENDING)

            job_file.write_text(
                json.dumps(original_job_data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            source_path.write_text(original_source, encoding="utf-8")
            with patch("xvt.pipeline.resegment.save_subtitles", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    resegment_translation(job_file)

            failed_job = load_job(job_file)
            failed_kinds = {artifact.kind for artifact in failed_job.artifacts}
            self.assertIn("translated_cues", failed_kinds)
            self.assertIn("translation_progress", failed_kinds)
            self.assertEqual(
                failed_job.stages[StageName.SUBTITLE_QA].status,
                StageStatus.FAILED,
            )

    def test_resegment_translation_invalidates_cue_retranslation_artifacts(self) -> None:
        # P7e: cue_retranslation_batches/cue_retranslation_response 引用重分段前的
        # 废弃 cue id，重分段后必须失效，不能以 checksum 有效的姿态残留在
        # job.artifacts 中污染 status/verify/恢复计划的可复用产物清单。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            words = [
                Word(("" if index == 0 else " ") + token, index * 500, index * 500 + 400, 0.9)
                for index, token in enumerate(("This", "subtitle", "needs", "splitting"))
            ]
            source_path = artifacts / "source-cues.json"
            source_path.write_text(
                json.dumps([asdict(Cue(1, 0, 2000, "This subtitle needs splitting", words=words))]),
                encoding="utf-8",
            )
            translated_path = artifacts / "translated-cues.json"
            translated_path.write_text(
                json.dumps(
                    [
                        asdict(
                            Cue(
                                1,
                                0,
                                2000,
                                "This subtitle needs splitting",
                                target_text="长" * 40,
                                target_language="zh-CN",
                            )
                        )
                    ]
                ),
                encoding="utf-8",
            )
            media_path = artifacts / "media.json"
            media_path.write_text(
                json.dumps({"duration_ms": 2000, "width": 1920, "height": 1080}),
                encoding="utf-8",
            )
            cue_retranslation_batches_path = artifacts / "cue-retranslation-batches.json"
            cue_retranslation_batches_path.write_text("{}", encoding="utf-8")
            subtitle_qa_path = artifacts / "qa.json"
            subtitle_qa_path.write_text("{}", encoding="utf-8")
            cue_retranslation_response_path = artifacts / "cue-retranslation-response.json"
            cue_retranslation_response_path.write_text("{}", encoding="utf-8")
            job.artifacts.extend(
                [
                    Artifact("source_cues", str(source_path), file_checksum(source_path)),
                    Artifact(
                        "translated_cues", str(translated_path), file_checksum(translated_path)
                    ),
                    Artifact("media_info", str(media_path), file_checksum(media_path)),
                    Artifact("subtitle_qa", str(subtitle_qa_path), file_checksum(subtitle_qa_path)),
                    Artifact(
                        "cue_retranslation_batches",
                        str(cue_retranslation_batches_path),
                        file_checksum(cue_retranslation_batches_path),
                    ),
                    Artifact(
                        "cue_retranslation_response",
                        str(cue_retranslation_response_path),
                        file_checksum(cue_retranslation_response_path),
                    ),
                ]
            )
            job_file = save_job(job)

            result = resegment_translation(job_file)

            self.assertTrue(result.changed)
            updated = load_job(job_file)
            artifact_kinds = {artifact.kind for artifact in updated.artifacts}
            self.assertNotIn("cue_retranslation_batches", artifact_kinds)
            self.assertNotIn("cue_retranslation_response", artifact_kinds)
            # P7f：旧 subtitle_qa 引用重分段前的 cue，不得以有效产物姿态残留。
            self.assertNotIn("subtitle_qa", artifact_kinds)

    def test_resegment_translation_rejects_imported_source_subtitles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            source_path = artifacts / "source-cues.json"
            source_path.write_text(
                json.dumps(
                    [asdict(Cue(1, 0, 2000, "Imported", provenance="user_source_subtitle"))]
                ),
                encoding="utf-8",
            )
            translated_path = artifacts / "translated-cues.json"
            translated_path.write_text(
                json.dumps(
                    [
                        asdict(
                            Cue(
                                1,
                                0,
                                2000,
                                "Imported",
                                target_text="长" * 40,
                                target_language="zh-CN",
                            )
                        )
                    ]
                ),
                encoding="utf-8",
            )
            media_path = artifacts / "media.json"
            media_path.write_text(
                json.dumps({"duration_ms": 2000, "width": 1920, "height": 1080}),
                encoding="utf-8",
            )
            job.artifacts.extend(
                [
                    Artifact("source_cues", str(source_path), file_checksum(source_path)),
                    Artifact(
                        "translated_cues", str(translated_path), file_checksum(translated_path)
                    ),
                    Artifact("media_info", str(media_path), file_checksum(media_path)),
                ]
            )
            job_file = save_job(job)

            with self.assertRaisesRegex(ValueError, "no ASR word timestamps"):
                resegment_translation(job_file)


if __name__ == "__main__":
    unittest.main()

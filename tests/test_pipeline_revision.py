import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from xvt.errors import UserInputError
from xvt.job_store import get_job_directory, get_job_file, load_job, save_job
from xvt.models import Artifact, Cue, StageName, StageStatus, Word
from xvt.pipeline import (
    acknowledge_subtitle_review,
    apply_source_review_fixes,
    prepare_source_review,
    run_subtitle_transcription,
)
from xvt.recovery import file_checksum, valid_artifact_path
from xvt.workflow import _source_selection_satisfied, continue_workflow
from xvt.workflow_config import WorkflowConfig, set_workflow_config


class SourceReviewPipelineTest(unittest.TestCase):
    def _job(self, root: Path) -> Path:
        source = root / "source.srt"
        source.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nGPT's 5.6 soul.\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\nKeep this line.\n",
            encoding="utf-8",
        )
        job = run_subtitle_transcription(source, root, language="en")
        job.config["source_strategy"] = "auto"
        source_path = valid_artifact_path(job, "source_cues")
        assert source_path is not None
        cues = json.loads(source_path.read_text(encoding="utf-8"))
        cues[0]["words"] = [
            asdict(Word("GPT's", 0, 400, 0.8)),
            asdict(Word("5.6", 400, 700, 0.7)),
        ]
        cues[0]["text_confidence"] = 0.8
        cues[0]["timing_confidence"] = 0.7
        cues[0]["provenance"] = "asr"
        source_path.write_text(json.dumps(cues, ensure_ascii=False), encoding="utf-8")
        for artifact in job.artifacts:
            if artifact.kind == "source_cues":
                artifact.checksum = file_checksum(source_path)
        selection = get_job_directory(job) / "artifacts" / "source-selection.json"
        selection.write_text(
            json.dumps(
                {
                    "strategy": "auto",
                    "candidate_kind": "none",
                    "selected_kind": "asr",
                    "reason": "test",
                    "source_cues_checksum": file_checksum(source_path),
                }
            ),
            encoding="utf-8",
        )
        job.artifacts = [item for item in job.artifacts if item.kind != "source_selection"]
        job.artifacts.append(Artifact("source_selection", str(selection), file_checksum(selection)))
        set_workflow_config(
            job,
            WorkflowConfig(
                target_language="zh-CN",
                translation_quality="polished",
                provider="agent",
            ),
        )
        translated = get_job_directory(job) / "artifacts" / "translated-cues.json"
        translated.write_text(
            json.dumps(
                [
                    asdict(
                        replace(
                            Cue(**{**item, "words": [Word(**word) for word in item["words"]]}),
                            target_text="旧译文",
                            target_language="zh-CN",
                        )
                    )
                    for item in cues
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        job.artifacts.append(
            Artifact("translated_cues", str(translated), file_checksum(translated))
        )
        translation_revision = (
            get_job_directory(job) / "artifacts" / "translation-review-revision.json"
        )
        translation_revision.write_text("{}", encoding="utf-8")
        job.artifacts.append(
            Artifact(
                "translation_review_revision",
                str(translation_revision),
                file_checksum(translation_revision),
            )
        )
        job.config["translation_review_acknowledgment"] = {"stale": True}
        job.config["subtitle_review_acknowledgment"] = {"stale": True}
        save_job(job)
        return get_job_file(job)

    def _response(self, job_file: Path, cue_ids: list[int]) -> Path:
        template = prepare_source_review(job_file, cue_ids)
        return template

    def test_prepare_and_apply_preserve_timeline_and_invalidate_downstream(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            before_job = load_job(job_file)
            before_source = valid_artifact_path(before_job, "source_cues")
            assert before_source is not None
            before_cues = json.loads(before_source.read_text(encoding="utf-8"))
            response = self._response(job_file, [1])
            payload = json.loads(response.read_text(encoding="utf-8"))
            payload["items"][0]["text"] = "GPT-5.6 does in these categories."
            response.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            paths = apply_source_review_fixes(job_file, response)

            job = load_job(job_file)
            source_path = valid_artifact_path(job, "source_cues")
            assert source_path is not None
            after_cues = json.loads(source_path.read_text(encoding="utf-8"))
            self.assertEqual(after_cues[0]["source_text"], "GPT-5.6 does in these categories.")
            for field in (
                "id",
                "start_ms",
                "end_ms",
                "words",
                "source_language",
                "target_language",
                "text_confidence",
                "timing_confidence",
                "provenance",
                "warnings",
            ):
                self.assertEqual(after_cues[0][field], before_cues[0][field])
            self.assertEqual(after_cues[1], before_cues[1])
            kinds = {artifact.kind for artifact in job.artifacts}
            self.assertIn("source_review_revision", kinds)
            self.assertIn("source_srt", kinds)
            self.assertIn("subtitle_qa", kinds)
            self.assertNotIn("translated_cues", kinds)
            self.assertNotIn("translation_review_revision", kinds)
            self.assertNotIn("translation_review_acknowledgment", job.config)
            self.assertNotIn("subtitle_review_acknowledgment", job.config)
            selection_path = valid_artifact_path(job, "source_selection")
            assert selection_path is not None
            selection = json.loads(selection_path.read_text(encoding="utf-8"))
            self.assertEqual(selection["selected_kind"], "human_reviewed")
            self.assertTrue(_source_selection_satisfied(job))
            revision_path = valid_artifact_path(job, "source_review_revision")
            assert revision_path is not None
            revision = json.loads(revision_path.read_text(encoding="utf-8"))
            self.assertEqual(
                revision["reviewed_source_cues_checksum"],
                file_checksum(source_path),
            )
            self.assertEqual(job.stages[StageName.SUBTITLE_QA].status, StageStatus.SUCCEEDED)
            self.assertTrue(all(path.is_file() for path in paths))

    def test_human_selection_rejects_mismatched_revision_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            response = self._response(job_file, [1])
            payload = json.loads(response.read_text(encoding="utf-8"))
            payload["items"][0]["text"] = "Changed."
            response.write_text(json.dumps(payload), encoding="utf-8")
            apply_source_review_fixes(job_file, response)
            job = load_job(job_file)
            revision_path = valid_artifact_path(job, "source_review_revision")
            assert revision_path is not None
            revision = json.loads(revision_path.read_text(encoding="utf-8"))
            revision["reviewed_source_cues_checksum"] = "wrong"
            revision_path.write_text(json.dumps(revision), encoding="utf-8")
            for artifact in job.artifacts:
                if artifact.kind == "source_review_revision":
                    artifact.checksum = file_checksum(revision_path)
            save_job(job)

            self.assertFalse(_source_selection_satisfied(load_job(job_file)))

    def test_stale_response_and_unknown_ids_do_not_change_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            response = self._response(job_file, [1])
            payload = json.loads(response.read_text(encoding="utf-8"))
            payload["items"][0]["id"] = 99
            payload["items"][0]["text"] = "Changed"
            response.write_text(json.dumps(payload), encoding="utf-8")
            before = job_file.read_bytes()

            with self.assertRaisesRegex(UserInputError, "stale"):
                apply_source_review_fixes(job_file, response)

            self.assertEqual(job_file.read_bytes(), before)

    def test_prepare_does_not_overwrite_existing_user_edits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            response = prepare_source_review(job_file, [1])
            payload = json.loads(response.read_text(encoding="utf-8"))
            payload["items"][0]["text"] = "Unsaved user edit"
            response.write_text(json.dumps(payload), encoding="utf-8")

            repeated = prepare_source_review(job_file, [1])

            self.assertEqual(repeated, response)
            persisted = json.loads(repeated.read_text(encoding="utf-8"))
            self.assertEqual(persisted["items"][0]["text"], "Unsaved user edit")

    def test_response_rejects_timing_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            response = self._response(job_file, [1])
            payload = json.loads(response.read_text(encoding="utf-8"))
            payload["items"][0]["text"] = "Changed"
            payload["items"][0]["start_ms"] = 500
            response.write_text(json.dumps(payload), encoding="utf-8")
            before = job_file.read_bytes()

            with self.assertRaisesRegex(UserInputError, "only id and text"):
                apply_source_review_fixes(job_file, response)

            self.assertEqual(job_file.read_bytes(), before)

    def test_source_only_job_keeps_unrequested_stages_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:02,000\nOriginal.\n",
                encoding="utf-8",
            )
            job = run_subtitle_transcription(source, root, language="en")
            job_file = get_job_file(job)
            response = prepare_source_review(job_file, [1])
            payload = json.loads(response.read_text(encoding="utf-8"))
            payload["items"][0]["text"] = "Corrected."
            response.write_text(json.dumps(payload), encoding="utf-8")

            apply_source_review_fixes(job_file, response)

            persisted = load_job(job_file)
            for stage in (StageName.TRANSLATE, StageName.BURN, StageName.MEDIA_QA):
                self.assertEqual(persisted.stages[stage].status, StageStatus.SKIPPED)

    def test_qa_needs_review_persists_revision_without_source_srt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            response = self._response(job_file, [1])
            payload = json.loads(response.read_text(encoding="utf-8"))
            payload["items"][0]["text"] = "x" * 80
            response.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "needs_review"):
                apply_source_review_fixes(job_file, response)

            job = load_job(job_file)
            self.assertEqual(job.stages[StageName.SUBTITLE_QA].status, StageStatus.FAILED)
            self.assertIsNotNone(valid_artifact_path(job, "source_review_revision"))
            self.assertIsNotNone(valid_artifact_path(job, "subtitle_qa"))
            self.assertIsNone(valid_artifact_path(job, "source_srt"))
            self.assertTrue(_source_selection_satisfied(job))

    def test_acknowledged_review_resumes_from_edited_source_cues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            response = self._response(job_file, [1])
            payload = json.loads(response.read_text(encoding="utf-8"))
            payload["items"][0]["text"] = "x" * 80
            response.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                apply_source_review_fixes(job_file, response)

            acknowledge_subtitle_review(job_file)
            result = continue_workflow(job_file)

            job = load_job(job_file)
            source_path = valid_artifact_path(job, "source_cues")
            assert source_path is not None
            cues = json.loads(source_path.read_text(encoding="utf-8"))
            self.assertEqual(cues[0]["source_text"], "x" * 80)
            self.assertIsNotNone(valid_artifact_path(job, "source_srt"))
            self.assertEqual(result.status, "awaiting_input")

    def test_imported_translation_workflow_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            response = self._response(job_file, [1])
            payload = json.loads(response.read_text(encoding="utf-8"))
            payload["items"][0]["text"] = "Changed"
            response.write_text(json.dumps(payload), encoding="utf-8")
            job = load_job(job_file)
            workflow = WorkflowConfig(
                target_language="zh-CN",
                translation_subtitles="/tmp/translated.srt",
            )
            set_workflow_config(job, workflow)
            save_job(job)

            with self.assertRaisesRegex(UserInputError, "imported translation"):
                apply_source_review_fixes(job_file, response)

    def test_save_failure_keeps_previous_job_and_artifact_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            response = self._response(job_file, [1])
            payload = json.loads(response.read_text(encoding="utf-8"))
            payload["items"][0]["text"] = "Changed source text."
            response.write_text(json.dumps(payload), encoding="utf-8")
            job = load_job(job_file)
            old_source = valid_artifact_path(job, "source_cues")
            assert old_source is not None
            before_job = job_file.read_bytes()
            before_source = old_source.read_bytes()

            with (
                patch("xvt.pipeline.revision.save_job", side_effect=OSError("disk full")),
                self.assertRaisesRegex(OSError, "disk full"),
            ):
                apply_source_review_fixes(job_file, response)

            self.assertEqual(job_file.read_bytes(), before_job)
            self.assertEqual(old_source.read_bytes(), before_source)

    def test_events_do_not_include_subtitle_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            events = []
            response = prepare_source_review(job_file, [1], event_sink=events.append)
            payload = json.loads(response.read_text(encoding="utf-8"))
            payload["items"][0]["text"] = "TOP SECRET SOURCE TEXT"
            response.write_text(json.dumps(payload), encoding="utf-8")

            apply_source_review_fixes(job_file, response, event_sink=events.append)

            serialized = "\n".join(event.to_json() for event in events)
            self.assertNotIn("TOP SECRET SOURCE TEXT", serialized)
            self.assertIn("source_review_input_ready", serialized)
            self.assertIn("source_review_revised", serialized)

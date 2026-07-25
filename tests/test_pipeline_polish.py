import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xvt.job_store import get_job_file, load_job, save_job
from xvt.models import Cue, StageName, StageStatus
from xvt.pipeline import (
    apply_agent_translation,
    apply_agent_translation_polish,
    apply_cue_retranslation,
    apply_subtitle_translation,
    prepare_agent_translation,
    run_subtitle_transcription,
    run_translation,
    run_translation_polish,
    translation_polish_satisfied,
)
from xvt.recovery import file_checksum
from xvt.translation_provider import TranslationProviderError
from xvt.workflow_config import WorkflowConfig, set_workflow_config


class TranslationPolishPipelineTest(unittest.TestCase):
    def _write_polish_response(
        self,
        job_file: Path,
        path: Path,
        items: list[dict],
        *,
        fingerprint: str | None = None,
    ) -> None:
        job = load_job(job_file)
        manifest_path = next(
            Path(artifact.path)
            for artifact in job.artifacts
            if artifact.kind == "translation_polish_batches"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        path.write_text(
            json.dumps(
                {
                    "fingerprint": fingerprint or manifest["fingerprint"],
                    "items": items,
                }
            ),
            encoding="utf-8",
        )

    def _translated_job(self, root: Path, *, cue_count: int = 2) -> Path:
        source = root / "source.srt"
        source.write_text(
            "".join(
                f"{cue_id}\n00:00:0{cue_id - 1},000 --> 00:00:0{cue_id},000\nSource {cue_id}.\n\n"
                for cue_id in range(1, cue_count + 1)
            ),
            encoding="utf-8",
        )
        job = run_subtitle_transcription(source, root, language="en")
        job_file = get_job_file(job)
        prepare_agent_translation(
            job_file,
            source_language="en",
            target_language="zh-CN",
            await_input=False,
        )
        response = root / "translation-response.json"
        response.write_text(
            json.dumps(
                {
                    "items": [
                        {"id": cue_id, "text": f"草稿{cue_id}"}
                        for cue_id in range(1, cue_count + 1)
                    ]
                }
            ),
            encoding="utf-8",
        )
        apply_agent_translation(job_file, response, target_language="zh-CN")
        job = load_job(job_file)
        set_workflow_config(
            job,
            WorkflowConfig(
                target_language="zh-CN",
                requested_outputs=("translated_subtitles", "bilingual_subtitles"),
                provider="agent",
                batch_size=1,
            ),
        )
        save_job(job)
        return job_file

    def test_agent_polish_round_trip_preserves_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root)

            result = run_translation_polish(job_file)

            self.assertEqual(result.mode, "agent")
            manifest = json.loads(result.paths[0].read_text(encoding="utf-8"))
            self.assertEqual(manifest["batches"][0]["items"][0]["draft_text"], "草稿1")
            response = root / "polish-response.json"
            self._write_polish_response(
                job_file,
                response,
                [{"id": 1, "text": "润色一"}, {"id": 2, "text": "润色二"}],
            )
            apply_agent_translation_polish(job_file, response)

            job = load_job(job_file)
            kinds = {artifact.kind for artifact in job.artifacts}
            self.assertTrue(
                {
                    "translation_draft_cues",
                    "translation_polish_batches",
                    "translation_polish_progress",
                    "translation_polished_cues",
                    "translated_cues",
                }
                <= kinds
            )
            polished_path = next(
                Path(artifact.path)
                for artifact in job.artifacts
                if artifact.kind == "translation_polished_cues"
            )
            polished = [Cue(**item) for item in json.loads(polished_path.read_text())]
            self.assertEqual([cue.target_text for cue in polished], ["润色一", "润色二"])
            self.assertEqual(
                [(cue.id, cue.start_ms, cue.end_ms) for cue in polished],
                [(1, 0, 1000), (2, 1000, 2000)],
            )
            self.assertEqual(job.config["workflow"]["translation_quality"], "polished")

    def test_api_polish_resumes_from_valid_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root)
            calls = []

            def fail_second(batch, _config, *, correction=None):
                calls.append(batch.id)
                if batch.id == 2:
                    raise TranslationProviderError("offline", retryable=True)
                return {item.id: f"润色{item.id}" for item in batch.items}

            with (
                patch.dict("os.environ", {"TEST_POLISH_API_KEY": "secret"}),
                patch("xvt.pipeline.polish.polish_batch", side_effect=fail_second),
                self.assertRaises(TranslationProviderError),
            ):
                run_translation_polish(
                    job_file,
                    provider="openai-compatible",
                    base_url="https://example.com/v1",
                    model="model",
                    api_key_env="TEST_POLISH_API_KEY",
                    max_retries=0,
                )
            self.assertEqual(calls, [1, 2])

            with (
                patch.dict("os.environ", {"TEST_POLISH_API_KEY": "secret"}),
                patch(
                    "xvt.pipeline.polish.polish_batch",
                    side_effect=lambda batch, _config, **_kwargs: {
                        item.id: f"润色{item.id}" for item in batch.items
                    },
                ) as provider,
            ):
                result = run_translation_polish(job_file)

            self.assertEqual(result.mode, "openai-compatible")
            self.assertEqual(provider.call_count, 1)
            self.assertEqual(provider.call_args.args[0].id, 2)

    def test_agent_polish_rejects_extra_id_without_replacing_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root, cue_count=1)
            run_translation_polish(job_file)
            response = root / "polish-response.json"
            self._write_polish_response(
                job_file,
                response,
                [{"id": 1, "text": "润色"}, {"id": 99, "text": "多余"}],
            )

            with self.assertRaisesRegex(ValueError, "cover every requested cue id"):
                apply_agent_translation_polish(job_file, response)

            job = load_job(job_file)
            draft_path = next(
                Path(artifact.path)
                for artifact in job.artifacts
                if artifact.kind == "translation_draft_cues"
            )
            draft = json.loads(draft_path.read_text())
            self.assertEqual(draft[0]["target_text"], "草稿1")
            self.assertNotIn(
                "translation_polished_cues", {artifact.kind for artifact in job.artifacts}
            )

    def test_agent_polish_rejects_response_for_an_old_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root, cue_count=1)
            run_translation_polish(job_file)
            response = root / "polish-response.json"
            self._write_polish_response(
                job_file,
                response,
                [{"id": 1, "text": "旧润色"}],
                fingerprint="stale",
            )

            with self.assertRaisesRegex(ValueError, "fingerprint does not match"):
                apply_agent_translation_polish(job_file, response)

    def test_cue_retranslation_replaces_draft_and_invalidates_old_polish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root, cue_count=1)
            run_translation_polish(job_file)
            polish_response = root / "polish-response.json"
            self._write_polish_response(
                job_file,
                polish_response,
                [{"id": 1, "text": "旧润色"}],
            )
            apply_agent_translation_polish(job_file, polish_response)
            correction = root / "correction.json"
            correction.write_text(
                json.dumps({"items": [{"id": 1, "text": "新修订"}]}),
                encoding="utf-8",
            )

            paths = apply_cue_retranslation(
                job_file,
                correction,
                cue_ids=(1,),
                target_language="zh-CN",
            )

            self.assertEqual(paths[0].name, "translation-draft-cues.json")
            job = load_job(job_file)
            kinds = {artifact.kind for artifact in job.artifacts}
            self.assertIn("translation_draft_cues", kinds)
            self.assertNotIn("translation_polished_cues", kinds)
            self.assertNotIn("translated_cues", kinds)
            draft = json.loads(paths[0].read_text())
            self.assertEqual(draft[0]["target_text"], "新修订")

    def test_invalid_override_does_not_create_draft_or_change_goal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root, cue_count=1)

            with self.assertRaisesRegex(ValueError, "batch_size"):
                run_translation_polish(job_file, batch_size=0)

            job = load_job(job_file)
            self.assertEqual(job.config["workflow"]["translation_quality"], "standard")
            self.assertNotIn(
                "translation_draft_cues", {artifact.kind for artifact in job.artifacts}
            )
            self.assertIn("translated_cues", {artifact.kind for artifact in job.artifacts})

    def test_polish_satisfaction_rejects_semantically_incomplete_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root, cue_count=1)
            run_translation_polish(job_file)
            response = root / "polish-response.json"
            self._write_polish_response(job_file, response, [{"id": 1, "text": "润色"}])
            apply_agent_translation_polish(job_file, response)
            job = load_job(job_file)
            self.assertTrue(translation_polish_satisfied(job))
            progress_artifact = next(
                artifact
                for artifact in job.artifacts
                if artifact.kind == "translation_polish_progress"
            )
            progress_path = Path(progress_artifact.path)
            progress = json.loads(progress_path.read_text())
            progress["completed_batches"] = []
            progress_path.write_text(json.dumps(progress), encoding="utf-8")
            progress_artifact.checksum = file_checksum(progress_path)
            save_job(job)

            self.assertFalse(translation_polish_satisfied(load_job(job_file)))

    def test_polish_satisfaction_rejects_non_object_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root, cue_count=1)
            run_translation_polish(job_file)
            response = root / "polish-response.json"
            self._write_polish_response(job_file, response, [{"id": 1, "text": "润色"}])
            apply_agent_translation_polish(job_file, response)
            job = load_job(job_file)
            batches_artifact = next(
                artifact
                for artifact in job.artifacts
                if artifact.kind == "translation_polish_batches"
            )
            batches_path = Path(batches_artifact.path)
            batches_path.write_text("[]\n", encoding="utf-8")
            batches_artifact.checksum = file_checksum(batches_path)
            save_job(job)

            self.assertFalse(translation_polish_satisfied(load_job(job_file)))

    def test_missing_draft_does_not_resnapshot_polished_translation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root, cue_count=1)
            run_translation_polish(job_file)
            response = root / "polish-response.json"
            self._write_polish_response(job_file, response, [{"id": 1, "text": "润色"}])
            apply_agent_translation_polish(job_file, response)
            job = load_job(job_file)
            draft = next(
                artifact for artifact in job.artifacts if artifact.kind == "translation_draft_cues"
            )
            Path(draft.path).unlink()

            with self.assertRaisesRegex(ValueError, "no valid translation draft checkpoint"):
                run_translation_polish(job_file)

            job = load_job(job_file)
            self.assertNotIn(
                Path(draft.path),
                [
                    Path(artifact.path)
                    for artifact in job.artifacts
                    if artifact.kind == "translation_draft_cues" and Path(artifact.path).is_file()
                ],
            )

    def test_provider_override_reruns_completed_polish_from_original_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root, cue_count=1)
            run_translation_polish(job_file)
            response = root / "polish-response.json"
            self._write_polish_response(job_file, response, [{"id": 1, "text": "旧润色"}])
            apply_agent_translation_polish(job_file, response)

            with (
                patch.dict("os.environ", {"TEST_POLISH_API_KEY": "secret"}),
                patch(
                    "xvt.pipeline.polish.polish_batch",
                    side_effect=lambda batch, _config, **_kwargs: {
                        item.id: "新润色" for item in batch.items
                    },
                ) as provider,
            ):
                run_translation_polish(
                    job_file,
                    provider="openai-compatible",
                    base_url="https://example.com/v1",
                    model="new-model",
                    api_key_env="TEST_POLISH_API_KEY",
                )

            provider.assert_called_once()
            job = load_job(job_file)
            polished_path = next(
                Path(artifact.path)
                for artifact in job.artifacts
                if artifact.kind == "translation_polished_cues"
            )
            polished = json.loads(polished_path.read_text())
            self.assertEqual(polished[0]["target_text"], "新润色")
            draft_path = next(
                Path(artifact.path)
                for artifact in job.artifacts
                if artifact.kind == "translation_draft_cues"
            )
            draft = json.loads(draft_path.read_text())
            self.assertEqual(draft[0]["target_text"], "草稿1")

    def test_source_language_change_resets_api_polish_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root, cue_count=1)
            job = load_job(job_file)
            source_artifact = next(
                artifact for artifact in job.artifacts if artifact.kind == "source_cues"
            )
            source_path = Path(source_artifact.path)
            source = json.loads(source_path.read_text())
            source[0]["source_language"] = None
            source_path.write_text(json.dumps(source), encoding="utf-8")
            source_artifact.checksum = file_checksum(source_path)
            job.config["source_language"] = "en"
            save_job(job)

            with (
                patch.dict("os.environ", {"TEST_POLISH_API_KEY": "secret"}),
                patch(
                    "xvt.pipeline.polish.polish_batch",
                    side_effect=lambda batch, _config, **_kwargs: {
                        item.id: "英文润色" for item in batch.items
                    },
                ),
            ):
                run_translation_polish(
                    job_file,
                    provider="openai-compatible",
                    base_url="https://example.com/v1",
                    model="model",
                    api_key_env="TEST_POLISH_API_KEY",
                )

            job = load_job(job_file)
            job.config["source_language"] = "fr"
            save_job(job)
            with (
                patch.dict("os.environ", {"TEST_POLISH_API_KEY": "secret"}),
                patch(
                    "xvt.pipeline.polish.polish_batch",
                    side_effect=lambda batch, _config, **_kwargs: {
                        item.id: "法文润色" for item in batch.items
                    },
                ) as provider,
            ):
                run_translation_polish(job_file)

            provider.assert_called_once()

    def test_completed_polish_is_reused_without_mutating_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root, cue_count=1)
            run_translation_polish(job_file)
            response = root / "polish-response.json"
            self._write_polish_response(job_file, response, [{"id": 1, "text": "润色"}])
            apply_agent_translation_polish(job_file, response)
            before = job_file.read_bytes()

            result = run_translation_polish(job_file)

            self.assertEqual(result.mode, "reused")
            self.assertEqual(job_file.read_bytes(), before)

    def test_import_rejects_target_language_change_before_replacing_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root, cue_count=1)
            run_translation_polish(job_file)
            translated = root / "ja.srt"
            translated.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n翻訳\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "polished workflow goal"):
                apply_subtitle_translation(
                    job_file,
                    translated,
                    target_language="ja",
                )

            job = load_job(job_file)
            draft_path = next(
                Path(artifact.path)
                for artifact in job.artifacts
                if artifact.kind == "translation_draft_cues"
            )
            draft = json.loads(draft_path.read_text())
            self.assertEqual(draft[0]["target_text"], "草稿1")
            self.assertNotIn(
                "input_translation_subtitles", {artifact.kind for artifact in job.artifacts}
            )
            self.assertEqual(job.stages[StageName.TRANSLATE].status, StageStatus.AWAITING_INPUT)

    def test_agent_translation_rejects_polished_target_language_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root, cue_count=1)
            run_translation_polish(job_file)
            response = root / "translation-response.json"
            response.write_text(
                json.dumps({"items": [{"id": 1, "text": "翻訳"}]}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "polished workflow goal"):
                apply_agent_translation(job_file, response, target_language="ja")

            job = load_job(job_file)
            self.assertEqual(job.stages[StageName.TRANSLATE].status, StageStatus.AWAITING_INPUT)

    def test_translate_rejects_polished_target_language_change_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root, cue_count=1)
            run_translation_polish(job_file)
            response = root / "polish-response.json"
            self._write_polish_response(job_file, response, [{"id": 1, "text": "润色"}])
            apply_agent_translation_polish(job_file, response)
            before = load_job(job_file)
            before_kinds = {artifact.kind for artifact in before.artifacts}

            with self.assertRaisesRegex(ValueError, "polished workflow goal"):
                run_translation(job_file, target_language="ja", provider="agent")

            after = load_job(job_file)
            self.assertEqual(after.stages[StageName.TRANSLATE].status, StageStatus.SUCCEEDED)
            self.assertEqual({artifact.kind for artifact in after.artifacts}, before_kinds)


if __name__ == "__main__":
    unittest.main()

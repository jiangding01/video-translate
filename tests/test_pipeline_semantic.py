import hashlib
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from xvt.errors import UserInputError
from xvt.job_store import get_job_directory, get_job_file, load_job, save_job
from xvt.models import Artifact, Cue, StageName, StageStatus
from xvt.pipeline import (
    acknowledge_translation_review,
    agent_semantic_review_waiting,
    apply_agent_semantic_review,
    apply_translation_review_fixes,
    complete_acknowledged_semantic_review,
    ensure_translation_review_input,
    prepare_agent_semantic_review,
    run_subtitle_transcription,
    run_translation_semantic_review,
    semantic_review_approved,
    semantic_review_satisfied,
    semantic_review_waiting,
    translation_review_input_path,
)
from xvt.recovery import build_recovery_plan, file_checksum, valid_artifact_path
from xvt.translate import SemanticIssue, SemanticSuggestedItem
from xvt.translation_provider import ProviderResponseError
from xvt.workflow_config import WorkflowConfig, set_workflow_config


def _load_target_text(path: Path, cue_id: int) -> str | None:
    cues = json.loads(path.read_text(encoding="utf-8"))
    return next(item["target_text"] for item in cues if item["id"] == cue_id)


class SemanticReviewPipelineTest(unittest.TestCase):
    def _job(self, root: Path, *, provider: str = "openai-compatible") -> Path:
        source = root / "source.srt"
        source.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHello.\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\nWorld.\n",
            encoding="utf-8",
        )
        job = run_subtitle_transcription(source, root, language="en")
        set_workflow_config(
            job,
            WorkflowConfig(
                target_language="zh-CN",
                translation_quality="polished",
                provider=provider,
                base_url="https://example.com/v1" if provider != "agent" else None,
                model="model" if provider != "agent" else None,
                api_key_env="TEST_TRANSLATION_API_KEY",
            ),
        )
        source_artifact = next(item for item in job.artifacts if item.kind == "source_cues")
        source_cues = [
            Cue(**item)
            for item in json.loads(Path(source_artifact.path).read_text(encoding="utf-8"))
        ]
        polished = [
            replace(cue, target_text=text, target_language="zh-CN")
            for cue, text in zip(source_cues, ("你好。", "世界。"), strict=True)
        ]
        polished_path = get_job_directory(job) / "artifacts" / "translation-polished-cues.json"
        polished_path.write_text(
            json.dumps([asdict(cue) for cue in polished], ensure_ascii=False),
            encoding="utf-8",
        )
        job.artifacts.append(
            Artifact(
                "translation_polished_cues",
                str(polished_path),
                file_checksum(polished_path),
            )
        )
        save_job(job)
        return get_job_file(job)

    def test_api_review_without_issues_finalizes_translation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch("xvt.pipeline.semantic.semantic_review_batch", return_value=()),
            ):
                result = run_translation_semantic_review(job_file)

            job = load_job(job_file)
            self.assertEqual(result.status, "completed")
            self.assertTrue(semantic_review_satisfied(job))
            self.assertEqual(job.stages[StageName.TRANSLATE].status, StageStatus.SUCCEEDED)
            self.assertTrue(
                {"translation_semantic_qa", "translated_cues", "translated_srt", "bilingual_srt"}
                <= {artifact.kind for artifact in job.artifacts}
            )

    def test_completed_api_review_is_reused_without_credentials_or_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch(
                    "xvt.pipeline.semantic.semantic_review_batch",
                    return_value=(),
                ) as provider,
            ):
                run_translation_semantic_review(job_file)

            job = load_job(job_file)
            tracked = {
                Path(item.path): Path(item.path).read_bytes()
                for item in job.artifacts
                if item.kind
                in {
                    "translation_semantic_qa",
                    "translated_cues",
                    "translated_srt",
                    "bilingual_srt",
                    "bilingual_ass",
                }
            }
            before = job_file.read_bytes()

            result = run_translation_semantic_review(job_file)

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.mode, "reused")
            provider.assert_called_once()
            self.assertEqual(job_file.read_bytes(), before)
            self.assertEqual(
                {path: path.read_bytes() for path in tracked},
                tracked,
            )

    def test_api_repairs_complete_group_once_then_reviews_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            issue = SemanticIssue(
                "meaning_error",
                (1,),
                "review",
                "meaning is wrong",
                (SemanticSuggestedItem(1, "您好。"),),
            )
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch(
                    "xvt.pipeline.semantic.semantic_review_batch",
                    side_effect=[(issue,), ()],
                ) as review,
                patch(
                    "xvt.pipeline.semantic.semantic_repair_batch",
                    return_value={1: "您好。"},
                ) as repair,
            ):
                result = run_translation_semantic_review(job_file)

            job = load_job(job_file)
            self.assertEqual(result.status, "completed")
            self.assertEqual(review.call_count, 2)
            repair.assert_called_once()
            kinds = {artifact.kind for artifact in job.artifacts}
            self.assertTrue(
                {
                    "translation_semantic_qa",
                    "translation_semantic_repair",
                    "translation_repaired_cues",
                    "translation_semantic_qa_final",
                }
                <= kinds
            )
            repaired_path = next(
                Path(item.path)
                for item in job.artifacts
                if item.kind == "translation_repaired_cues"
            )
            repaired = json.loads(repaired_path.read_text(encoding="utf-8"))
            self.assertEqual(repaired[0]["target_text"], "您好。")
            self.assertEqual(
                [(item["id"], item["start_ms"], item["end_ms"]) for item in repaired],
                [(1, 0, 1000), (2, 1000, 2000)],
            )
            before = job_file.read_bytes()

            reused = run_translation_semantic_review(job_file)

            self.assertEqual(reused.mode, "reused")
            self.assertEqual(job_file.read_bytes(), before)
            self.assertEqual(review.call_count, 2)
            repair.assert_called_once()
            translated_path = next(
                Path(item.path)
                for item in load_job(job_file).artifacts
                if item.kind == "translated_cues"
            )
            translated = json.loads(translated_path.read_text(encoding="utf-8"))
            self.assertEqual(translated[0]["target_text"], "您好。")

    def test_api_final_review_issues_wait_for_acknowledgment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            initial = SemanticIssue("omission", (1,), "review", "missing", ())
            final = SemanticIssue("terminology", (1,), "review", "term mismatch", ())
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch(
                    "xvt.pipeline.semantic.semantic_review_batch",
                    side_effect=[(initial,), (final,)],
                ),
                patch(
                    "xvt.pipeline.semantic.semantic_repair_batch",
                    return_value={1: "您好。"},
                ),
            ):
                result = run_translation_semantic_review(job_file)

            job = load_job(job_file)
            self.assertEqual(result.status, "awaiting_review")
            self.assertFalse(semantic_review_approved(job))
            self.assertNotIn("translated_cues", {item.kind for item in job.artifacts})
            acknowledge_translation_review(job_file)
            complete_acknowledged_semantic_review(job_file)
            self.assertTrue(semantic_review_satisfied(load_job(job_file)))

    def test_api_review_resumes_after_completed_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            job = load_job(job_file)
            workflow = WorkflowConfig.from_dict(job.config["workflow"])
            set_workflow_config(job, replace(workflow, batch_size=1))
            save_job(job)
            first_calls: list[int] = []

            def first_review(batch, _config, *, correction=None):
                self.assertIsNone(correction)
                first_calls.append(batch.id)
                if batch.id == 2:
                    raise RuntimeError("temporary failure")
                return ()

            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch(
                    "xvt.pipeline.semantic.semantic_review_batch",
                    side_effect=first_review,
                ),
                self.assertRaisesRegex(RuntimeError, "temporary failure"),
            ):
                run_translation_semantic_review(job_file)

            second_calls: list[int] = []

            def second_review(batch, _config, *, correction=None):
                self.assertIsNone(correction)
                second_calls.append(batch.id)
                return ()

            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch(
                    "xvt.pipeline.semantic.semantic_review_batch",
                    side_effect=second_review,
                ),
            ):
                result = run_translation_semantic_review(job_file)

            self.assertEqual(first_calls, [1, 2])
            self.assertEqual(second_calls, [2])
            self.assertEqual(result.status, "completed")

    def test_api_review_uses_issues_correction_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            corrections = []

            def review(_batch, _config, *, correction=None):
                corrections.append(correction)
                if len(corrections) == 1:
                    raise ProviderResponseError("invalid response")
                return ()

            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch("xvt.pipeline.semantic.semantic_review_batch", side_effect=review),
                patch("xvt.pipeline.translation.time.sleep"),
            ):
                run_translation_semantic_review(job_file)

            self.assertIsNone(corrections[0])
            self.assertIn('{"issues":[]}', corrections[1])

    def test_repair_failure_cannot_be_acknowledged_as_final_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            issue = SemanticIssue("omission", (1,), "review", "missing", ())
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch("xvt.pipeline.semantic.semantic_review_batch", return_value=(issue,)),
                patch(
                    "xvt.pipeline.semantic.semantic_repair_batch",
                    side_effect=RuntimeError("repair failed"),
                ),
                self.assertRaisesRegex(RuntimeError, "repair failed"),
            ):
                run_translation_semantic_review(job_file)

            job = load_job(job_file)
            self.assertFalse(semantic_review_waiting(job))
            with self.assertRaisesRegex(UserInputError, "confirmable"):
                acknowledge_translation_review(job_file)

    def test_source_suspect_survives_unrelated_api_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            source_issue = SemanticIssue("source_suspect", (1,), "review", "check source", ())
            repair_issue = SemanticIssue("omission", (2,), "review", "missing", ())
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch(
                    "xvt.pipeline.semantic.semantic_review_batch",
                    side_effect=[(source_issue, repair_issue), ()],
                ),
                patch(
                    "xvt.pipeline.semantic.semantic_repair_batch",
                    return_value={2: "世界。"},
                ),
            ):
                result = run_translation_semantic_review(job_file)

            self.assertEqual(result.status, "awaiting_review")
            job = load_job(job_file)
            final_path = next(
                Path(item.path)
                for item in job.artifacts
                if item.kind == "translation_semantic_qa_final"
            )
            final_report = json.loads(final_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [(item["category"], item["cue_ids"]) for item in final_report["issues"]],
                [("source_suspect", [1])],
            )
            self.assertTrue(semantic_review_waiting(job))

    def test_source_suspect_waits_for_bound_acknowledgment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            issue = SemanticIssue(
                "source_suspect",
                (1,),
                "review",
                "source may contain a name error",
                (),
            )
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch("xvt.pipeline.semantic.semantic_review_batch", return_value=(issue,)),
            ):
                result = run_translation_semantic_review(job_file)

            self.assertEqual(result.status, "awaiting_review")
            self.assertIsNone(
                next(
                    (
                        item
                        for item in load_job(job_file).artifacts
                        if item.kind == "translated_cues"
                    ),
                    None,
                )
            )
            acknowledge_translation_review(job_file)
            complete_acknowledged_semantic_review(job_file)
            self.assertTrue(semantic_review_satisfied(load_job(job_file)))

    def test_human_revision_is_bound_and_runs_review_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job(root)
            issue = SemanticIssue("source_suspect", (1,), "review", "awkward", ())
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch("xvt.pipeline.semantic.semantic_review_batch", return_value=(issue,)),
            ):
                result = run_translation_semantic_review(job_file)

            template = translation_review_input_path(load_job(job_file))
            self.assertEqual(result.status, "awaiting_review")
            self.assertIsNotNone(template)
            job = load_job(job_file)
            workflow = job.config.pop("workflow")
            job.config["target_language"] = "zh-CN"
            save_job(job)
            before_status = job_file.read_bytes()
            recovery = build_recovery_plan(load_job(job_file))
            self.assertEqual(recovery.input_paths, (str(template),))
            self.assertIn("apply-translation-review-fixes", recovery.primary_command or "")
            self.assertIn(
                "acknowledge-translation-review",
                " ".join(recovery.alternative_commands),
            )
            self.assertEqual(job_file.read_bytes(), before_status)
            job = load_job(job_file)
            job.config.pop("target_language")
            job.config["workflow"] = workflow
            save_job(job)
            template.unlink()
            ensure_translation_review_input(job_file)
            template = translation_review_input_path(load_job(job_file))
            self.assertIsNotNone(template)
            payload = json.loads(template.read_text(encoding="utf-8"))
            payload["items"] = [{"id": 1, "text": "你好，朋友。"}]
            response = root / "review-response.json"
            response.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            before_revision = load_job(job_file)
            original_candidate = next(
                Path(item.path)
                for item in before_revision.artifacts
                if item.kind == "translation_polished_cues"
            )
            original_cues = json.loads(original_candidate.read_text(encoding="utf-8"))

            apply_translation_review_fixes(job_file, response)
            job = load_job(job_file)
            self.assertIsNone(translation_review_input_path(job))
            reviewed = next(
                Path(item.path)
                for item in job.artifacts
                if item.kind == "translation_reviewed_cues"
            )
            self.assertEqual(_load_target_text(reviewed, 1), "你好，朋友。")
            reviewed_cues = json.loads(reviewed.read_text(encoding="utf-8"))
            self.assertEqual(
                [
                    (cue["id"], cue["start_ms"], cue["end_ms"], cue["source_text"])
                    for cue in reviewed_cues
                ],
                [
                    (cue["id"], cue["start_ms"], cue["end_ms"], cue["source_text"])
                    for cue in original_cues
                ],
            )
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch(
                    "xvt.pipeline.semantic.semantic_review_batch",
                    return_value=(SemanticIssue("terminology", (2,), "review", "term", ()),),
                ) as review,
                patch("xvt.pipeline.semantic.semantic_repair_batch") as repair,
            ):
                reviewed_result = run_translation_semantic_review(job_file)

            self.assertEqual(reviewed_result.status, "awaiting_review")
            self.assertTrue(review.called)
            repair.assert_not_called()
            second_template = translation_review_input_path(load_job(job_file))
            self.assertNotEqual(second_template, template)
            second_payload = json.loads(second_template.read_text(encoding="utf-8"))
            second_payload["items"] = [{"id": 2, "text": "世界，你好。"}]
            response.write_text(json.dumps(second_payload, ensure_ascii=False), encoding="utf-8")
            apply_translation_review_fixes(job_file, response)
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch("xvt.pipeline.semantic.semantic_review_batch", return_value=()),
            ):
                completed = run_translation_semantic_review(job_file)
            self.assertEqual(completed.status, "completed")
            self.assertTrue(semantic_review_satisfied(load_job(job_file)))

    def test_agent_human_revision_manifest_disables_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job(root, provider="agent")
            manifest_path = prepare_agent_semantic_review(job_file)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            response = root / "semantic-response.json"
            response.write_text(
                json.dumps(
                    {
                        "fingerprint": manifest["fingerprint"],
                        "initial_results": [
                            {
                                "batch": batch["id"],
                                "issues": (
                                    [
                                        {
                                            "category": "source_suspect",
                                            "cue_ids": [1],
                                            "severity": "review",
                                            "message": "check",
                                            "suggested_items": [],
                                        }
                                    ]
                                    if index == 0
                                    else []
                                ),
                            }
                            for index, batch in enumerate(manifest["batches"])
                        ],
                    }
                ),
                encoding="utf-8",
            )
            apply_agent_semantic_review(job_file, response)
            template = translation_review_input_path(load_job(job_file))
            payload = json.loads(template.read_text(encoding="utf-8"))
            payload["items"] = [{"id": 1, "text": "人工译文。"}]
            response.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            apply_translation_review_fixes(job_file, response)

            reviewed_manifest_path = prepare_agent_semantic_review(job_file)
            reviewed_manifest = json.loads(reviewed_manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(reviewed_manifest["task"], "semantic_review_only")
            response.write_text(
                json.dumps(
                    {
                        "fingerprint": reviewed_manifest["fingerprint"],
                        "initial_results": [
                            {"batch": batch["id"], "issues": []}
                            for batch in reviewed_manifest["batches"]
                        ],
                    }
                ),
                encoding="utf-8",
            )
            completed = apply_agent_semantic_review(job_file, response)
            self.assertEqual(completed.status, "completed")
            self.assertTrue(semantic_review_satisfied(load_job(job_file)))

    def test_reviewed_candidate_can_be_explicitly_acknowledged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job(root)
            issue = SemanticIssue("source_suspect", (1,), "review", "check", ())
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch("xvt.pipeline.semantic.semantic_review_batch", return_value=(issue,)),
            ):
                run_translation_semantic_review(job_file)
            template = translation_review_input_path(load_job(job_file))
            payload = json.loads(template.read_text(encoding="utf-8"))
            payload["items"] = [{"id": 1, "text": "确认后的译文。"}]
            response = root / "review-response.json"
            response.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            apply_translation_review_fixes(job_file, response)
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch("xvt.pipeline.semantic.semantic_review_batch", return_value=(issue,)),
            ):
                run_translation_semantic_review(job_file)

            report = acknowledge_translation_review(job_file)
            self.assertTrue(report.name.startswith("translation-semantic-qa-reviewed-"))
            complete_acknowledged_semantic_review(job_file)
            self.assertTrue(semantic_review_satisfied(load_job(job_file)))

    def test_stale_human_revision_does_not_mutate_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job(root)
            issue = SemanticIssue("source_suspect", (1,), "review", "check", ())
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch("xvt.pipeline.semantic.semantic_review_batch", return_value=(issue,)),
            ):
                run_translation_semantic_review(job_file)
            template = translation_review_input_path(load_job(job_file))
            payload = json.loads(template.read_text(encoding="utf-8"))
            payload["revision_fingerprint"] = "stale"
            response = root / "stale-response.json"
            response.write_text(json.dumps(payload), encoding="utf-8")
            before = job_file.read_bytes()

            with self.assertRaisesRegex(UserInputError, "stale"):
                apply_translation_review_fixes(job_file, response)

            self.assertEqual(job_file.read_bytes(), before)

    def test_failed_second_revision_keeps_previous_checkpoint_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job(root)
            first_issue = SemanticIssue("source_suspect", (1,), "review", "check", ())
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch(
                    "xvt.pipeline.semantic.semantic_review_batch",
                    return_value=(first_issue,),
                ),
            ):
                run_translation_semantic_review(job_file)
            template = translation_review_input_path(load_job(job_file))
            payload = json.loads(template.read_text(encoding="utf-8"))
            payload["items"] = [{"id": 1, "text": "第一轮人工译文。"}]
            response = root / "review-response.json"
            response.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            apply_translation_review_fixes(job_file, response)

            second_issue = SemanticIssue("terminology", (2,), "review", "term", ())
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch(
                    "xvt.pipeline.semantic.semantic_review_batch",
                    return_value=(second_issue,),
                ),
            ):
                run_translation_semantic_review(job_file)
            template = translation_review_input_path(load_job(job_file))
            payload = json.loads(template.read_text(encoding="utf-8"))
            payload["items"] = [{"id": 2, "text": "第二轮人工译文。"}]
            response.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            before = job_file.read_bytes()

            with (
                patch("xvt.pipeline.revision.save_job", side_effect=OSError("disk full")),
                self.assertRaisesRegex(OSError, "disk full"),
            ):
                apply_translation_review_fixes(job_file, response)

            self.assertEqual(job_file.read_bytes(), before)
            persisted = load_job(job_file)
            self.assertIsNotNone(valid_artifact_path(persisted, "translation_review_revision"))
            self.assertIsNotNone(valid_artifact_path(persisted, "translation_reviewed_cues"))
            self.assertIsNotNone(valid_artifact_path(persisted, "translation_semantic_qa_reviewed"))

    def test_human_revision_clears_stale_subtitle_review_acknowledgment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job(root)
            issue = SemanticIssue("source_suspect", (1,), "review", "check", ())
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch("xvt.pipeline.semantic.semantic_review_batch", return_value=(issue,)),
            ):
                run_translation_semantic_review(job_file)
            job = load_job(job_file)
            job.config["subtitle_review_acknowledgment"] = {
                "source_cues_checksum": next(
                    artifact.checksum
                    for artifact in job.artifacts
                    if artifact.kind == "source_cues"
                ),
                "acknowledged_issues": [["display_width", 1]],
            }
            save_job(job)
            template = translation_review_input_path(job)
            payload = json.loads(template.read_text(encoding="utf-8"))
            payload["items"] = [{"id": 1, "text": "修改后的译文。"}]
            response = root / "review-response.json"
            response.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            apply_translation_review_fixes(job_file, response)

            self.assertNotIn("subtitle_review_acknowledgment", load_job(job_file).config)

    def test_invalid_human_revision_items_do_not_mutate_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job(root)
            issue = SemanticIssue("source_suspect", (1,), "review", "check", ())
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch("xvt.pipeline.semantic.semantic_review_batch", return_value=(issue,)),
            ):
                run_translation_semantic_review(job_file)
            template = translation_review_input_path(load_job(job_file))
            base = json.loads(template.read_text(encoding="utf-8"))
            invalid_payloads = (
                {**base, "scope": "source"},
                {**base, "items": [{"id": 999, "text": "未知"}]},
                {**base, "items": [{"id": 1, "text": "一"}, {"id": 1, "text": "二"}]},
                {**base, "items": [{"id": 1, "text": "  "}]},
                base,
            )
            before = job_file.read_bytes()
            response = root / "invalid-response.json"
            for payload in invalid_payloads:
                response.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                with self.assertRaises(UserInputError):
                    apply_translation_review_fixes(job_file, response)
                self.assertEqual(job_file.read_bytes(), before)

    def test_acknowledgment_invalidates_when_candidate_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            issue = SemanticIssue("source_suspect", (1,), "review", "check source", ())
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch("xvt.pipeline.semantic.semantic_review_batch", return_value=(issue,)),
            ):
                run_translation_semantic_review(job_file)
            acknowledge_translation_review(job_file)
            job = load_job(job_file)
            self.assertTrue(semantic_review_approved(job))
            candidate = next(
                Path(item.path)
                for item in job.artifacts
                if item.kind == "translation_polished_cues"
            )
            candidate.write_text(candidate.read_text(encoding="utf-8") + " ", encoding="utf-8")

            self.assertFalse(semantic_review_approved(load_job(job_file)))

    def test_deterministic_subtitle_qa_still_blocks_after_semantic_ack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            job = load_job(job_file)
            artifact = next(
                item for item in job.artifacts if item.kind == "translation_polished_cues"
            )
            path = Path(artifact.path)
            cues = [Cue(**item) for item in json.loads(path.read_text(encoding="utf-8"))]
            cues[0] = replace(cues[0], target_text="超长字幕" * 30)
            path.write_text(
                json.dumps([asdict(cue) for cue in cues], ensure_ascii=False),
                encoding="utf-8",
            )
            artifact.checksum = file_checksum(path)
            save_job(job)
            issue = SemanticIssue("source_suspect", (1,), "review", "check source", ())
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch("xvt.pipeline.semantic.semantic_review_batch", return_value=(issue,)),
            ):
                run_translation_semantic_review(job_file)
            acknowledge_translation_review(job_file)

            with self.assertRaisesRegex(RuntimeError, "subtitle QA needs_review"):
                complete_acknowledged_semantic_review(job_file)

    def test_agent_combined_review_without_issues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job(root, provider="agent")
            manifest_path = prepare_agent_semantic_review(job_file)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            response = root / "semantic-response.json"
            response.write_text(
                json.dumps(
                    {
                        "fingerprint": manifest["fingerprint"],
                        "initial_results": [
                            {"batch": batch["id"], "issues": []} for batch in manifest["batches"]
                        ],
                        "repair_results": [],
                        "final_results": [],
                    }
                ),
                encoding="utf-8",
            )

            result = apply_agent_semantic_review(job_file, response)

            self.assertEqual(result.status, "completed")
            self.assertTrue(semantic_review_satisfied(load_job(job_file)))

    def test_agent_combined_response_repairs_once_and_finalizes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job(root, provider="agent")
            manifest_path = prepare_agent_semantic_review(job_file)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            response = root / "semantic-response.json"
            response.write_text(
                json.dumps(
                    {
                        "fingerprint": manifest["fingerprint"],
                        "initial_results": [
                            {
                                "batch": manifest["batches"][0]["id"],
                                "issues": [
                                    {
                                        "category": "meaning_error",
                                        "cue_ids": [1],
                                        "severity": "review",
                                        "message": "wrong meaning",
                                        "suggested_items": [{"id": 1, "text": "您好。"}],
                                    }
                                ],
                            }
                        ],
                        "repair_results": [{"batch": 1, "items": [{"id": 1, "text": "您好。"}]}],
                        "final_results": [{"batch": manifest["batches"][0]["id"], "issues": []}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = apply_agent_semantic_review(job_file, response)

            self.assertEqual(result.status, "completed")
            job = load_job(job_file)
            self.assertTrue(semantic_review_satisfied(job))
            repaired_path = next(
                Path(item.path)
                for item in job.artifacts
                if item.kind == "translation_repaired_cues"
            )
            repaired = json.loads(repaired_path.read_text(encoding="utf-8"))
            self.assertEqual(repaired[0]["target_text"], "您好。")

    def test_agent_invalid_repair_keeps_manifest_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job(root, provider="agent")
            manifest_path = prepare_agent_semantic_review(job_file)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            original = manifest_path.read_bytes()
            response = root / "semantic-response.json"
            response.write_text(
                json.dumps(
                    {
                        "fingerprint": manifest["fingerprint"],
                        "initial_results": [
                            {
                                "batch": manifest["batches"][0]["id"],
                                "issues": [
                                    {
                                        "category": "meaning_error",
                                        "cue_ids": [1],
                                        "severity": "review",
                                        "message": "wrong",
                                        "suggested_items": [],
                                    }
                                ],
                            }
                        ],
                        "repair_results": [],
                        "final_results": [],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "every repair batch"):
                apply_agent_semantic_review(job_file, response)

            self.assertEqual(manifest_path.read_bytes(), original)
            self.assertTrue(agent_semantic_review_waiting(load_job(job_file)))

    def test_source_suspect_survives_unrelated_agent_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job(root, provider="agent")
            manifest_path = prepare_agent_semantic_review(job_file)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            response = root / "semantic-response.json"
            response.write_text(
                json.dumps(
                    {
                        "fingerprint": manifest["fingerprint"],
                        "initial_results": [
                            {
                                "batch": manifest["batches"][0]["id"],
                                "issues": [
                                    {
                                        "category": "source_suspect",
                                        "cue_ids": [1],
                                        "severity": "review",
                                        "message": "check source",
                                        "suggested_items": [],
                                    },
                                    {
                                        "category": "omission",
                                        "cue_ids": [2],
                                        "severity": "review",
                                        "message": "missing",
                                        "suggested_items": [],
                                    },
                                ],
                            }
                        ],
                        "repair_results": [{"batch": 1, "items": [{"id": 2, "text": "世界。"}]}],
                        "final_results": [{"batch": manifest["batches"][0]["id"], "issues": []}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = apply_agent_semantic_review(job_file, response)

            self.assertEqual(result.status, "awaiting_review")
            self.assertTrue(semantic_review_waiting(load_job(job_file)))

    def test_agent_manifest_is_bound_to_current_task_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job(root, provider="agent")
            manifest_path = prepare_agent_semantic_review(job_file)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["task_version"] = 999
            canonical = {key: value for key, value in manifest.items() if key != "fingerprint"}
            manifest["fingerprint"] = hashlib.sha256(
                json.dumps(
                    canonical,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            job = load_job(job_file)
            artifact = next(
                item for item in job.artifacts if item.kind == "translation_semantic_qa"
            )
            artifact.checksum = file_checksum(manifest_path)
            save_job(job)

            self.assertFalse(agent_semantic_review_waiting(load_job(job_file)))

    def test_final_report_requires_complete_repair_artifact_chain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            issue = SemanticIssue("omission", (1,), "review", "missing", ())
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch(
                    "xvt.pipeline.semantic.semantic_review_batch",
                    side_effect=[(issue,), ()],
                ),
                patch(
                    "xvt.pipeline.semantic.semantic_repair_batch",
                    return_value={1: "您好。"},
                ),
            ):
                run_translation_semantic_review(job_file)

            job = load_job(job_file)
            repaired_path = next(
                Path(item.path)
                for item in job.artifacts
                if item.kind == "translation_repaired_cues"
            )
            repaired_path.write_text(
                repaired_path.read_text(encoding="utf-8") + " ",
                encoding="utf-8",
            )

            job = load_job(job_file)
            self.assertFalse(semantic_review_approved(job))
            self.assertFalse(semantic_review_satisfied(job))

    def test_agent_rejects_stale_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job(root, provider="agent")
            prepare_agent_semantic_review(job_file)
            response = root / "semantic-response.json"
            response.write_text(
                json.dumps(
                    {
                        "fingerprint": "stale",
                        "initial_results": [],
                        "repair_results": [],
                        "final_results": [],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "fingerprint"):
                apply_agent_semantic_review(job_file, response)


if __name__ == "__main__":
    unittest.main()

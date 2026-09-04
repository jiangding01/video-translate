import hashlib
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from xvt.candidate_qa import translation_candidate_qa_issues
from xvt.errors import ExitCode, UserInputError
from xvt.events import EventType
from xvt.job_store import get_job_directory, get_job_file, load_job, save_job
from xvt.models import Artifact, Cue, StageName, StageStatus
from xvt.pipeline import (
    acknowledge_translation_review,
    agent_semantic_review_waiting,
    apply_agent_semantic_review,
    apply_translation_review_fixes,
    complete_acknowledged_semantic_review,
    ensure_semantic_review_inputs,
    ensure_translation_review_input,
    prepare_agent_semantic_review,
    run_subtitle_transcription,
    run_translation_semantic_review,
    semantic_review_approved,
    semantic_review_satisfied,
    semantic_review_waiting,
    source_review_input_path,
    translation_review_input_path,
)
from xvt.pipeline.revision import translation_review_stem
from xvt.recovery import build_recovery_plan, file_checksum, valid_artifact_path
from xvt.subtitles import save_subtitles
from xvt.translate import SemanticIssue, SemanticSuggestedItem
from xvt.translation_provider import ProviderResponseError
from xvt.workflow_config import WorkflowConfig, get_workflow_config, set_workflow_config


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

    def test_reused_review_rebuilds_invalid_translated_qa_report(self) -> None:
        """复用短路必须把译文 QA 报告算进完成条件。

        漏检时 polished 链路会在 QA 报告失效后空转短路：语义校对报「已完成」，
        而 burn/preview 门仍因缺有效报告拒绝放行，用户没有任何修复路径。
        """

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

                qa_path = valid_artifact_path(load_job(job_file), "translated_subtitle_qa")
                self.assertIsNotNone(qa_path)
                qa_path.write_text("{}", encoding="utf-8")
                self.assertIsNone(valid_artifact_path(load_job(job_file), "translated_subtitle_qa"))

                result = run_translation_semantic_review(job_file)

            self.assertEqual(result.status, "completed")
            provider.assert_called_once()
            rebuilt_qa = valid_artifact_path(load_job(job_file), "translated_subtitle_qa")
            self.assertIsNotNone(rebuilt_qa)
            self.assertEqual(
                json.loads(rebuilt_qa.read_text(encoding="utf-8"))["status"],
                "pass",
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
                    return_value={1: "您好，"},
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
            self.assertEqual(repaired[0]["target_text"], "您好")
            candidate_qa = json.loads(
                valid_artifact_path(job, "translation_candidate_qa").read_text(encoding="utf-8")
            )
            self.assertEqual(
                candidate_qa["candidate_cues_checksum"],
                file_checksum(repaired_path),
            )
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
            self.assertEqual(translated[0]["target_text"], "您好")

    def test_repair_chain_reuse_honours_job_config_policy_fallback(self) -> None:
        """workflow 未快照 policy 时，构建侧与校验侧必须走同一份 job.config 回退。

        修复前校验侧直接读 ``config.translation_policy``（None），重建出的 repair
        批次与落盘批次不一致 → ``_repair_chain_satisfied`` 恒 False → 语义链永不收敛。
        """

        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            job = load_job(job_file)
            # workflow 快照里 translation_policy 为 None，只有 job.config 有非默认 policy。
            self.assertIsNone(get_workflow_config(job).translation_policy)
            job.config["translation_policy"] = {"style": "concise", "terms": []}
            save_job(job)

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
                    return_value={1: "您好，"},
                ) as repair,
            ):
                result = run_translation_semantic_review(job_file)
                self.assertEqual(result.status, "completed")

                reused = run_translation_semantic_review(job_file)

            # 复用判定成立说明校验侧重建的批次与构建侧逐字节一致。
            self.assertEqual(reused.mode, "reused")
            self.assertEqual(review.call_count, 2)
            repair.assert_called_once()

            # 落盘的 policy 也必须是回退后的那份，而不是默认空 policy。
            repair_payload = json.loads(
                valid_artifact_path(load_job(job_file), "translation_semantic_repair").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(repair_payload["batches"][0]["policy"]["style"], "concise")

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

            # 模拟语义复核前已经生成译文产物，但此前的字幕 QA 失败仍残留在 translate stage。
            candidate_artifact = next(
                item for item in job.artifacts if item.kind == "translation_repaired_cues"
            )
            candidate_path = Path(candidate_artifact.path)
            translated_path = candidate_path.with_name("translated-cues.json")
            translated_path.write_bytes(candidate_path.read_bytes())
            candidate = [
                Cue(**item) for item in json.loads(candidate_path.read_text(encoding="utf-8"))
            ]
            translated_srt = save_subtitles(
                candidate,
                translated_path.with_name("translated.srt"),
                mode="translated",
            )
            bilingual_srt = save_subtitles(
                candidate,
                translated_path.with_name("bilingual.srt"),
                mode="bilingual",
            )
            job.artifacts.extend(
                [
                    Artifact(
                        "translated_cues", str(translated_path), file_checksum(translated_path)
                    ),
                    Artifact("translated_srt", str(translated_srt), file_checksum(translated_srt)),
                    Artifact("bilingual_srt", str(bilingual_srt), file_checksum(bilingual_srt)),
                ]
            )
            job.stages[StageName.TRANSLATE].status = StageStatus.FAILED
            job.stages[StageName.TRANSLATE].error = "subtitle QA needs_review"
            save_job(job)

            acknowledge_translation_review(job_file)
            complete_acknowledged_semantic_review(job_file)
            updated = load_job(job_file)
            self.assertTrue(semantic_review_satisfied(updated))
            self.assertEqual(updated.stages[StageName.TRANSLATE].status, StageStatus.SUCCEEDED)
            self.assertIsNone(updated.stages[StageName.TRANSLATE].error)

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

    def test_mixed_final_review_separates_source_and_translation_templates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            source_issue = SemanticIssue("source_suspect", (1,), "review", "check source", ())
            repair_issue = SemanticIssue("omission", (2,), "review", "missing", ())
            final_issue = SemanticIssue("meaning_error", (2,), "review", "still wrong", ())
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch(
                    "xvt.pipeline.semantic.semantic_review_batch",
                    side_effect=[(source_issue, repair_issue), (final_issue,)],
                ),
                patch(
                    "xvt.pipeline.semantic.semantic_repair_batch",
                    return_value={2: "世界。"},
                ),
            ):
                result = run_translation_semantic_review(job_file)

            self.assertEqual(result.status, "awaiting_review")
            job = load_job(job_file)
            source_template = source_review_input_path(job, [1])
            translation_template = translation_review_input_path(job)
            assert source_template is not None
            assert translation_template is not None
            source_payload = json.loads(source_template.read_text(encoding="utf-8"))
            translation_payload = json.loads(translation_template.read_text(encoding="utf-8"))
            self.assertEqual([item["id"] for item in source_payload["items"]], [1])
            self.assertEqual([item["id"] for item in translation_payload["items"]], [1, 2])

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
            self.assertEqual(
                {path.name.split("-")[0] for path in result.paths[-2:]},
                {"source", "translation"},
            )
            source_template = source_review_input_path(load_job(job_file), [1])
            assert source_template is not None
            source_payload = json.loads(source_template.read_text(encoding="utf-8"))
            self.assertEqual(source_payload["items"], [{"id": 1, "text": "Hello."}])
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

    def test_legacy_waiting_job_lazily_recreates_source_review_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            issue = SemanticIssue("source_suspect", (1,), "review", "check source", ())
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch("xvt.pipeline.semantic.semantic_review_batch", return_value=(issue,)),
            ):
                run_translation_semantic_review(job_file)

            source_template = source_review_input_path(load_job(job_file), [1])
            assert source_template is not None
            source_template.unlink()
            before_prepare = job_file.read_bytes()
            inputs = ensure_semantic_review_inputs(job_file)

            self.assertEqual(
                {path.name.split("-")[0] for path in inputs}, {"source", "translation"}
            )
            self.assertTrue(source_template.exists())
            self.assertEqual(job_file.read_bytes(), before_prepare)
            self.assertIsNotNone(source_review_input_path(load_job(job_file), [1]))

    def test_imported_translation_workflow_suppresses_source_review_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            job = load_job(job_file)
            set_workflow_config(
                job,
                WorkflowConfig(
                    target_language="zh-CN",
                    translation_quality="polished",
                    provider="openai-compatible",
                    base_url="https://example.com/v1",
                    model="model",
                    api_key_env="TEST_TRANSLATION_API_KEY",
                    translation_subtitles="/tmp/translated.srt",
                ),
            )
            save_job(job)
            issue = SemanticIssue("source_suspect", (1,), "review", "check source", ())
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch("xvt.pipeline.semantic.semantic_review_batch", return_value=(issue,)),
            ):
                result = run_translation_semantic_review(job_file)

            job = load_job(job_file)
            translation_template = translation_review_input_path(job)
            self.assertEqual(result.status, "awaiting_review")
            self.assertIsNotNone(translation_template)
            self.assertIsNone(source_review_input_path(job, [1]))

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
            source_template = source_review_input_path(load_job(job_file), [1])
            self.assertEqual(result.status, "awaiting_review")
            self.assertIsNotNone(template)
            self.assertIsNotNone(source_template)
            job = load_job(job_file)
            workflow = job.config.pop("workflow")
            job.config["target_language"] = "zh-CN"
            save_job(job)
            before_status = job_file.read_bytes()
            recovery = build_recovery_plan(load_job(job_file))
            self.assertEqual(
                recovery.input_paths,
                (
                    str(source_template),
                    str(source_template.with_suffix(".srt")),
                    str(template),
                    str(template.with_suffix(".srt")),
                ),
            )
            self.assertIn("apply-source-review-fixes", recovery.primary_command or "")
            self.assertIn(
                "apply-translation-review-fixes",
                " ".join(recovery.alternative_commands),
            )
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
                    return_value=(SemanticIssue("terminology", (1,), "review", "term", ()),),
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
            second_payload["items"] = [{"id": 1, "text": "你好，大家。"}]
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
            self.assertEqual(manifest["review_prompt_version"], 4)
            self.assertEqual(manifest["repair_prompt_version"], 3)
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
            self.assertEqual(reviewed_manifest["review_scope"]["changed_ids"], [1])
            self.assertEqual(reviewed_manifest["review_scope"]["reviewed_ids"], [1])
            self.assertEqual(reviewed_manifest["preserved_issues"], [])
            self.assertEqual(
                [item["id"] for item in reviewed_manifest["batches"][0]["items"]],
                [1],
            )
            self.assertEqual(
                [item["id"] for item in reviewed_manifest["batches"][0]["context_after"]],
                [2],
            )
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

    def test_human_revision_scopes_review_and_preserves_untouched_issues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job(root)
            first = SemanticIssue("source_suspect", (1,), "review", "first", ())
            second = SemanticIssue("source_suspect", (2,), "review", "second", ())
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch(
                    "xvt.pipeline.semantic.semantic_review_batch",
                    return_value=(first, second),
                ),
            ):
                run_translation_semantic_review(job_file)

            template = translation_review_input_path(load_job(job_file))
            payload = json.loads(template.read_text(encoding="utf-8"))
            payload["items"] = [
                {"id": 1, "text": "你好。"},
                {"id": 2, "text": "世间。"},
            ]
            response = root / "review-response.json"
            response.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            apply_translation_review_fixes(job_file, response)

            job = load_job(job_file)
            revision_path = valid_artifact_path(job, "translation_review_revision")
            revision = json.loads(revision_path.read_text(encoding="utf-8"))
            self.assertEqual(revision["template_ids"], [1, 2])
            self.assertEqual(revision["changed_ids"], [2])
            self.assertEqual(len(revision["base_issues"]), 2)
            events = []
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch("xvt.pipeline.semantic.semantic_review_batch", return_value=()) as review,
            ):
                result = run_translation_semantic_review(job_file, event_sink=events.append)

            self.assertEqual(result.status, "awaiting_review")
            reviewed_batch = review.call_args.args[0]
            self.assertEqual([item.id for item in reviewed_batch.items], [2])
            started = [
                event for event in events if event.type == EventType.TRANSLATION_BATCH_STARTED
            ]
            self.assertEqual(started[0].data["batch"], 2)
            self.assertEqual(started[0].data["batch_index"], 1)
            self.assertEqual(started[0].data["batch_total"], 1)
            report_path = valid_artifact_path(
                load_job(job_file), "translation_semantic_qa_reviewed"
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [(issue["category"], issue["cue_ids"]) for issue in report["issues"]],
                [("source_suspect", [1])],
            )
            self.assertEqual(report["review_scope"]["changed_ids"], [2])
            self.assertEqual(report["review_scope"]["reviewed_ids"], [2])

    def test_json_revision_requires_complete_template_id_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job(root)
            issue = SemanticIssue("source_suspect", (1, 2), "review", "check", ())
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch("xvt.pipeline.semantic.semantic_review_batch", return_value=(issue,)),
            ):
                run_translation_semantic_review(job_file)
            template = translation_review_input_path(load_job(job_file))
            payload = json.loads(template.read_text(encoding="utf-8"))
            payload["items"] = [{"id": 1, "text": "只提交一条。"}]
            response = root / "partial-review.json"
            response.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            with self.assertRaisesRegex(UserInputError, "complete template cue id set"):
                apply_translation_review_fixes(job_file, response)

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

            second_issue = SemanticIssue("terminology", (1,), "review", "term", ())
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
            payload["items"] = [{"id": 1, "text": "第二轮人工译文。"}]
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

    def test_srt_and_json_revision_channels_produce_identical_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job(root)
            issue = SemanticIssue("source_suspect", (1, 2), "review", "check", ())
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch("xvt.pipeline.semantic.semantic_review_batch", return_value=(issue,)),
            ):
                run_translation_semantic_review(job_file)
            template = translation_review_input_path(load_job(job_file))
            self.assertIsNotNone(template)
            srt_template = template.with_suffix(".srt")
            self.assertTrue(srt_template.is_file())
            template_text = srt_template.read_text(encoding="utf-8")
            self.assertIn("#1", template_text)
            self.assertIn("你好。", template_text)
            before = job_file.read_bytes()

            payload = json.loads(template.read_text(encoding="utf-8"))
            payload["items"] = [
                {"id": 1, "text": "你好，朋友。"},
                {"id": 2, "text": "世界，你好。"},
            ]
            json_response = root / "review-response.json"
            json_response.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            apply_translation_review_fixes(job_file, json_response)
            json_job = load_job(job_file)
            json_records = {
                item.kind: item.checksum
                for item in json_job.artifacts
                if item.kind in {"translation_review_revision", "translation_reviewed_cues"}
            }
            self.assertEqual(len(json_records), 2)

            # 回到修订前的 Job 状态，同一组编辑改走人工 SRT 通道。
            job_file.write_bytes(before)
            srt_response = root / srt_template.name
            srt_response.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n#1\n你好，朋友。\n\n"
                "2\n00:00:01,000 --> 00:00:02,000\n#2\n世界，你好。\n",
                encoding="utf-8",
            )
            apply_translation_review_fixes(job_file, srt_response)
            srt_job = load_job(job_file)
            srt_records = {
                item.kind: item.checksum
                for item in srt_job.artifacts
                if item.kind in {"translation_review_revision", "translation_reviewed_cues"}
            }

            self.assertEqual(srt_records, json_records)
            reviewed = next(
                Path(item.path)
                for item in srt_job.artifacts
                if item.kind == "translation_reviewed_cues"
            )
            self.assertEqual(_load_target_text(reviewed, 1), "你好，朋友。")
            self.assertEqual(_load_target_text(reviewed, 2), "世界，你好。")

    def test_srt_revision_rejects_timeline_edits_and_cue_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job(root)
            issue = SemanticIssue("source_suspect", (1, 2), "review", "check", ())
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch("xvt.pipeline.semantic.semantic_review_batch", return_value=(issue,)),
            ):
                run_translation_semantic_review(job_file)
            template = translation_review_input_path(load_job(job_file))
            srt_name = template.with_suffix(".srt").name
            first = "1\n00:00:00,000 --> 00:00:01,000\n#1\n你好，朋友。\n\n"
            second = "2\n00:00:01,000 --> 00:00:02,000\n#2\n世界，你好。\n"
            rejected = {
                # 拉长第一条的时间轴：时间轴是唯一来源，回填时无法匹配。
                "timing_shifted": (
                    srt_name,
                    "1\n00:00:00,000 --> 00:00:01,500\n#1\n你好，朋友。\n\n" + second,
                ),
                "cue_removed": (srt_name, first),
                "cue_added": (
                    srt_name,
                    first + second + "\n3\n00:00:02,000 --> 00:00:03,000\n#3\n新增。\n",
                ),
                # 注释行与时间轴各指一条，说明人工挪动了正文而非改译文。
                "marker_mismatch": (
                    srt_name,
                    "1\n00:00:00,000 --> 00:00:01,000\n#2\n你好，朋友。\n\n" + second,
                ),
                "empty_text": (
                    srt_name,
                    "1\n00:00:00,000 --> 00:00:01,000\n#1\n\n\n" + second,
                ),
                "stale_fingerprint": (
                    f"{translation_review_stem('0' * 32)}.srt",
                    first + second,
                ),
            }
            before = job_file.read_bytes()
            for label, (name, text) in rejected.items():
                with self.subTest(label):
                    response = root / name
                    response.write_text(text, encoding="utf-8")
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

    def test_missing_candidate_qa_is_rebuilt_without_repeating_semantic_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch("xvt.pipeline.semantic.semantic_review_batch", return_value=()) as review,
            ):
                run_translation_semantic_review(job_file)

            job = load_job(job_file)
            candidate_qa = valid_artifact_path(job, "translation_candidate_qa")
            candidate_qa.unlink()
            self.assertFalse(semantic_review_approved(load_job(job_file)))

            with patch("xvt.pipeline.semantic.semantic_review_batch") as repeated_review:
                result = run_translation_semantic_review(job_file)

            self.assertEqual(result.status, "completed")
            self.assertTrue(semantic_review_satisfied(load_job(job_file)))
            self.assertIsNotNone(
                valid_artifact_path(load_job(job_file), "translation_candidate_qa")
            )
            review.assert_called_once()
            repeated_review.assert_not_called()

    def test_source_only_candidate_qa_issue_does_not_force_translation_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            job = load_job(job_file)
            source_artifact = next(item for item in job.artifacts if item.kind == "source_cues")
            source_path = Path(source_artifact.path)
            source_cues = [
                Cue(**item) for item in json.loads(source_path.read_text(encoding="utf-8"))
            ]
            source_cues[0] = replace(source_cues[0], source_text="source text " * 30)
            source_path.write_text(
                json.dumps([asdict(cue) for cue in source_cues], ensure_ascii=False),
                encoding="utf-8",
            )
            source_artifact.checksum = file_checksum(source_path)
            artifact = next(
                item for item in job.artifacts if item.kind == "translation_polished_cues"
            )
            path = Path(artifact.path)
            cues = [Cue(**item) for item in json.loads(path.read_text(encoding="utf-8"))]
            cues[0] = replace(cues[0], source_text="source text " * 30)
            path.write_text(
                json.dumps([asdict(cue) for cue in cues], ensure_ascii=False),
                encoding="utf-8",
            )
            artifact.checksum = file_checksum(path)
            save_job(job)

            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch("xvt.pipeline.semantic.semantic_review_batch", return_value=()),
            ):
                result = run_translation_semantic_review(job_file)

            job = load_job(job_file)
            candidate_qa = json.loads(
                valid_artifact_path(job, "translation_candidate_qa").read_text(encoding="utf-8")
            )
            self.assertEqual(result.status, "completed")
            self.assertTrue(
                any(
                    issue["code"] == "bilingual_overflow_risk"
                    and issue["message"].startswith("source line width ")
                    for issue in candidate_qa["issues"]
                )
            )
            self.assertIsNone(translation_review_input_path(job))

    def test_source_and_target_overflow_still_blocks_on_the_target_issue(self) -> None:
        """同一 cue 源文与译文都超宽时，源文 warning 不能吞掉译文的 review 阻断项。"""

        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            job = load_job(job_file)
            source_artifact = next(item for item in job.artifacts if item.kind == "source_cues")
            source_path = Path(source_artifact.path)
            source_cues = [
                Cue(**item) for item in json.loads(source_path.read_text(encoding="utf-8"))
            ]
            source_cues[0] = replace(source_cues[0], source_text="source text " * 30)
            source_path.write_text(
                json.dumps([asdict(cue) for cue in source_cues], ensure_ascii=False),
                encoding="utf-8",
            )
            source_artifact.checksum = file_checksum(source_path)
            artifact = next(
                item for item in job.artifacts if item.kind == "translation_polished_cues"
            )
            path = Path(artifact.path)
            cues = [Cue(**item) for item in json.loads(path.read_text(encoding="utf-8"))]
            cues[0] = replace(cues[0], source_text="source text " * 30, target_text="超长字幕" * 30)
            path.write_text(
                json.dumps([asdict(cue) for cue in cues], ensure_ascii=False),
                encoding="utf-8",
            )
            artifact.checksum = file_checksum(path)
            save_job(job)

            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch("xvt.pipeline.semantic.semantic_review_batch", return_value=()),
            ):
                result = run_translation_semantic_review(job_file)

            self.assertEqual(result.status, "awaiting_review")
            job = load_job(job_file)
            candidate_qa_path = valid_artifact_path(job, "translation_candidate_qa")
            report = json.loads(candidate_qa_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "needs_review")
            self.assertEqual(
                sorted(
                    issue["message"].split(" line width ")[0]
                    for issue in report["issues"]
                    if issue["code"] == "bilingual_overflow_risk"
                ),
                ["source", "target"],
            )
            candidate_path = next(
                Path(item.path)
                for item in job.artifacts
                if item.kind == "translation_polished_cues"
            )
            blockers = translation_candidate_qa_issues(job, candidate_path)
            self.assertEqual(
                [issue["message"].split(" line width ")[0] for issue in blockers or ()],
                ["target"],
            )

    def test_candidate_qa_is_included_in_revision_and_rerun_after_edit(self) -> None:
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
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch("xvt.pipeline.semantic.semantic_review_batch", return_value=()),
            ):
                result = run_translation_semantic_review(job_file)

            self.assertEqual(result.status, "awaiting_review")
            job = load_job(job_file)
            qa_path = valid_artifact_path(job, "translation_candidate_qa")
            qa = json.loads(qa_path.read_text(encoding="utf-8"))
            self.assertEqual(qa["status"], "needs_review")
            template = translation_review_input_path(job)
            payload = json.loads(template.read_text(encoding="utf-8"))
            self.assertEqual(payload["issues"], [])
            self.assertEqual(payload["qa_issues"][0]["code"], "bilingual_overflow_risk")
            self.assertEqual([item["id"] for item in payload["items"]], [1])
            workflow = job.config.pop("workflow")
            job.config.update(workflow)
            save_job(job)
            plan = build_recovery_plan(job_file)
            job = load_job(job_file)
            job.config["workflow"] = workflow
            save_job(job)
            self.assertIn(str(qa_path), plan.report_paths)
            self.assertNotIn(
                "acknowledge-translation-review",
                " ".join(plan.alternative_commands),
            )

            payload["items"] = [{"id": 1, "text": "简短译文。"}]
            response = Path(directory) / "review-response.json"
            response.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            apply_translation_review_fixes(job_file, response)
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch("xvt.pipeline.semantic.semantic_review_batch", return_value=()),
            ):
                completed = run_translation_semantic_review(job_file)

            self.assertEqual(completed.status, "completed")
            self.assertFalse(
                any(
                    issue["severity"] in {"error", "review"}
                    for issue in json.loads(
                        valid_artifact_path(
                            load_job(job_file), "translation_candidate_qa"
                        ).read_text(encoding="utf-8")
                    )["issues"]
                )
            )

    def test_unchanged_revision_reports_pending_qa_blocked_cues(self) -> None:
        """QA 阻断项还在、却提交了零改动：报错要点名仍需缩短的 cue 与原因。"""

        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            job = load_job(job_file)
            artifact = next(
                item for item in job.artifacts if item.kind == "translation_polished_cues"
            )
            path = Path(artifact.path)
            cues = [Cue(**item) for item in json.loads(path.read_text(encoding="utf-8"))]
            overlong = "超长字幕" * 30
            cues[0] = replace(cues[0], target_text=overlong)
            path.write_text(
                json.dumps([asdict(cue) for cue in cues], ensure_ascii=False),
                encoding="utf-8",
            )
            artifact.checksum = file_checksum(path)
            save_job(job)
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch("xvt.pipeline.semantic.semantic_review_batch", return_value=()),
            ):
                result = run_translation_semantic_review(job_file)

            self.assertEqual(result.status, "awaiting_review")
            template = translation_review_input_path(load_job(job_file))
            payload = json.loads(template.read_text(encoding="utf-8"))
            payload["items"] = [{"id": 1, "text": overlong}]
            response = Path(directory) / "review-response.json"
            response.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            with self.assertRaises(UserInputError) as caught:
                apply_translation_review_fixes(job_file, response)

            message = str(caught.exception)
            self.assertIn("does not change any cue text", message)
            self.assertIn("QA-blocked", message)
            self.assertIn("1 (target line width ", message)

    def test_unchanged_revision_without_qa_blockers_keeps_the_plain_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job(root)
            issue = SemanticIssue("source_suspect", (1,), "review", "awkward", ())
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch("xvt.pipeline.semantic.semantic_review_batch", return_value=(issue,)),
            ):
                result = run_translation_semantic_review(job_file)

            self.assertEqual(result.status, "awaiting_review")
            template = translation_review_input_path(load_job(job_file))
            payload = json.loads(template.read_text(encoding="utf-8"))
            self.assertEqual(payload["qa_issues"], [])
            payload["items"] = [{"id": 1, "text": "你好。"}]
            response = root / "review-response.json"
            response.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            with self.assertRaises(UserInputError) as caught:
                apply_translation_review_fixes(job_file, response)

            self.assertEqual(
                str(caught.exception),
                "translation review response does not change any cue text",
            )

    def test_missing_semantic_response_file_is_a_user_input_error(self) -> None:
        """等待态正确、只是 response 路径打错：属于用户输入错误（2），不是依赖故障（3）。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job(root, provider="agent")
            prepare_agent_semantic_review(job_file)

            with self.assertRaisesRegex(
                UserInputError, "cannot read semantic review response"
            ) as caught:
                apply_agent_semantic_review(job_file, root / "gone" / "semantic-response.json")

            self.assertEqual(caught.exception.exit_code, ExitCode.USER_ERROR)

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
                        "repair_results": [{"batch": 1, "items": [{"id": 1, "text": "您好，"}]}],
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
            self.assertEqual(repaired[0]["target_text"], "您好")

    def test_manifest_rebuilt_after_apply_crash_keeps_the_same_fingerprint(self) -> None:
        """apply 原地覆盖 manifest 后崩溃，仍有可恢复路径（A6 决策留痕）。

        恢复依赖 manifest fingerprint 的确定性：``continue`` 重建的 manifest 必须与
        崩溃前逐位相同，同一份 Agent 响应才能原样重放。若未来 payload 引入非确定性
        字段，本测试先红，而不是让可恢复性悄悄失效。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job(root, provider="agent")
            manifest_path = prepare_agent_semantic_review(job_file)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            original_fingerprint = manifest["fingerprint"]
            response = root / "semantic-response.json"
            response.write_text(
                json.dumps(
                    {
                        "fingerprint": original_fingerprint,
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
                        "repair_results": [{"batch": 1, "items": [{"id": 1, "text": "您好，"}]}],
                        "final_results": [{"batch": manifest["batches"][0]["id"], "issues": []}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            # 崩溃窗口：manifest 已被就地覆盖为 report，repair checkpoint 尚未落盘。
            with patch(
                "xvt.pipeline.semantic._write_repair_checkpoint",
                side_effect=RuntimeError("crash after manifest overwrite"),
            ):
                with self.assertRaises(RuntimeError):
                    apply_agent_semantic_review(job_file, response)

            self.assertFalse(agent_semantic_review_waiting(load_job(job_file)))
            with self.assertRaises(ValueError):
                apply_agent_semantic_review(job_file, response)

            plan = build_recovery_plan(job_file)
            self.assertEqual(plan.category, "retryable")

            # continue 内部路径：重建 manifest。
            rerun = run_translation_semantic_review(job_file)

            self.assertEqual(rerun.status, "awaiting_agent")
            rebuilt = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(rebuilt["fingerprint"], original_fingerprint)
            self.assertEqual(rebuilt["batches"], manifest["batches"])
            self.assertTrue(agent_semantic_review_waiting(load_job(job_file)))

            # 同一份响应原样重放成功。
            final = apply_agent_semantic_review(job_file, response)

            self.assertEqual(final.status, "completed")
            self.assertTrue(semantic_review_satisfied(load_job(job_file)))

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
            job = load_job(job_file)
            self.assertTrue(semantic_review_waiting(job))
            self.assertIsNotNone(source_review_input_path(job, [1]))

    def test_agent_manifest_is_bound_to_current_contract_versions(self) -> None:
        for field, value in (
            ("task_version", 999),
            ("review_prompt_version", 2),
            ("repair_prompt_version", 1),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                job_file = self._job(root, provider="agent")
                manifest_path = prepare_agent_semantic_review(job_file)
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest[field] = value
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

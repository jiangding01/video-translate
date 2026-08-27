import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xvt.job_store import create_job, save_job
from xvt.models import Artifact, StageName, StageStatus
from xvt.recovery import (
    _semantic_review_options,
    build_recovery_plan,
    file_checksum,
    subtitle_qa_failure_guidance,
    valid_artifact_path,
    verify_job,
)


class RecoveryTest(unittest.TestCase):
    def test_semantic_review_recovery_explains_revision_cost(self) -> None:
        job = create_job("/tmp/source.srt", Path("/tmp"), {"input_kind": "subtitle"})
        reason, _primary, _alternatives, _inputs = _semantic_review_options(
            job,
            Path("/tmp/job.json"),
            acknowledgment_command="xvt continue /tmp/job.json",
        )

        self.assertIn("不会重新翻译或润色", reason)

    def test_recovery_plan_resumes_subtitle_job_from_imported_subtitles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/source.srt", root, {"input_kind": "subtitle"})
            artifact_path = root / "input-subtitles.srt"
            artifact_path.write_text("subtitle", encoding="utf-8")
            job.artifacts.append(
                Artifact("input_subtitles", str(artifact_path), file_checksum(artifact_path))
            )
            job_file = save_job(job)

            plan = build_recovery_plan(job_file)

        self.assertEqual(plan.category, "retryable")
        self.assertIn("resume", plan.primary_command or "")

    def _add_artifact(self, job, root: Path, kind: str, content: str = "{}") -> Path:
        path = root / f"{kind}.json"
        path.write_text(content, encoding="utf-8")
        job.artifacts.append(Artifact(kind, str(path), file_checksum(path)))
        return path

    def test_verify_job_accepts_valid_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            artifact_path = root / "artifact.json"
            artifact_path.write_text("{}", encoding="utf-8")
            job.artifacts.append(
                Artifact("example", str(artifact_path), file_checksum(artifact_path))
            )
            job_file = save_job(job)

            report = verify_job(job_file)

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["failed_count"], 0)
        self.assertEqual(report["artifacts"][0]["status"], "pass")

    def test_verify_job_reports_changed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            artifact_path = root / "artifact.json"
            artifact_path.write_text("before", encoding="utf-8")
            job.artifacts.append(
                Artifact("example", str(artifact_path), file_checksum(artifact_path))
            )
            job_file = save_job(job)
            artifact_path.write_text("after", encoding="utf-8")

            report = verify_job(job_file)

        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["artifacts"][0]["message"], "artifact checksum mismatch")
        self.assertIsNone(valid_artifact_path(job, "example"))

    def test_verify_job_reports_missing_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            job.artifacts.append(Artifact("missing", str(root / "missing.json"), "abc"))
            job_file = save_job(job)

            report = verify_job(job_file)

        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["artifacts"][0]["message"], "artifact file is missing")

    def test_recovery_plan_uses_checksum_valid_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            job.config["workflow"] = {"target_language": "zh-CN", "burn": False}
            source = self._add_artifact(job, root, "source_cues", "before")
            job.stages[StageName.TRANSLATE].status = StageStatus.AWAITING_INPUT
            job_file = save_job(job)
            source.write_text("changed", encoding="utf-8")

            plan = build_recovery_plan(job_file)

        self.assertEqual(plan.category, "unrecoverable")
        self.assertNotIn("source_cues", plan.reusable_artifacts)
        self.assertIsNone(plan.primary_command)

    def test_recovery_plan_blocks_continue_after_qa_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            job.config["workflow"] = {"target_language": "zh-CN", "burn": True}
            self._add_artifact(job, root, "source_cues")
            report = self._add_artifact(
                job,
                root,
                "subtitle_qa",
                content=(
                    '{"status": "failed", "issues": '
                    '[{"code": "cue_overlap", "severity": "error", "cue_id": 1}]}'
                ),
            )
            job.stages[StageName.SUBTITLE_QA].status = StageStatus.FAILED
            job_file = save_job(job)

            plan = build_recovery_plan(job_file)

        self.assertEqual(plan.category, "manual_fix")
        self.assertIsNone(plan.primary_command)
        self.assertIn("修复字幕", plan.reason)
        self.assertEqual(plan.report_paths, (str(report),))

    def test_source_qa_error_recommends_rebuild_source_when_media_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            job = create_job(str(media), root)
            self._add_artifact(job, root, "source_cues")
            self._add_artifact(
                job,
                root,
                "subtitle_qa",
                '{"status":"failed","issues":[{"severity":"error"}]}',
            )
            job.stages[StageName.SUBTITLE_QA].status = StageStatus.FAILED
            plan = build_recovery_plan(save_job(job))

        self.assertEqual(plan.category, "manual_fix")
        self.assertIn("rebuild-source", plan.primary_command or "")
        self.assertIn("--strategy asr", plan.primary_command or "")

    def test_asr_source_qa_error_does_not_recommend_identical_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            job = create_job(str(media), root, {"source_strategy": "asr"})
            source = self._add_artifact(job, root, "source_cues")
            self._add_artifact(
                job,
                root,
                "source_selection",
                json.dumps(
                    {
                        "selected_kind": "asr",
                        "source_cues_checksum": file_checksum(source),
                    }
                ),
            )
            self._add_artifact(
                job,
                root,
                "subtitle_qa",
                '{"status":"failed","issues":[{"severity":"error"}]}',
            )
            job.stages[StageName.SUBTITLE_QA].status = StageStatus.FAILED

            plan = build_recovery_plan(save_job(job))

        self.assertEqual(plan.category, "manual_fix")
        self.assertNotIn("rebuild-source", plan.primary_command or "")

    def test_interrupted_source_qa_resumes_from_existing_cues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root, {"workflow": {}})
            self._add_artifact(job, root, "source_cues")
            job.stages[StageName.SUBTITLE_QA].status = StageStatus.FAILED

            plan = build_recovery_plan(save_job(job))

        self.assertEqual(plan.category, "retryable")
        self.assertIn("continue", plan.primary_command or "")

    def test_failed_atomic_remote_rebuild_can_be_retried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job(
                "https://youtu.be/example",
                Path(directory),
                {"source_strategy": "asr", "source_rebuild_requested": True},
            )

            plan = build_recovery_plan(save_job(job))

        self.assertEqual(plan.category, "manual_fix")
        self.assertIn("rebuild-source", plan.primary_command or "")

    def test_initial_atomic_asr_failure_is_not_misclassified_as_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job(
                "https://youtu.be/example",
                Path(directory),
                {"source_strategy": "asr"},
            )

            plan = build_recovery_plan(save_job(job))

        self.assertEqual(plan.category, "unrecoverable")
        self.assertNotIn("rebuild-source", plan.primary_command or "")

    def test_failed_workflow_rebuild_stays_explicit_for_batch_safety(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job(
                "https://youtu.be/example",
                Path(directory),
                {
                    "source_strategy": "asr",
                    "source_rebuild_requested": True,
                    "workflow": {"target_language": "zh-CN"},
                },
            )

            plan = build_recovery_plan(save_job(job))

        self.assertEqual(plan.category, "manual_fix")
        self.assertIn("rebuild-source", plan.primary_command or "")
        self.assertNotIn("continue", plan.primary_command or "")

    def test_qa_failures_not_eligible_for_rebuild_remain_manual_fix(self) -> None:
        cases = (
            ({"input_kind": "subtitle"}, '{"status":"failed","issues":[{"severity":"error"}]}'),
            ({}, '{"status":"needs_review","issues":[{"severity":"review"}]}'),
            ({}, '{"status":"failed","issues":[{"severity":"error"}]}'),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, (config, source_report) in enumerate(cases):
                job = create_job(f"/tmp/input-{index}.mp4", root / str(index), config)
                self._add_artifact(job, Path(job.output_root), "source_cues")
                self._add_artifact(job, Path(job.output_root), "subtitle_qa", source_report)
                if index == 2:
                    self._add_artifact(
                        job,
                        Path(job.output_root),
                        "translated_subtitle_qa",
                        '{"status":"failed","issues":[{"severity":"error"}]}',
                    )
                job.stages[StageName.SUBTITLE_QA].status = StageStatus.FAILED
                plan = build_recovery_plan(save_job(job))
                self.assertEqual(plan.category, "manual_fix")
                self.assertNotIn("rebuild-source", plan.primary_command or "")

    def test_local_burn_media_does_not_make_missing_source_rebuildable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job(str(root / "missing-audio.wav"), root, {})
            self._add_artifact(job, root, "source_video")
            self._add_artifact(job, root, "source_cues")
            self._add_artifact(
                job,
                root,
                "subtitle_qa",
                '{"status":"failed","issues":[{"severity":"error"}]}',
            )
            job.stages[StageName.SUBTITLE_QA].status = StageStatus.FAILED

            plan = build_recovery_plan(save_job(job))

        self.assertEqual(plan.category, "manual_fix")
        self.assertNotIn("rebuild-source", plan.primary_command or "")

    def test_recovery_plan_guides_acknowledge_for_needs_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            job.config["workflow"] = {"target_language": "zh-CN", "burn": True}
            self._add_artifact(job, root, "source_cues")
            report = self._add_artifact(
                job,
                root,
                "subtitle_qa",
                content=(
                    '{"status": "needs_review", "issues": '
                    '[{"code": "line_too_wide", "severity": "review", "cue_id": 1}]}'
                ),
            )
            job.stages[StageName.SUBTITLE_QA].status = StageStatus.FAILED
            job_file = save_job(job)

            plan = build_recovery_plan(job_file)

        self.assertEqual(plan.category, "manual_fix")
        self.assertIn("needs_review", plan.reason)
        self.assertIn("人工确认", plan.reason)
        self.assertIsNotNone(plan.primary_command)
        self.assertIn("--acknowledge-subtitle-review", plan.primary_command or "")
        self.assertIn("continue", plan.primary_command or "")
        self.assertEqual(plan.report_paths, (str(report),))

    def test_missing_translated_qa_report_is_rebuilt_from_existing_cues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            job.config["workflow"] = {"target_language": "zh-CN"}
            self._add_artifact(job, root, "source_cues")
            self._add_artifact(job, root, "subtitle_qa", '{"status":"pass","issues":[]}')
            self._add_artifact(job, root, "translated_cues")
            translated_report = self._add_artifact(
                job,
                root,
                "translated_subtitle_qa",
                '{"status":"needs_review","issues":[{"severity":"review"}]}',
            )
            job.stages[StageName.SUBTITLE_QA].status = StageStatus.AWAITING_INPUT
            job_file = save_job(job)
            translated_report.unlink()

            plan = build_recovery_plan(job_file)
            guidance = subtitle_qa_failure_guidance(job, job_file)

        self.assertEqual(plan.category, "retryable")
        self.assertIn("译文字幕 QA 报告", plan.reason)
        self.assertIn("continue", plan.primary_command or "")
        self.assertIsNone(guidance)

    def test_missing_translated_qa_does_not_hide_source_qa_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root, {"input_kind": "subtitle"})
            job.config["workflow"] = {"target_language": "zh-CN"}
            self._add_artifact(job, root, "source_cues")
            self._add_artifact(
                job,
                root,
                "subtitle_qa",
                '{"status":"failed","issues":[{"severity":"error"}]}',
            )
            self._add_artifact(job, root, "translated_cues")
            translated_report = self._add_artifact(
                job,
                root,
                "translated_subtitle_qa",
                '{"status":"needs_review","issues":[{"severity":"review"}]}',
            )
            job.stages[StageName.SUBTITLE_QA].status = StageStatus.AWAITING_INPUT
            job_file = save_job(job)
            translated_report.unlink()

            plan = build_recovery_plan(job_file)

        self.assertEqual(plan.category, "manual_fix")
        self.assertIn("修复字幕", plan.reason)

    def test_recovery_plan_retries_burn_after_media_qa_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            job.config["workflow"] = {"target_language": "zh-CN", "burn": True}
            self._add_artifact(job, root, "source_cues")
            self._add_artifact(job, root, "translated_cues")
            self._add_artifact(job, root, "translated_srt", content="1\n")
            self._add_artifact(job, root, "bilingual_srt", content="1\n")
            self._add_artifact(
                job,
                root,
                "translated_subtitle_qa",
                content='{"status":"pass","issues":[]}',
            )
            self._add_artifact(job, root, "burned_video")
            job.stages[StageName.MEDIA_QA].status = StageStatus.FAILED
            job_file = save_job(job)

            plan = build_recovery_plan(job_file)

        self.assertEqual(plan.category, "retryable")
        self.assertIn("continue", plan.primary_command or "")
        self.assertIn("媒体 QA", plan.reason)
        self.assertEqual(plan.advance_steps, ("burn",))

    def test_recovery_plan_retries_burn_when_artifact_missing_after_media_qa_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            job.config["workflow"] = {"target_language": "zh-CN", "burn": True}
            self._add_artifact(job, root, "source_cues")
            self._add_artifact(job, root, "translated_cues")
            self._add_artifact(job, root, "translated_srt", content="1\n")
            self._add_artifact(job, root, "bilingual_srt", content="1\n")
            self._add_artifact(
                job,
                root,
                "translated_subtitle_qa",
                content='{"status":"pass","issues":[]}',
            )
            job.stages[StageName.MEDIA_QA].status = StageStatus.SUCCEEDED
            job_file = save_job(job)

            plan = build_recovery_plan(job_file)

        self.assertEqual(plan.category, "retryable")
        self.assertIn("continue", plan.primary_command or "")
        self.assertIn("媒体 QA 已通过", plan.reason)
        self.assertIn("成品", plan.reason)
        self.assertNotIn("指纹", plan.reason)
        self.assertEqual(plan.advance_steps, ("burn",))

    def test_recovery_plan_retries_burn_when_fingerprint_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            job.config["workflow"] = {
                "target_language": "zh-CN",
                "burn": True,
                "burn_fingerprint": "stale-fingerprint",
            }
            self._add_artifact(job, root, "source_cues")
            self._add_artifact(job, root, "translated_cues")
            self._add_artifact(job, root, "translated_srt", content="1\n")
            self._add_artifact(job, root, "bilingual_srt", content="1\n")
            self._add_artifact(
                job,
                root,
                "translated_subtitle_qa",
                content='{"status":"pass","issues":[]}',
            )
            self._add_artifact(job, root, "burned_video")
            job.stages[StageName.MEDIA_QA].status = StageStatus.SUCCEEDED
            job_file = save_job(job)

            plan = build_recovery_plan(job_file)

        self.assertEqual(plan.category, "retryable")
        self.assertIn("continue", plan.primary_command or "")
        self.assertIn("指纹", plan.reason)
        self.assertEqual(plan.advance_steps, ("burn",))

    def test_recovery_plan_render_step_when_translation_cues_lack_srt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            job.config["workflow"] = {"target_language": "zh-CN", "burn": False}
            self._add_artifact(job, root, "source_cues")
            self._add_artifact(job, root, "translated_cues")
            job_file = save_job(job)

            plan = build_recovery_plan(job_file)

        self.assertEqual(plan.category, "retryable")
        self.assertEqual(plan.advance_steps, ("render",))
        self.assertIn("渲染", plan.reason)
        self.assertIn("continue", plan.primary_command or "")

    def test_recovery_plan_render_step_when_translated_qa_report_is_invalid(self) -> None:
        """译文 QA 报告单独失效必须归为 retryable + continue，而不是「已完成」。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            job.config["workflow"] = {"target_language": "zh-CN", "burn": False}
            self._add_artifact(job, root, "source_cues")
            self._add_artifact(job, root, "translated_cues")
            self._add_artifact(job, root, "translated_srt", content="1\n")
            self._add_artifact(job, root, "bilingual_srt", content="1\n")
            qa_report = self._add_artifact(
                job,
                root,
                "translated_subtitle_qa",
                content='{"status":"pass","issues":[]}',
            )
            job_file = save_job(job)
            qa_report.write_text("{}", encoding="utf-8")

            plan = build_recovery_plan(job_file)

        self.assertEqual(plan.category, "retryable")
        self.assertEqual(plan.advance_steps, ("render",))
        self.assertIn("渲染", plan.reason)
        self.assertIn("continue", plan.primary_command or "")

    def test_recovery_plan_keeps_relink_navigation_when_translated_qa_is_invalid(self) -> None:
        """QA 报告失效让计划变成 ("render", "burn") 时，relink 导航不能丢。

        丢了导航用户就会被引导做一次完整重编码，而成品本可零成本重新关联。
        """

        from xvt.workflow import _record_burn_fingerprint

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "video.mp4"
            media.write_bytes(b"media")
            burned = root / "burned.mp4"
            burned.write_bytes(b"burned-output")
            job = create_job(str(media), root, {})
            job.config["workflow"] = {"target_language": "zh-CN", "burn": True}
            self._add_artifact(job, root, "source_cues")
            self._add_artifact(job, root, "translated_cues")
            self._add_artifact(job, root, "translated_srt", content="1\n")
            self._add_artifact(job, root, "bilingual_srt", content="1\n")
            self._add_artifact(job, root, "media_qa", content='{"status":"pass","issues":[]}')
            qa_report = self._add_artifact(
                job,
                root,
                "translated_subtitle_qa",
                content='{"status":"pass","issues":[]}',
            )
            job.artifacts.append(Artifact("burned_video", str(burned), file_checksum(burned)))
            job.stages[StageName.MEDIA_QA].status = StageStatus.SUCCEEDED
            job_file = save_job(job)
            _record_burn_fingerprint(job_file)
            # 成品被移走 + QA 报告失效：两处同时失效才会命中 ("render", "burn")。
            burned.unlink()
            qa_report.write_text("{}", encoding="utf-8")

            plan = build_recovery_plan(job_file)

        self.assertEqual(plan.advance_steps, ("render", "burn"))
        self.assertEqual(plan.category, "awaiting_input")
        self.assertIn("relink-burned-output", plan.primary_command or "")
        self.assertIn("continue", plan.alternative_commands[0])

    def test_recovery_plan_describes_agent_translation_prerequisite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            job.config["workflow"] = {"target_language": "zh-CN", "burn": False}
            self._add_artifact(job, root, "source_cues")
            job.stages[StageName.TRANSLATE].status = StageStatus.AWAITING_INPUT
            job_file = save_job(job)

            plan = build_recovery_plan(job_file)

        self.assertEqual(plan.category, "awaiting_input")
        self.assertIn("xvt apply ", plan.prerequisite_commands[0])
        self.assertIn("apply-translation", plan.alternative_commands[0])
        self.assertIn("continue", plan.primary_command or "")
        self.assertEqual(plan.advance_steps, ("await_agent_translation",))

    def test_recovery_plan_describes_agent_polish_prerequisite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            job.config["workflow"] = {
                "target_language": "zh-CN",
                "translation_quality": "polished",
            }
            self._add_artifact(job, root, "source_cues")
            self._add_artifact(job, root, "translation_draft_cues")
            self._add_artifact(job, root, "translation_polish_batches")
            job.stages[StageName.TRANSLATE].status = StageStatus.AWAITING_INPUT
            job_file = save_job(job)

            with patch("xvt.workflow.agent_translation_polish_manifest_valid", return_value=True):
                plan = build_recovery_plan(job_file)

        self.assertEqual(plan.category, "awaiting_input")
        self.assertIn("xvt apply ", plan.prerequisite_commands[0])
        self.assertIn("apply-polish-translation", plan.alternative_commands[0])
        self.assertEqual(plan.advance_steps, ("await_agent_polish",))

    def test_recovery_plan_blocks_when_polished_draft_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            job.config["workflow"] = {
                "target_language": "zh-CN",
                "translation_quality": "polished",
            }
            self._add_artifact(job, root, "source_cues")
            draft = self._add_artifact(job, root, "translation_draft_cues")
            self._add_artifact(job, root, "translation_polish_batches")
            job.stages[StageName.TRANSLATE].status = StageStatus.AWAITING_INPUT
            draft.write_text("corrupt", encoding="utf-8")
            job_file = save_job(job)

            plan = build_recovery_plan(job_file)

        self.assertEqual(plan.category, "manual_fix")
        self.assertIsNone(plan.primary_command)
        self.assertEqual(plan.advance_steps, ("repair_polish_draft",))

    def test_recovery_plan_workflow_translate_exposes_advance_steps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            job.config["workflow"] = {"target_language": "zh-CN", "burn": True}
            self._add_artifact(job, root, "source_cues")
            job_file = save_job(job)

            plan = build_recovery_plan(job_file)

        self.assertEqual(plan.category, "retryable")
        self.assertEqual(plan.advance_steps, ("translate", "burn"))
        self.assertIn("continue", plan.primary_command or "")
        self.assertIn("翻译", plan.reason)

    def test_recovery_plan_resumes_from_valid_transcription_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            self._add_artifact(job, root, "media_info")
            self._add_artifact(job, root, "normalized_audio")
            job_file = save_job(job)

            plan = build_recovery_plan(job_file)

        self.assertEqual(plan.category, "retryable")
        self.assertIn("resume", plan.primary_command or "")

    def test_recovery_plan_retries_early_remote_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job(
                "https://youtu.be/abc",
                directory,
                {"workflow": {"target_language": "zh-CN", "burn": True}},
            )
            job.stages[StageName.ACQUIRE].status = StageStatus.FAILED
            job_file = save_job(job)

            plan = build_recovery_plan(job_file)

        self.assertEqual(plan.category, "retryable")
        self.assertIn("continue", plan.primary_command or "")

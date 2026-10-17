import json
import os
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xvt.job_store import create_job, load_job, save_job
from xvt.models import Artifact, StageName, StageStatus
from xvt.pipeline import retranslate_cues
from xvt.recovery import (
    _candidate_qa_blocker_reason,
    _semantic_review_options,
    atomic_translated_qa_rebuild_command,
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

    def test_polished_relink_navigation_requires_the_candidate_to_be_final(self) -> None:
        """polished 的 render 可能改写 translated_cues：那时 relink 会白做。

        语义校对已 approved 但 finalize 中断（translated_cues 还不是最终候选）时，
        continue 走 ``_finalize_candidate`` 会重写 translated_cues，烧录派生指纹随即失效，
        重新关联的成品必然被重烧录取代——只能引导 continue，不能引导 relink。
        """

        from xvt.workflow import _record_burn_fingerprint

        for satisfied, expected_category in ((False, "retryable"), (True, "awaiting_input")):
            with self.subTest(semantic_review_satisfied=satisfied):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    media = root / "video.mp4"
                    media.write_bytes(b"media")
                    burned = root / "burned.mp4"
                    burned.write_bytes(b"burned-output")
                    job = create_job(str(media), root, {})
                    job.config["workflow"] = {
                        "target_language": "zh-CN",
                        "burn": True,
                        "translation_quality": "polished",
                    }
                    self._add_artifact(job, root, "source_cues")
                    self._add_artifact(job, root, "translated_cues")
                    self._add_artifact(job, root, "translated_srt", content="1\n")
                    self._add_artifact(job, root, "bilingual_srt", content="1\n")
                    self._add_artifact(
                        job, root, "media_qa", content='{"status":"pass","issues":[]}'
                    )
                    qa_report = self._add_artifact(
                        job,
                        root,
                        "translated_subtitle_qa",
                        content='{"status":"pass","issues":[]}',
                    )
                    job.artifacts.append(
                        Artifact("burned_video", str(burned), file_checksum(burned))
                    )
                    job.stages[StageName.MEDIA_QA].status = StageStatus.SUCCEEDED
                    job_file = save_job(job)
                    _record_burn_fingerprint(job_file)
                    burned.unlink()
                    qa_report.write_text("{}", encoding="utf-8")

                    with (
                        patch("xvt.workflow.translation_polish_satisfied", return_value=True),
                        patch("xvt.workflow.agent_semantic_review_waiting", return_value=False),
                        patch("xvt.workflow.semantic_review_waiting", return_value=False),
                        patch("xvt.workflow.semantic_review_approved", return_value=True),
                        patch(
                            "xvt.pipeline.semantic.semantic_review_satisfied",
                            return_value=satisfied,
                        ),
                    ):
                        plan = build_recovery_plan(job_file)

                self.assertEqual(plan.advance_steps, ("render", "burn"))
                self.assertEqual(plan.category, expected_category)
                if satisfied:
                    self.assertIn("relink-burned-output", plan.primary_command or "")
                else:
                    self.assertNotIn("relink-burned-output", plan.primary_command or "")
                    self.assertIn("continue", plan.primary_command or "")

    def test_recovery_plan_describes_agent_translation_prerequisite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            job.config["workflow"] = {"target_language": "zh-CN", "burn": False}
            self._add_artifact(job, root, "source_cues")
            # 等待引导的前提是批次 manifest 有效；manifest 失效时按 F3 应给出可推进的
            # translate，而不是描述一次没有任何响应能回填的等待。
            self._add_artifact(job, root, "translation_batches")
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

    def test_checksum_has_a_single_implementation(self) -> None:
        """防止未来又抄一份分块 SHA-256：所有口径都必须落到 xvt.checksums。"""

        from xvt import candidate_qa, checksums
        from xvt.pipeline import common

        self.assertIs(common.checksum, checksums.file_checksum)
        self.assertIs(candidate_qa.file_checksum, checksums.file_checksum)
        # recovery.file_checksum 是只读作用域记忆化包装，不是同一个对象，
        # 但无作用域时必须与纯哈希逐字一致。
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "payload.bin"
            path.write_bytes(b"xvt" * 4096)
            self.assertEqual(file_checksum(path), checksums.file_checksum(path))

    def test_missing_registered_artifact_is_not_a_user_input_error(self) -> None:
        """边界守卫：用户敲错 job_file 路径是 2，已登记产物被删是 Job 状态问题，不是 2。

        `load_job` 对缺失的 job_file 抛 UserInputError（P2a）。这条测试把边界钉住，
        防止后来者顺手把「登记的 artifact 文件不见了」也改成用户输入错误——
        路径的责任归属只有入口知道，登记产物的路径来源是 Job 自己。
        """

        from xvt.errors import UserInputError
        from xvt.pipeline.common import artifact_path, load_cues

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cues = root / "source-cues.json"
            cues.write_text("[]", encoding="utf-8")
            job = create_job("/tmp/source.mp4", root, {"workflow": {"target_language": "zh-CN"}})
            job.artifacts.append(
                Artifact(kind="source_cues", path=str(cues), checksum=file_checksum(cues))
            )
            job_file = save_job(job)
            cues.unlink()

            loaded = load_job(job_file)
            path = artifact_path(loaded, "source_cues")
            with self.assertRaises(FileNotFoundError) as caught:
                load_cues(path)

            self.assertNotIsInstance(caught.exception, UserInputError)

    def _burned_complete_job(self, root: Path) -> Path:
        """一个「烧录已完成」的端到端 Job：判定链会反复触碰同几个文件。"""

        from xvt.workflow import _record_burn_fingerprint

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
        self._add_artifact(
            job,
            root,
            "translated_subtitle_qa",
            content='{"status":"pass","issues":[]}',
        )
        job.artifacts.append(Artifact("burned_video", str(burned), file_checksum(burned)))
        job.stages[StageName.MEDIA_QA].status = StageStatus.SUCCEEDED
        job_file = save_job(job)
        _record_burn_fingerprint(job_file)
        return job_file

    def test_build_recovery_plan_hashes_each_file_once(self) -> None:
        """同一次判定内重复哈希同一个文件是纯浪费，且会让计划自相矛盾。"""

        from xvt import recovery

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._burned_complete_job(root)

            calls: list[str] = []
            real_hash = recovery._hash_file

            def counting_hash(path: Path) -> str:
                calls.append(str(path))
                return real_hash(path)

            with patch.object(recovery, "_hash_file", counting_hash):
                plan = build_recovery_plan(job_file)
                memoized_calls = list(calls)
                calls.clear()
                # 同一个入口，去掉记忆化包装后重新算一遍。
                bare_plan = recovery._build_recovery_plan(job_file)
                bare_calls = list(calls)

        self.assertEqual(plan, bare_plan)
        self.assertEqual(len(memoized_calls), len(set(memoized_calls)))
        self.assertLess(len(memoized_calls), len(bare_calls))

    def test_readonly_checksum_scope_does_not_leak(self) -> None:
        from xvt import recovery

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "payload.bin"
            path.write_bytes(b"first")
            calls: list[str] = []
            real_hash = recovery._hash_file

            def counting_hash(target: Path) -> str:
                calls.append(str(target))
                return real_hash(target)

            with patch.object(recovery, "_hash_file", counting_hash):
                with recovery.readonly_checksum_scope():
                    recovery.file_checksum(path)
                    recovery.file_checksum(path)
                    self.assertEqual(len(calls), 1)
                    with recovery.readonly_checksum_scope():
                        # 嵌套作用域建新字典，不复用外层结果。
                        recovery.file_checksum(path)
                        self.assertEqual(len(calls), 2)
                # 作用域退出后不再记忆化。
                recovery.file_checksum(path)
                recovery.file_checksum(path)

        self.assertEqual(len(calls), 4)

    def test_checksum_cache_is_keyed_on_stat(self) -> None:
        """size 变化会换掉 cache key，因此改写后能拿到新哈希。

        注意这条只覆盖「stat 变了」的一半：等长且 mtime_ns 被恢复的另一半见
        ``test_checksum_cache_reuses_the_hash_when_stat_is_restored``。
        """

        from xvt import recovery

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "payload.bin"
            path.write_bytes(b"first")
            with recovery.readonly_checksum_scope():
                before = recovery.file_checksum(path)
                path.write_bytes(b"second-and-longer")
                os.utime(path, ns=(0, 0))
                after = recovery.file_checksum(path)

        self.assertNotEqual(before, after)

    def test_checksum_cache_reuses_the_hash_when_stat_is_restored(self) -> None:
        """有意的边界，不是缺陷：作用域内等长覆盖 + 还原 mtime_ns 会复用旧哈希。

        缓存 key 是 ``(path, st_dev, st_ino, st_size, st_mtime_ns)``，命中即复用，
        所以「同一次只读判定内把文件改成等长内容并把 mtime_ns 改回原值」这个毫秒级
        窗口里读到的是旧哈希。这与 git 的 racy-timestamp 取舍同类。``build_recovery_plan``
        本身只读，但判定结果会经 ``batch._record_recovery`` 进入 ``batch.json`` 的条目
        终态，因此误判并非完全不落地；被接受的理由是：命中它要求外部写入者在 xvt 持有
        job 锁期间正好插进一次调用内首次哈希之后的亚毫秒缝隙，同样的坏状态在无缓存时
        也能由「计划返回后、落盘前」这个更宽的 TOCTOU 窗口达成（缓存不是根因），而
        ``xvt verify JOB`` 在作用域外重新哈希必然识破。

        这条测试把该行为锁进回归网：如果哪天有人往 key 里加内容摘要，「一次调用只
        哈希一次」的记忆化前提就没了（同一个文件会被哈希两次），必须连同
        ``readonly_checksum_scope`` 的 docstring 一起重新论证，而不是顺手改绿。
        """

        from xvt import recovery

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "payload.bin"
            path.write_bytes(b"first")
            original = path.stat()
            with recovery.readonly_checksum_scope():
                before = recovery.file_checksum(path)
                # 等长覆盖：st_size 不变。
                path.write_bytes(b"secnd")
                os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
                self.assertEqual(path.stat().st_size, original.st_size)
                self.assertEqual(path.stat().st_mtime_ns, original.st_mtime_ns)
                cached = recovery.file_checksum(path)
            # 离开作用域后不再记忆化，能读到真实内容的哈希。
            fresh = recovery.file_checksum(path)

        self.assertEqual(cached, before)
        self.assertNotEqual(fresh, before)

    def test_build_recovery_plan_does_not_touch_artifacts(self) -> None:
        """记忆化的前提是判定链只读：任何一处偷偷写产物都会在这里炸掉。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._burned_complete_job(root)

            def snapshot() -> dict[str, tuple[int, int]]:
                return {
                    str(path): (path.stat().st_mtime_ns, path.stat().st_size)
                    for path in sorted(root.rglob("*"))
                    if path.is_file()
                }

            before_files = snapshot()
            before_job = job_file.read_text(encoding="utf-8")

            build_recovery_plan(job_file)

            self.assertEqual(snapshot(), before_files)
            self.assertEqual(job_file.read_text(encoding="utf-8"), before_job)


class CueRetranslationRecoveryTest(unittest.TestCase):
    """局部重译等待在端到端与原子两条恢复分支上都要给出可复制的回填命令。"""

    def _waiting_job_file(
        self,
        root: Path,
        *,
        workflow: dict | None,
        glossary_qa_blocked: bool = False,
    ) -> Path:
        config = {"workflow": workflow} if workflow is not None else {}
        job = create_job("/tmp/input.mp4", root, config)
        artifacts = root / "jobs" / job.id / "artifacts"
        artifacts.mkdir(parents=True)
        source_path = artifacts / "source-cues.json"
        source_path.write_text(
            json.dumps(
                [
                    {
                        "id": cue_id,
                        "start_ms": (cue_id - 1) * 1000,
                        "end_ms": cue_id * 1000,
                        "source_text": f"S{cue_id}",
                    }
                    for cue_id in (1, 2, 3)
                ]
            ),
            encoding="utf-8",
        )
        translated_path = artifacts / "translated-cues.json"
        translated_path.write_text(
            json.dumps(
                [
                    {
                        "id": cue_id,
                        "start_ms": (cue_id - 1) * 1000,
                        "end_ms": cue_id * 1000,
                        "source_text": f"S{cue_id}",
                        "target_text": f"译{cue_id}",
                        "target_language": "zh-CN",
                    }
                    for cue_id in (1, 2, 3)
                ]
            ),
            encoding="utf-8",
        )
        job.artifacts.extend(
            [
                Artifact("source_cues", str(source_path), file_checksum(source_path)),
                Artifact("translated_cues", str(translated_path), file_checksum(translated_path)),
            ]
        )
        job_file = save_job(job)
        retranslate_cues(job_file, cue_ids=(2,), target_language="zh-CN", provider="agent")
        if glossary_qa_blocked:
            # 术语死锁的真实形态：用户按 status 的出口跑完 retranslate-cues 后，译文 QA
            # 报告还是那一份 failed 的旧报告，SUBTITLE_QA 仍停在 FAILED。
            job = load_job(job_file)
            artifacts = root / "jobs" / job.id / "artifacts"
            source_qa = artifacts / "qa-source.json"
            source_qa.write_text(json.dumps({"status": "pass", "issues": []}), encoding="utf-8")
            translated_qa = artifacts / "qa-translated.json"
            translated_qa.write_text(
                json.dumps(
                    {
                        "status": "failed",
                        "issues": [
                            {
                                "code": "glossary_violation",
                                "severity": "error",
                                "cue_id": 2,
                                "message": "glossary term 'widget' requires '小组件'",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            job.artifacts.extend(
                [
                    Artifact("subtitle_qa", str(source_qa), file_checksum(source_qa)),
                    Artifact(
                        "translated_subtitle_qa",
                        str(translated_qa),
                        file_checksum(translated_qa),
                    ),
                ]
            )
            job.stages[StageName.SUBTITLE_QA].status = StageStatus.FAILED
            job_file = save_job(job)
        return job_file

    def test_glossary_qa_block_does_not_shadow_the_pending_retranslation(self) -> None:
        """按术语死锁出口跑完 retranslate-cues 后，status 必须报等待而不是再劝一次重译。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._waiting_job_file(
                root,
                workflow={"target_language": "zh-CN"},
                glossary_qa_blocked=True,
            )

            plan = build_recovery_plan(job_file)

            self.assertEqual(plan.category, "awaiting_input")
            self.assertEqual(plan.advance_steps, ("await_cue_retranslation",))
            self.assertIsNone(plan.primary_command)
            self.assertIn("xvt apply-cue-retranslation", plan.prerequisite_commands[0])
            self.assertNotIn("retranslate-cues", " ".join(plan.prerequisite_commands))

    def _failed_retranslation(self, job_file: Path) -> None:
        """跑一次 API 模式重译并让它失败：TRANSLATE 落到 failed，manifest 原样保留。

        必须走 API 模式。用户模式的 ``apply-cue-retranslation`` 校验失败已经不再改写 Job
        状态（坏输入下 TRANSLATE 保持 awaiting_input），拿它铺 failed 只会得到等待态，
        这两条用例就测不到 failed 分支了。failed + 有效 manifest 现在只来自 API 模式重译
        失败（响应是内部产物）或旧版本留下的历史 Job。
        """

        provider_env = {
            "XVT_TRANSLATION_BASE_URL": "https://provider.example.com/v1",
            "XVT_TRANSLATION_MODEL": "test-model",
            "XVT_TRANSLATION_API_KEY": "secret",
        }
        with (
            patch.dict(os.environ, provider_env, clear=False),
            # 提供方交回请求范围外的 cue id：覆盖范围校验失败，stage 已在 running。
            patch(
                "xvt.pipeline.translation.translate_batch",
                side_effect=lambda *_a, **_k: {9: "越界译文"},
            ),
        ):
            with self.assertRaises(ValueError):
                retranslate_cues(
                    job_file,
                    cue_ids=(2,),
                    target_language="zh-CN",
                    provider="openai-compatible",
                )
        self.assertIs(load_job(job_file).stages[StageName.TRANSLATE].status, StageStatus.FAILED)

    def test_a_failed_retranslation_still_reports_the_pending_retranslation(self) -> None:
        """API 模式重译失败不作废这次等待：status 必须继续提示回填，而不是再劝一次重译。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._waiting_job_file(
                root,
                workflow={"target_language": "zh-CN"},
                glossary_qa_blocked=True,
            )
            self._failed_retranslation(job_file)

            plan = build_recovery_plan(job_file)

            self.assertEqual(plan.category, "awaiting_input")
            self.assertEqual(plan.advance_steps, ("await_cue_retranslation",))
            self.assertIn("xvt apply-cue-retranslation", plan.prerequisite_commands[0])
            self.assertNotIn("retranslate-cues", " ".join(plan.prerequisite_commands))

    def test_atomic_failed_retranslation_still_reports_the_pending_retranslation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._waiting_job_file(root, workflow=None)
            self._failed_retranslation(job_file)

            plan = build_recovery_plan(job_file)

            self.assertEqual(plan.category, "awaiting_input")
            self.assertIn("xvt apply-cue-retranslation", plan.prerequisite_commands[0])

    def test_atomic_glossary_qa_block_also_reports_the_pending_retranslation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._waiting_job_file(root, workflow=None, glossary_qa_blocked=True)

            plan = build_recovery_plan(job_file)

            self.assertEqual(plan.category, "awaiting_input")
            self.assertEqual(plan.advance_steps, ())
            self.assertIn("xvt apply-cue-retranslation", plan.prerequisite_commands[0])

    def test_end_to_end_job_reports_the_pending_cue_retranslation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._waiting_job_file(root, workflow={"target_language": "zh-CN"})
            plan = build_recovery_plan(job_file)

            self.assertEqual(plan.category, "awaiting_input")
            self.assertEqual(plan.advance_steps, ("await_cue_retranslation",))
            self.assertIsNone(plan.primary_command)
            self.assertEqual(len(plan.prerequisite_commands), 1)
            command = plan.prerequisite_commands[0]
            self.assertIn("xvt apply-cue-retranslation", command)
            self.assertIn("--cue 2", command)
            self.assertIn("--target-language zh-CN", command)
            self.assertNotIn("--cue 1", command)
            self.assertNotIn("--cue 3", command)
            self.assertEqual(len(plan.report_paths), 1)
            self.assertTrue(plan.report_paths[0].endswith("cue-retranslation-batches.json"))

    def test_atomic_job_no_longer_suggests_apply_translation(self) -> None:
        """原子分支此前把重译等待当初译等待，给出的 apply-translation 命令对不上。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._waiting_job_file(root, workflow=None)
            plan = build_recovery_plan(job_file)

            self.assertEqual(plan.category, "awaiting_input")
            self.assertEqual(plan.advance_steps, ())
            self.assertIsNone(plan.primary_command)
            command = plan.prerequisite_commands[0]
            self.assertIn("xvt apply-cue-retranslation", command)
            self.assertIn("--cue 2", command)
            self.assertIn("--target-language zh-CN", command)
            self.assertNotIn("apply-translation ", command)
            self.assertTrue(plan.report_paths[0].endswith("cue-retranslation-batches.json"))

    def test_unreadable_manifest_body_falls_back_to_a_shape_placeholder(self) -> None:
        """cue id 读不出来时退回形状占位，绝不猜测 cue id。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._waiting_job_file(root, workflow={"target_language": "zh-CN"})
            job = load_job(job_file)
            manifest = next(
                artifact
                for artifact in job.artifacts
                if artifact.kind == "cue_retranslation_batches"
            )
            path = Path(manifest.path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["batches"] = "not a list"
            path.write_text(json.dumps(payload), encoding="utf-8")
            manifest.checksum = file_checksum(path)
            save_job(job)

            plan = build_recovery_plan(job_file)

            # source / translated checksum 仍然对得上，因此确实还在等待；但 cue id
            # 已经读不出来，命令必须退回形状占位而不是编一个 cue id 出来。
            self.assertEqual(plan.category, "awaiting_input")
            command = plan.prerequisite_commands[0]
            self.assertIn("--cue ID [--cue ID ...]", command)
            self.assertIn("--target-language LANG", command)


class CandidateQaBlockerReasonTest(unittest.TestCase):
    """候选译文 QA 的等待原因必须点名 cue，并在 message 异常时安全降级。"""

    @staticmethod
    def _issue(code: str, cue_id: int, message: str) -> dict[str, object]:
        return {"code": code, "severity": "review", "cue_id": cue_id, "message": message}

    def test_overflow_issues_name_the_cues_with_width_and_limit(self) -> None:
        reason = _candidate_qa_blocker_reason(
            (
                self._issue(
                    "bilingual_overflow_risk", 22, "target line width 41 exceeds 36 display units"
                ),
                self._issue(
                    "bilingual_overflow_risk", 33, "target line width 37 exceeds 36 display units"
                ),
            )
        )

        self.assertEqual(
            reason,
            "候选译文有 2 条 cue 行宽超限：22（41/36）、33（37/36）；请缩短这些译文后继续。"
            "这类问题不能通过语义确认绕过，也不会触发重新翻译或润色。",
        )

    def test_non_overflow_blockers_are_grouped_by_code(self) -> None:
        reason = _candidate_qa_blocker_reason(
            (
                self._issue(
                    "bilingual_overflow_risk", 22, "target line width 41 exceeds 36 display units"
                ),
                self._issue("missing_translation", 5, "missing translation"),
                self._issue("missing_translation", 9, "missing translation"),
                self._issue("glossary_violation", 12, "glossary term not used"),
            )
        )

        self.assertIn("候选译文有 1 条 cue 行宽超限：22（41/36）；请缩短这些译文后继续。", reason)
        self.assertIn("另有 2 条 cue 缺少译文：5、9。", reason)
        self.assertIn("另有 1 条 cue 违反术语表：12。", reason)

    def test_blockers_without_overflow_lead_with_the_template_edit_action(self) -> None:
        reason = _candidate_qa_blocker_reason(
            (self._issue("missing_translation", 5, "missing translation"),)
        )

        self.assertTrue(
            reason.startswith("候选译文有 1 条 cue 缺少译文：5；请修改译文模板后继续。")
        )

    def test_unparsable_width_message_degrades_to_the_cue_id_only(self) -> None:
        reason = _candidate_qa_blocker_reason(
            (self._issue("bilingual_overflow_risk", 7, "target line is too wide"),)
        )

        self.assertIn("候选译文有 1 条 cue 行宽超限：7；请缩短这些译文后继续。", reason)
        self.assertNotIn("（", reason.split("；")[0])

    def test_empty_issue_set_keeps_the_generic_reason(self) -> None:
        reason = _candidate_qa_blocker_reason(())

        self.assertEqual(
            reason,
            "候选译文的确定性 QA 仍有可修复问题；请修改译文模板后继续。"
            "这类问题不能通过语义确认绕过，也不会触发重新翻译或润色。",
        )


class SemanticReviewContinueCommandTest(unittest.TestCase):
    """`xvt apply ... --continue` 只能推荐给持久化了 workflow 目标的 Job。"""

    def _primary_command(self, *, workflow: bool) -> str:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/source.srt", root, {"input_kind": "subtitle"})
            job.config["translation_quality"] = "polished"
            if workflow:
                job.config["workflow"] = {"target_language": "zh-CN", "burn": False}
            candidate = root / "translation-polished-cues.json"
            candidate.write_text("[]", encoding="utf-8")
            job.artifacts.append(
                Artifact("translation_polished_cues", str(candidate), file_checksum(candidate))
            )
            review_input = root / "translation-review.json"
            review_input.write_text("{}", encoding="utf-8")
            issues = (
                {
                    "code": "missing_translation",
                    "severity": "review",
                    "cue_id": 5,
                    "message": "missing translation",
                },
            )
            with (
                patch(
                    "xvt.pipeline.revision.translation_review_input_path",
                    return_value=review_input,
                ),
                patch("xvt.pipeline.revision.source_suspect_cue_ids", return_value=()),
                patch("xvt.recovery.translation_candidate_qa_issues", return_value=issues),
            ):
                _reason, primary, _alternatives, _inputs = _semantic_review_options(
                    job,
                    root / "job.json",
                    acknowledgment_command="xvt acknowledge-translation-review JOB",
                )
        return primary

    def test_atomic_job_keeps_the_two_step_apply_translation_review_fixes(self) -> None:
        primary = self._primary_command(workflow=False)

        self.assertIn("apply-translation-review-fixes", primary)
        self.assertNotIn("--continue", primary)

    def test_workflow_job_recommends_apply_with_continue(self) -> None:
        primary = self._primary_command(workflow=True)

        self.assertIn("--continue", primary)
        self.assertNotIn("apply-translation-review-fixes", primary)


class QaBlockedAgentWaitRecoveryTest(unittest.TestCase):
    """SUBTITLE_QA 停在 failed 时，端到端 Job 的在途 Agent 等待必须压过 qa_blocked 分支。

    ``SUBTITLE_QA`` 不会因为随后开了一次新的等待而复位：术语死锁后改走整轮重翻时，
    ``prepare_agent_translation`` 只置 ``TRANSLATE=awaiting_input``。按 qa_blocked 处理会把
    出口指向已经被移出登记的 retranslate-cues manifest，而统一 ``xvt apply`` 拿不到
    ``await_agent_translation`` 路由，用户两头无路。
    """

    def _job_file(self, root: Path, *, batches: bool) -> Path:
        job = create_job("/tmp/input.mp4", root, {"workflow": {"target_language": "zh-CN"}})
        artifacts = root / "jobs" / job.id / "artifacts"
        artifacts.mkdir(parents=True)
        source_path = artifacts / "source-cues.json"
        source_path.write_text(
            json.dumps([{"id": 1, "start_ms": 0, "end_ms": 1000, "source_text": "S1"}]),
            encoding="utf-8",
        )
        # 旧译文与旧 QA 报告刻意保留：等待期间它们对用户仍有价值。渲染产物没有，
        # 因为译文 QA 失败时 finalize_translation 走不到渲染。
        translated_path = artifacts / "translated-cues.json"
        translated_path.write_text(
            json.dumps(
                [
                    {
                        "id": 1,
                        "start_ms": 0,
                        "end_ms": 1000,
                        "source_text": "S1",
                        "target_text": "译1",
                        "target_language": "zh-CN",
                    }
                ]
            ),
            encoding="utf-8",
        )
        source_qa = artifacts / "qa-source.json"
        source_qa.write_text(json.dumps({"status": "pass", "issues": []}), encoding="utf-8")
        translated_qa = artifacts / "qa-translated.json"
        translated_qa.write_text(
            json.dumps(
                {
                    "status": "failed",
                    "issues": [
                        {
                            "code": "glossary_violation",
                            "severity": "error",
                            "cue_id": 1,
                            "message": "glossary term 'widget' requires '小组件'",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        job.artifacts.extend(
            [
                Artifact("source_cues", str(source_path), file_checksum(source_path)),
                Artifact("translated_cues", str(translated_path), file_checksum(translated_path)),
                Artifact("subtitle_qa", str(source_qa), file_checksum(source_qa)),
                Artifact(
                    "translated_subtitle_qa",
                    str(translated_qa),
                    file_checksum(translated_qa),
                ),
            ]
        )
        if batches:
            manifest = artifacts / "translation-batches.json"
            manifest.write_text(json.dumps({"batches": []}), encoding="utf-8")
            job.artifacts.append(
                Artifact("translation_batches", str(manifest), file_checksum(manifest))
            )
        job.stages[StageName.SUBTITLE_QA].status = StageStatus.FAILED
        job.stages[StageName.TRANSLATE].status = StageStatus.AWAITING_INPUT
        return save_job(job)

    def test_pending_full_round_wins_over_the_stale_qa_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job_file(Path(directory), batches=True)

            plan = build_recovery_plan(job_file)

            self.assertEqual(plan.category, "awaiting_input")
            self.assertEqual(plan.advance_steps, ("await_agent_translation",))
            self.assertIn("xvt apply ", plan.prerequisite_commands[0])
            self.assertNotIn("retranslate-cues", " ".join(plan.prerequisite_commands))
            self.assertNotIn("retranslate-cues", plan.primary_command or "")

    def test_without_a_pending_request_the_qa_guidance_still_wins(self) -> None:
        """护栏：等待态残留（批次 manifest 已经不在）不得被当成在途等待放行。"""

        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job_file(Path(directory), batches=False)

            plan = build_recovery_plan(job_file)

            self.assertEqual(plan.category, "manual_fix")
            self.assertIn("xvt retranslate-cues", plan.primary_command or "")


class AtomicQaBlockedAgentWaitRecoveryTest(unittest.TestCase):
    """F4：原子 Job 的 ``qa_blocked`` 也必须给在途 Agent 等待让路。

    术语死锁后用户改跑 ``translate --provider agent``：``prepare_agent_translation`` 只置
    ``TRANSLATE=awaiting_input``，不复位 ``SUBTITLE_QA``。按 qa_blocked 处理会继续推荐
    ``retranslate-cues``，照做会盖掉刚写入的整轮 manifest。端到端分支已有同构让路。
    """

    def _job_file(self, root: Path, *, batches: bool) -> Path:
        job = create_job("/tmp/input.mp4", root, {"target_language": "zh-CN"})
        artifacts = root / "jobs" / job.id / "artifacts"
        artifacts.mkdir(parents=True)

        def register(kind: str, name: str, payload: object) -> None:
            path = artifacts / name
            path.write_text(json.dumps(payload), encoding="utf-8")
            job.artifacts.append(Artifact(kind, str(path), file_checksum(path)))

        register("source_cues", "source-cues.json", [])
        # 旧译文与旧 QA 报告刻意保留：等待期间它们对用户仍有价值。
        register("translated_cues", "translated-cues.json", [])
        register("subtitle_qa", "qa-source.json", {"status": "pass", "issues": []})
        register(
            "translated_subtitle_qa",
            "qa-translated.json",
            {
                "status": "failed",
                "issues": [
                    {
                        "code": "glossary_violation",
                        "severity": "error",
                        "cue_id": 1,
                        "message": "glossary term 'widget' requires '小组件'",
                    }
                ],
            },
        )
        if batches:
            register("translation_batches", "translation-batches.json", {"batches": []})
        job.stages[StageName.SUBTITLE_QA].status = StageStatus.FAILED
        job.stages[StageName.TRANSLATE].status = StageStatus.AWAITING_INPUT
        return save_job(job)

    def test_pending_full_round_wins_over_the_stale_qa_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job_file(Path(directory), batches=True)

            plan = build_recovery_plan(job_file)

        self.assertEqual(plan.category, "awaiting_input")
        self.assertIn("apply-translation", plan.prerequisite_commands[0])
        self.assertNotIn("retranslate-cues", " ".join(plan.prerequisite_commands))
        self.assertNotIn("retranslate-cues", plan.primary_command or "")

    def test_without_a_pending_request_the_qa_guidance_still_wins(self) -> None:
        """护栏：等待态残留（批次 manifest 已经不在）不得被当成在途等待放行。"""

        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job_file(Path(directory), batches=False)

            plan = build_recovery_plan(job_file)

        self.assertEqual(plan.category, "manual_fix")
        self.assertIn("xvt retranslate-cues", plan.primary_command or "")


class AtomicTranslatedQaRebuildTest(unittest.TestCase):
    """原子/历史 Job 上译文 QA 报告单独失效不能再归 complete。"""

    def _job_with_stale_translated_qa(
        self,
        root: Path,
        *,
        translated_srt: bool = True,
        qa_blocked: bool = False,
        target_language: str = "zh-CN",
        translation_quality: str = "standard",
    ) -> Path:
        job = create_job(
            "/tmp/input.mp4",
            root,
            {"target_language": target_language, "translation_quality": translation_quality},
        )
        artifacts = root / "jobs" / job.id / "artifacts"
        artifacts.mkdir(parents=True)
        source_cues = artifacts / "source-cues.json"
        source_cues.write_text(
            json.dumps([{"id": 1, "start_ms": 0, "end_ms": 1000, "source_text": "Hello"}]),
            encoding="utf-8",
        )
        translated_cues = artifacts / "translated-cues.json"
        translated_cues.write_text(
            json.dumps(
                [
                    {
                        "id": 1,
                        "start_ms": 0,
                        "end_ms": 1000,
                        "source_text": "Hello",
                        "target_text": "你好",
                        "target_language": "zh-CN",
                    }
                ]
            ),
            encoding="utf-8",
        )
        job.artifacts.extend(
            [
                Artifact("source_cues", str(source_cues), file_checksum(source_cues)),
                Artifact("translated_cues", str(translated_cues), file_checksum(translated_cues)),
            ]
        )
        if translated_srt:
            path = artifacts / "translated.srt"
            path.write_text("1\n00:00:00,000 --> 00:00:01,000\n你好\n", encoding="utf-8")
            job.artifacts.append(Artifact("translated_srt", str(path), file_checksum(path)))
        # 登记过、但内容已被改写：checksum 失效，报告不再有效。
        translated_qa = artifacts / "qa-translated.json"
        translated_qa.write_text('{"status":"pass","issues":[]}', encoding="utf-8")
        job.artifacts.append(
            Artifact("translated_subtitle_qa", str(translated_qa), file_checksum(translated_qa))
        )
        translated_qa.write_text('{"status":"pass","issues":[],"stale":true}', encoding="utf-8")
        if qa_blocked:
            # _translated_qa_needs_rebuild 还要求一份有效且无 error 的源字幕 QA 报告。
            source_qa = artifacts / "qa-source.json"
            source_qa.write_text('{"status":"pass","issues":[]}', encoding="utf-8")
            job.artifacts.append(Artifact("subtitle_qa", str(source_qa), file_checksum(source_qa)))
            job.stages[StageName.SUBTITLE_QA].status = StageStatus.FAILED
        return save_job(job)

    def test_stale_report_with_reusable_subtitles_is_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job_with_stale_translated_qa(root)

            plan = build_recovery_plan(job_file)

            self.assertEqual(plan.category, "retryable")
            self.assertIn("xvt import-translation", plan.primary_command or "")
            self.assertIn("--target-language zh-CN", plan.primary_command or "")
            self.assertIn("translated.srt", plan.primary_command or "")
            self.assertIn("零 API 成本", plan.reason)
            self.assertIn("导入译文", plan.reason)
            self.assertEqual(len(plan.alternative_commands), 1)
            self.assertIn("xvt translate", plan.alternative_commands[0])

    def test_stale_report_without_subtitles_needs_a_real_translation_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job_with_stale_translated_qa(root, translated_srt=False)

            plan = build_recovery_plan(job_file)

            self.assertEqual(plan.category, "manual_fix")
            self.assertIn("xvt translate", plan.primary_command or "")
            self.assertIn("重新调用翻译提供方", plan.reason)

    def test_qa_blocked_atomic_job_is_no_longer_a_dead_end(self) -> None:
        """此前这条分支对 workflow 为 None 的 Job 给 primary_command=None，无路可走。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job_with_stale_translated_qa(root, qa_blocked=True)

            plan = build_recovery_plan(job_file)

            self.assertEqual(plan.category, "retryable")
            self.assertIsNotNone(plan.primary_command)
            self.assertIn("xvt import-translation", plan.primary_command or "")

    def test_polished_job_is_not_offered_the_zero_cost_rebuild(self) -> None:
        """polished Job 上 import-translation 走 draft 分支：QA 不重建、整条润色腿被摧毁。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job_with_stale_translated_qa(root, translation_quality="polished")

            plan = build_recovery_plan(job_file)

            self.assertEqual(plan.category, "manual_fix")
            self.assertNotIn("import-translation", plan.primary_command or "")
            self.assertNotIn("import-translation", " ".join(plan.alternative_commands))
            self.assertIn("xvt translate", plan.primary_command or "")
            self.assertIsNone(atomic_translated_qa_rebuild_command(load_job(job_file), job_file))

    def test_atomic_rebuild_command_quotes_a_target_language_with_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job_with_stale_translated_qa(root, target_language="zh CN")

            plan = build_recovery_plan(job_file)

            command = plan.primary_command or ""
            self.assertIn("zh CN", shlex.split(command))
            self.assertIn("zh CN", shlex.split(plan.alternative_commands[0]))


class AtomicWaitingWithoutBatchesTest(unittest.TestCase):
    """TRANSLATE 停在等待态、但批次 manifest 已经不在时不能再劝 apply-translation。"""

    def _waiting_job(self, root: Path, *, batches: bool) -> Path:
        job = create_job("/tmp/input.mp4", root, {"target_language": "zh CN"})
        artifacts = root / "jobs" / job.id / "artifacts"
        artifacts.mkdir(parents=True)
        source_cues = artifacts / "source-cues.json"
        source_cues.write_text(
            json.dumps([{"id": 1, "start_ms": 0, "end_ms": 1000, "source_text": "Hello"}]),
            encoding="utf-8",
        )
        job.artifacts.append(Artifact("source_cues", str(source_cues), file_checksum(source_cues)))
        if batches:
            manifest = artifacts / "translation-batches.json"
            manifest.write_text(json.dumps({"batches": []}), encoding="utf-8")
            job.artifacts.append(
                Artifact("translation_batches", str(manifest), file_checksum(manifest))
            )
        job.stages[StageName.TRANSLATE].status = StageStatus.AWAITING_INPUT
        return save_job(job)

    def test_missing_batches_manifest_points_at_a_rerun_instead_of_a_backfill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._waiting_job(root, batches=False)

            plan = build_recovery_plan(job_file)

            self.assertEqual(plan.category, "retryable")
            self.assertIn("xvt translate", plan.primary_command or "")
            self.assertEqual(plan.prerequisite_commands, ())
            self.assertIn("没有任何响应能回填这次等待", plan.reason)
            # 含空格的目标语言必须被 quote，命令才能原样复制执行。
            self.assertIn("zh CN", shlex.split(plan.primary_command or ""))

    def test_valid_batches_manifest_keeps_the_backfill_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._waiting_job(root, batches=True)

            plan = build_recovery_plan(job_file)

            self.assertEqual(plan.category, "awaiting_input")
            self.assertIn("xvt apply-translation", plan.prerequisite_commands[0])
            self.assertIn("zh CN", shlex.split(plan.prerequisite_commands[0]))


class GlossaryViolationDeadlockTest(unittest.TestCase):
    """译文只因术语违规而 QA 失败时必须有出口，且文案按质量档如实分岔。"""

    def _blocked_job(
        self,
        root: Path,
        *,
        quality: str = "standard",
        translated_issues: list[dict] | None = None,
        source_issues: list[dict] | None = None,
    ) -> Path:
        job = create_job(
            "/tmp/input.mp4",
            root,
            {"workflow": {"target_language": "zh-CN", "translation_quality": quality}},
        )
        artifacts = root / "jobs" / job.id / "artifacts"
        artifacts.mkdir(parents=True)

        def register(kind: str, name: str, payload: object) -> None:
            path = artifacts / name
            path.write_text(json.dumps(payload), encoding="utf-8")
            job.artifacts.append(Artifact(kind, str(path), file_checksum(path)))

        register("source_cues", "source-cues.json", [])
        register("translated_cues", "translated-cues.json", [])
        register(
            "subtitle_qa",
            "qa-source.json",
            {"status": "failed" if source_issues else "pass", "issues": source_issues or []},
        )
        register(
            "translated_subtitle_qa",
            "qa-translated.json",
            {
                "status": "failed",
                "issues": translated_issues
                if translated_issues is not None
                else [
                    {
                        "code": "glossary_violation",
                        "severity": "error",
                        "cue_id": 7,
                        "message": "glossary term 'widget' requires '小组件'",
                    },
                    {
                        "code": "glossary_violation",
                        "severity": "error",
                        "cue_id": 3,
                        "message": "glossary term 'widget' requires '小组件'",
                    },
                    {"code": "line_too_wide", "severity": "warning", "cue_id": 9},
                ],
            },
        )
        job.stages[StageName.SUBTITLE_QA].status = StageStatus.FAILED
        return save_job(job)

    def test_standard_job_gets_a_retranslate_cues_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._blocked_job(root)

            plan = build_recovery_plan(job_file)

            self.assertEqual(plan.category, "manual_fix")
            command = plan.primary_command or ""
            self.assertIn("xvt retranslate-cues", command)
            # cue id 去重且按升序，只覆盖 error 级 glossary_violation。
            self.assertIn("--cue 3 --cue 7", command)
            self.assertNotIn("--cue 9", command)
            self.assertIn("--target-language zh-CN", command)
            self.assertIn("其余译文与全部时间轴保持不变", plan.reason)
            self.assertIn("术语表冲突", plan.reason)

    def test_polished_job_reason_does_not_promise_untouched_translations(self) -> None:
        """polished Job 上回填会写成新草稿并重启整条润色腿，文案不得承诺其余译文不变。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._blocked_job(root, quality="polished")

            plan = build_recovery_plan(job_file)

            self.assertIn("xvt retranslate-cues", plan.primary_command or "")
            self.assertNotIn("其余译文与全部时间轴保持不变", plan.reason)
            self.assertIn("新的翻译草稿", plan.reason)
            self.assertIn("重新走一遍润色与语义校对", plan.reason)

    def test_other_blocking_errors_keep_the_generic_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._blocked_job(
                root,
                translated_issues=[
                    {"code": "glossary_violation", "severity": "error", "cue_id": 3},
                    {"code": "missing_translation", "severity": "error", "cue_id": 4},
                ],
            )

            plan = build_recovery_plan(job_file)

            self.assertEqual(plan.category, "manual_fix")
            self.assertIsNone(plan.primary_command)
            self.assertIn("修复字幕", plan.reason)

    def test_source_errors_block_the_retranslation_exit(self) -> None:
        """源字幕自身有 error 时重译译文改不了问题，不能给这条出口。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._blocked_job(
                root,
                source_issues=[{"code": "cue_overlap", "severity": "error", "cue_id": 1}],
            )

            guidance = subtitle_qa_failure_guidance(load_job(job_file), job_file)

            assert guidance is not None
            self.assertIsNone(guidance.primary_command)
            self.assertFalse(guidance.is_needs_review)

    def test_guidance_message_carries_the_command_for_continue(self) -> None:
        """continue 把 message 作为 RuntimeError 抛出，命令必须在消息里。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._blocked_job(root)

            guidance = subtitle_qa_failure_guidance(load_job(job_file), job_file)

            assert guidance is not None
            self.assertIn("xvt retranslate-cues", guidance.message)
            self.assertIn("--cue 3 --cue 7", guidance.message)

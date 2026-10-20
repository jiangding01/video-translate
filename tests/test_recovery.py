import json
import os
import shlex
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from xvt.cli import main
from xvt.events import EventType
from xvt.job_store import create_job, load_job, save_job
from xvt.media import MediaInfo
from xvt.models import Artifact, StageName, StageStatus, Transcript, Word
from xvt.pipeline import pending_translation_import_blocker, retranslate_cues
from xvt.process import CommandResult
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
from xvt.workflow import continue_workflow, plan_advance_steps
from xvt.workflow_config import require_workflow_config


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

    def test_verify_job_warns_on_artifact_without_checksum(self) -> None:
        """没有登记 checksum 的 artifact 只记 warn，但**绝不**算有效产物。

        O7c：这条分支此前零覆盖——把 ``warn`` 改成 ``pass`` 全量 915 条仍绿。
        它同时是 ``valid_artifact_path`` 的判定来源，一旦退化成 ``pass``，一个从未
        登记过内容 checksum 的文件就会被恢复链路当成有效产物复用，违反 AGENTS
        不可破坏约束 9「产物有效性只认内容 checksum」。所以这里既钉住 verify 的
        三元结果（warn 不升级为 fail、warning_count 计数），也钉住下游后果。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            artifact_path = root / "artifact.json"
            artifact_path.write_text("{}", encoding="utf-8")
            job.artifacts.append(Artifact("example", str(artifact_path), None))
            job_file = save_job(job)

            report = verify_job(job_file)
            unchecked_is_valid = valid_artifact_path(job, "example") is not None

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["failed_count"], 0)
        self.assertEqual(report["warning_count"], 1)
        self.assertEqual(report["artifacts"][0]["status"], "warn")
        self.assertEqual(report["artifacts"][0]["message"], "artifact has no checksum")
        self.assertIsNone(report["artifacts"][0]["expected_checksum"])
        self.assertFalse(unchecked_is_valid)

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
            # 源媒体必须真实存在：缺失源媒体是另一类恢复分类（manual_fix），
            # 用不存在的路径会把本例想测的那条原因盖掉。
            media = root / "input.mp4"
            media.write_bytes(b"media")
            job = create_job(str(media), root)
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
            # 源媒体必须真实存在：缺失源媒体是另一类恢复分类（manual_fix），
            # 用不存在的路径会把本例想测的那条原因盖掉。
            media = root / "input.mp4"
            media.write_bytes(b"media")
            job = create_job(str(media), root)
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
            # 源媒体必须真实存在：缺失源媒体是另一类恢复分类（manual_fix），
            # 用不存在的路径会把本例想测的那条原因盖掉。
            media = root / "input.mp4"
            media.write_bytes(b"media")
            job = create_job(str(media), root)
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

    def _invalid_polish_draft_job(self, root: Path, *, with_translated_srt: bool):
        """`repair_polish_draft` 的 manual_fix 骨架；可选挂一份 checksum 有效的译文字幕。"""

        job = create_job("/tmp/input.mp4", root)
        job.config["workflow"] = {
            "target_language": "zh-CN",
            "translation_quality": "polished",
            "provider": "agent",
        }
        self._add_artifact(job, root, "source_cues")
        draft = self._add_artifact(job, root, "translation_draft_cues")
        self._add_artifact(job, root, "translation_polish_batches")
        if with_translated_srt:
            self._add_artifact(job, root, "translated_srt", content="1\n")
        draft.write_text("corrupt", encoding="utf-8")
        return save_job(job), draft

    def test_repair_polish_draft_offers_import_translation_with_valid_subtitle(self) -> None:
        """Job 内有 checksum 有效的译文字幕时，manual_fix 必须给出确切的重新导入命令。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, _draft = self._invalid_polish_draft_job(root, with_translated_srt=True)
            translated_srt = root / "translated_srt.json"

            plan = build_recovery_plan(job_file)

        self.assertEqual(plan.category, "manual_fix")
        self.assertEqual(plan.advance_steps, ("repair_polish_draft",))
        self.assertEqual(
            plan.primary_command,
            "xvt import-translation "
            f"{shlex.quote(str(job_file))} {shlex.quote(str(translated_srt))} "
            "--target-language zh-CN",
        )
        expected_alternative = (
            f"xvt translate {shlex.quote(str(job_file))} --target-language zh-CN --provider agent"
        )
        self.assertEqual(plan.alternative_commands, (expected_alternative,))
        self.assertIn("重跑", plan.reason)

    def test_repair_polish_draft_without_subtitle_names_the_missing_draft_path(self) -> None:
        """没有可复用字幕时仍不推荐命令，但必须点名那个缺失/已变化的草稿文件。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, draft = self._invalid_polish_draft_job(root, with_translated_srt=False)

            plan = build_recovery_plan(job_file)

        self.assertEqual(plan.category, "manual_fix")
        self.assertIsNone(plan.primary_command)
        self.assertIn(str(draft), plan.reason)
        expected_alternative = (
            f"xvt translate {shlex.quote(str(job_file))} --target-language zh-CN --provider agent"
        )
        self.assertEqual(plan.alternative_commands, (expected_alternative,))

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

    def _alignment_deadlock_job(self, root: Path, **config_overrides) -> Path:
        """构造「强制对齐置信度不足」的断点态：转写 checkpoint 全在，只缺 source_cues。"""

        config = {
            "engine": "mlx",
            "source_language": "ja",
            "source_strategy": "auto",
            "align_subtitles": True,
            "minimum_alignment_confidence": 0.55,
            "allow_low_confidence_alignment": False,
            "workflow": {"target_language": "zh-CN", "burn": False},
        }
        config.update(config_overrides)
        job = create_job("/tmp/input.mp4", root, config)
        self._add_artifact(job, root, "media_info")
        self._add_artifact(job, root, "normalized_audio")
        self._add_artifact(job, root, "word_transcript")
        self._add_artifact(job, root, "input_subtitles")
        self._add_artifact(
            job, root, "subtitle_alignment", json.dumps({"confidence": 0.231, "cues": []})
        )
        job.stages[StageName.ALIGN].status = StageStatus.FAILED
        job.stages[
            StageName.ALIGN
        ].error = "subtitle alignment confidence is below the required threshold (0.231 < 0.550)"
        return save_job(job)

    def test_alignment_confidence_deadlock_does_not_recommend_a_doomed_continue(self) -> None:
        """`continue` 在这个状态下必然再次失败，恢复计划必须给出真正的出口。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = build_recovery_plan(self._alignment_deadlock_job(root))

        self.assertEqual(plan.category, "manual_fix")
        self.assertIn("rebuild-source", plan.primary_command or "")
        self.assertIn("--strategy asr", plan.primary_command or "")
        commands = (plan.primary_command or "", *plan.alternative_commands)
        self.assertFalse(
            any(command.split()[1:2] == ["continue"] for command in commands),
            f"注定失败的 continue 不得出现在恢复计划里: {commands}",
        )
        self.assertIn("置信度", plan.reason)

    def test_alignment_deadlock_detection_requires_the_same_align_inputs(self) -> None:
        """转写 checkpoint 失效时重跑可能得到不同置信度，不得断言「必然失败」。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._alignment_deadlock_job(root)
            job = load_job(job_file)
            job.artifacts = [
                artifact for artifact in job.artifacts if artifact.kind != "word_transcript"
            ]
            save_job(job)

            plan = build_recovery_plan(job_file)

        self.assertEqual(plan.category, "retryable")
        self.assertIn("continue", plan.primary_command or "")

    def test_low_confidence_alignment_allowed_job_is_still_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = build_recovery_plan(
                self._alignment_deadlock_job(root, allow_low_confidence_alignment=True)
            )

        self.assertEqual(plan.category, "retryable")
        self.assertIn("continue", plan.primary_command or "")

    def test_alignment_above_the_threshold_is_not_treated_as_a_deadlock(self) -> None:
        """置信度达标时这条判定不生效。

        强制对齐的第二种失败（对齐后字幕 QA 出 error 级问题）现在由
        ``_alignment_post_qa_deadlock`` 单独判定，用例在
        ``AlignmentPostQaDeadlockTest``。本 Job 没有 ``aligned_subtitle_qa`` 登记，
        那条判定够不着它，因此这里仍然只钉住置信度门这一条判定的边界。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._alignment_deadlock_job(root)
            job = load_job(job_file)
            path = Path(
                next(item.path for item in job.artifacts if item.kind == "subtitle_alignment")
            )
            path.write_text(json.dumps({"confidence": 0.9, "cues": []}), encoding="utf-8")
            for artifact in job.artifacts:
                if artifact.kind == "subtitle_alignment":
                    artifact.checksum = file_checksum(path)
            save_job(job)

            plan = build_recovery_plan(job_file)

        self.assertEqual(plan.category, "retryable")
        self.assertIn("continue", plan.primary_command or "")

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


class PendingTranslationImportDeadEndTest(unittest.TestCase):
    """`rebuild-source` 之后已存译文套不回新源 cue 时，导航必须给出真出口。

    这是本仓库第 7 次「status 推荐一条必然失败的命令」：`continue` 会在
    `map_subtitle_translations` 原地失败（退出码 2），而恢复计划照旧给
    `retryable` + `continue`，用户没有出口。

    场景全部用**真实 CLI 命令**造：`xvt run`（带 `--subtitles` 与
    `--translation-subtitles`）→ `xvt rebuild-source --strategy asr`。打桩只替掉
    ffprobe、ffmpeg 抽音与 ASR 模型三处，产物照真实运行落盘、artifact 照真实登记。
    """

    def _media_info(self, media: Path) -> MediaInfo:
        return MediaInfo(
            path=str(media),
            duration_ms=5_000,
            size_bytes=media.stat().st_size,
            format_name="mp4",
            width=1920,
            height=1080,
            frame_rate=30.0,
            video_codec="h264",
            audio_codec="aac",
            has_video=True,
            has_audio=True,
        )

    def _rebuilt_job(self, root: Path, *, words: tuple[Word, ...]) -> Path:
        media = root / "input.mp4"
        media.write_bytes(b"video")
        source = root / "source.srt"
        source.write_text(
            "1\n00:00:00,200 --> 00:00:01,500\nHello world.\n\n"
            "2\n00:00:03,000 --> 00:00:04,500\nSecond line.\n",
            encoding="utf-8",
        )
        translated = root / "translated.srt"
        translated.write_text(
            "1\n00:00:00,200 --> 00:00:01,500\n你好，世界。\n\n"
            "2\n00:00:03,000 --> 00:00:04,500\n第二行。\n",
            encoding="utf-8",
        )
        media_info = self._media_info(media)

        def run_ffmpeg(args, **_kwargs):
            Path(args[-1]).write_bytes(b"audio")
            return CommandResult(tuple(args), 0, "", "")

        with (
            patch("xvt.pipeline.common.probe_media", return_value=media_info),
            patch("xvt.pipeline.transcription.local.probe_media", return_value=media_info),
        ):
            self.assertEqual(
                main(
                    [
                        "run",
                        str(media),
                        "--output",
                        str(root / "work"),
                        "--subtitles",
                        str(source),
                        "--translation-subtitles",
                        str(translated),
                        "--target-language",
                        "zh-CN",
                    ]
                ),
                0,
            )
        job_file = next((root / "work").rglob("job.json"))

        with (
            patch("xvt.pipeline.common.probe_media", return_value=media_info),
            patch("xvt.pipeline.transcription.resume.probe_media", return_value=media_info),
            patch("xvt.pipeline.transcription.local.run_command", side_effect=run_ffmpeg),
            patch(
                "xvt.pipeline.transcription.local.transcribe_audio",
                return_value=Transcript(language="en", engine="mlx", words=words),
            ),
        ):
            self.assertEqual(main(["rebuild-source", str(job_file), "--strategy", "asr"]), 0)
        return job_file

    _UNMAPPABLE_WORDS = (Word("Hello.", 0, 500, 0.9),)
    _MAPPABLE_WORDS = (Word("Hello.", 200, 1_500, 0.9), Word("Second.", 3_000, 4_500, 0.9))

    def test_status_offers_a_real_exit_instead_of_the_failing_continue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._rebuilt_job(root, words=self._UNMAPPABLE_WORDS)

            plan = build_recovery_plan(job_file)

            self.assertEqual(plan.category, "manual_fix")
            self.assertEqual(plan.advance_steps, ("import_translation",))
            assert plan.primary_command is not None
            self.assertIn("xvt import-translation", plan.primary_command)
            self.assertNotIn("xvt continue", plan.primary_command)
            self.assertTrue(
                any("xvt translate" in command for command in plan.alternative_commands),
                plan.alternative_commands,
            )
            self.assertFalse(
                any("xvt continue" in command for command in plan.alternative_commands),
                plan.alternative_commands,
            )

    def test_recommended_command_actually_drives_the_job_to_completed(self) -> None:
        """自证推荐命令真的能推进：把 primary_command 原样喂回 CLI。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._rebuilt_job(root, words=self._UNMAPPABLE_WORDS)
            aligned = root / "aligned.srt"
            aligned.write_text(
                "1\n00:00:00,000 --> 00:00:00,500\n你好。\n",
                encoding="utf-8",
            )

            plan = build_recovery_plan(job_file)
            assert plan.primary_command is not None
            argv = shlex.split(plan.primary_command)
            self.assertEqual(argv[:2], ["xvt", "import-translation"])
            self.assertEqual(Path(argv[2]), Path(job_file).resolve())
            self.assertEqual(argv[3:], ["TRANSLATED_SUBTITLE", "--target-language", "zh-CN"])
            # 只把占位符换成真实路径，其余 token 原样喂回 CLI。
            argv[3] = str(aligned)

            with patch(
                "xvt.pipeline.common.probe_media", return_value=self._media_info(root / "input.mp4")
            ):
                self.assertEqual(main(argv[1:]), 0)

            self.assertEqual(build_recovery_plan(job_file).category, "complete")
            self.assertEqual(verify_job(job_file)["status"], "pass")
            self.assertIs(
                load_job(job_file).stages[StageName.TRANSLATE].status,
                StageStatus.SUCCEEDED,
            )

    def test_still_mappable_translation_is_not_pushed_into_manual_fix(self) -> None:
        """反证：映射得上的已存译文不得被归 manual_fix，continue 仍是真出口。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._rebuilt_job(root, words=self._MAPPABLE_WORDS)

            plan = build_recovery_plan(job_file)

            self.assertEqual(plan.category, "retryable")
            assert plan.primary_command is not None
            self.assertIn("xvt continue", plan.primary_command)

            with patch(
                "xvt.pipeline.common.probe_media", return_value=self._media_info(root / "input.mp4")
            ):
                self.assertEqual(main(["continue", str(job_file)]), 0)
            self.assertEqual(build_recovery_plan(job_file).category, "complete")

    def test_continue_failure_message_is_verbatim_the_recovery_diagnosis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._rebuilt_job(root, words=self._UNMAPPABLE_WORDS)
            job = load_job(job_file)
            diagnosis = pending_translation_import_blocker(job, require_workflow_config(job))

            assert diagnosis is not None
            self.assertIn(diagnosis, build_recovery_plan(job_file).reason)
            with (
                patch(
                    "xvt.pipeline.common.probe_media",
                    return_value=self._media_info(root / "input.mp4"),
                ),
                self.assertRaises(ValueError) as raised,
            ):
                continue_workflow(job_file)
            self.assertEqual(str(raised.exception), diagnosis)

    def test_rebuild_source_tail_points_at_the_exit_not_at_continue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = StringIO()
            with redirect_stdout(output):
                self._rebuilt_job(root, words=self._UNMAPPABLE_WORDS)

            printed = output.getvalue()
            self.assertIn("xvt import-translation", printed)
            self.assertNotIn("继续任务: xvt continue", printed)

    def test_blocker_stays_silent_on_the_polished_leg(self) -> None:
        """polished Job 从不重新套用已存译文副本，谓词不得对它出声。

        ``_ensure_translation`` 的 polished 分支永远在自己的块里返回，draft 失效时
        直接 ``run_translation``。对它报 manual_fix 会把一次真能推进的 ``continue``
        判死，并连带让 ``batch-continue`` 把条目记成 failed。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._rebuilt_job(root, words=self._UNMAPPABLE_WORDS)
            job = load_job(job_file)
            standard = require_workflow_config(job)
            self.assertIsNotNone(pending_translation_import_blocker(job, standard))

            polished = replace(standard, translation_quality="polished")

            self.assertIsNone(pending_translation_import_blocker(job, polished))

    def test_blocker_separates_a_missing_file_from_a_timeline_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._rebuilt_job(root, words=self._UNMAPPABLE_WORDS)
            job = load_job(job_file)
            config = require_workflow_config(job)

            mismatch = pending_translation_import_blocker(job, config)
            assert mismatch is not None
            self.assertIn("时间轴", mismatch)

            retained = next(
                item for item in job.artifacts if item.kind == "input_translation_subtitles"
            )
            job.artifacts.remove(retained)
            save_job(job)
            Path(str(config.translation_subtitles)).unlink()

            missing = pending_translation_import_blocker(load_job(job_file), config)
            assert missing is not None
            self.assertNotEqual(missing, mismatch)
            self.assertIn(str(config.translation_subtitles), missing)


class AtomicPolishedTranslationWaitPrecedenceTest(unittest.TestCase):
    """polished 原子 Job：「润色候选有效」分支不得盖掉在途整轮 Agent 等待。

    ``_build_recovery_plan`` 的原子段里，「润色候选有效，可以重新执行原子翻译命令继续
    语义校对」排在既有的原子等待分支之前。两者在同一个状态上同时成立——
    ``prepare_agent_translation`` 只置 ``TRANSLATE=awaiting_input``，不失效
    ``translation_polished_cues``——于是 ``status`` 推荐的是 ``xvt translate``，而它在
    Agent 模式下只会把 manifest 原样重写一遍（零推进的死循环），用户当初显式传过
    ``--batch-size`` 时还会把 manifest 的 batch size 与 fingerprint 一起改掉，手里
    那份准备好的响应随之作废。真正能推进的是 ``apply-translation``。
    """

    _SOURCE = (
        "1\n00:00:00,000 --> 00:00:02,000\nHello world.\n\n"
        "2\n00:00:02,500 --> 00:00:04,500\nThis is a test subtitle.\n\n"
        "3\n00:00:05,000 --> 00:00:07,000\nThe quick brown fox jumps.\n"
    )

    def setUp(self) -> None:
        # `apply-translation` 在原子 polished Job 上会接着跑语义校对，而 resolve_provider
        # 的 base_url / model / API Key 三项都能从环境变量里取。开发机或 CI 配齐这三项时
        # 这条链路会真的发网络请求，因此这里把它们清空，整条链路钉在 Agent 模式。
        patcher = patch.dict(
            "os.environ",
            {
                "XVT_TRANSLATION_BASE_URL": "",
                "XVT_TRANSLATION_MODEL": "",
                "XVT_TRANSLATION_API_KEY": "",
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _atomic_polished_job(self, root: Path) -> Path:
        """真实 CLI 跑出一个 polished 原子 Job，停在整轮 Agent 翻译等待上。

        零网络零 API：纯字幕输入 + ``--provider agent``。原子 Job（没有持久化
        ``config.workflow``）只能由旧版本 xvt 落盘，这里按仓库既有做法（见
        ``tests/test_pipeline_semantic.py``）把 workflow 目标摘掉、改写成顶层键。
        """

        source = root / "src.srt"
        source.write_text(self._SOURCE, encoding="utf-8")
        output = root / "out"
        with redirect_stdout(StringIO()):
            self.assertEqual(
                main(
                    [
                        "run",
                        str(source),
                        "--output",
                        str(output),
                        "--target-language",
                        "zh-CN",
                        "--translation-quality",
                        "polished",
                        "--provider",
                        "agent",
                    ]
                ),
                0,
            )
        job_file = next(output.rglob("job.json"))
        job = load_job(job_file)
        workflow = job.config.pop("workflow")
        job.config["target_language"] = workflow["target_language"]
        job.config["translation_quality"] = workflow["translation_quality"]
        save_job(job)
        return job_file

    def _agent_response(self, job_file: Path, path: Path) -> Path:
        """按当前 manifest 生成一份合法回填响应（绑定它的 fingerprint）。"""

        manifest_path = valid_artifact_path(load_job(job_file), "translation_batches")
        assert manifest_path is not None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload = {
            "items": [
                {"id": item["id"], "text": f"译文第{item['id']}句。"}
                for batch in manifest["batches"]
                for item in batch["items"]
            ],
            "job_id": manifest["job_id"],
            "task": manifest["task"],
            "task_version": manifest["task_version"],
            "fingerprint": manifest["fingerprint"],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def _drop_semantic_report(self, job_file: Path) -> None:
        """语义报告失效：``semantic_review_target`` 变 None，两条语义等待分支都让位。

        现实对应「产物被删 / 磁盘损坏 / 语义校对在落盘前中断」，恢复侧只认内容
        checksum，文件不在就等于没有这份报告。
        """

        report = valid_artifact_path(load_job(job_file), "translation_semantic_qa")
        assert report is not None
        report.unlink()

    def _apply_recommended_command(self, plan, response: Path) -> None:
        """把恢复计划吐出来的那条命令原样喂回 CLI，只替换 RESPONSE 占位符。"""

        self.assertEqual(plan.category, "awaiting_input")
        self.assertIsNone(plan.primary_command)
        self.assertEqual(len(plan.prerequisite_commands), 1, plan.prerequisite_commands)
        argv = shlex.split(plan.prerequisite_commands[0])
        self.assertEqual(argv[:2], ["xvt", "apply-translation"])
        self.assertEqual(argv[3], "RESPONSE")
        self.assertEqual(argv[4:], ["--target-language", "zh-CN"])
        argv[3] = str(response)
        with redirect_stdout(StringIO()):
            self.assertEqual(main(argv[1:]), 0)

    def _semantic_wait_reached(self, job_file: Path) -> bool:
        plan = build_recovery_plan(job_file)
        return plan.category == "awaiting_input" and any(
            "apply-semantic-review" in command for command in plan.prerequisite_commands
        )

    def test_pending_round_keeps_its_manifest_and_its_prepared_response(self) -> None:
        """用户显式 ``--batch-size`` 重开一轮：推荐命令不得改掉 manifest 指纹。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._atomic_polished_job(root)
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    main(
                        [
                            "apply-translation",
                            str(job_file),
                            str(self._agent_response(job_file, root / "round1.json")),
                            "--target-language",
                            "zh-CN",
                        ]
                    ),
                    0,
                )
            self._drop_semantic_report(job_file)
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    main(
                        [
                            "translate",
                            str(job_file),
                            "--target-language",
                            "zh-CN",
                            "--provider",
                            "agent",
                            "--batch-size",
                            "2",
                        ]
                    ),
                    0,
                )
            manifest_path = valid_artifact_path(load_job(job_file), "translation_batches")
            assert manifest_path is not None
            pending = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(pending["batch_size"], 2)
            response = self._agent_response(job_file, root / "round2.json")

            self._apply_recommended_command(build_recovery_plan(job_file), response)

            # manifest 一字未动，Job 真的推进到了语义校对等待。
            self.assertEqual(json.loads(manifest_path.read_text(encoding="utf-8")), pending)
            self.assertTrue(self._semantic_wait_reached(job_file))

    def test_already_applied_manifest_is_not_a_zero_progress_loop(self) -> None:
        """同一道闸门的第二种形态：manifest 已回填过，推荐命令必须仍能推进。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._atomic_polished_job(root)
            response = self._agent_response(job_file, root / "round1.json")
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    main(
                        [
                            "apply-translation",
                            str(job_file),
                            str(response),
                            "--target-language",
                            "zh-CN",
                        ]
                    ),
                    0,
                )
            self._drop_semantic_report(job_file)

            self._apply_recommended_command(build_recovery_plan(job_file), response)

            self.assertTrue(self._semantic_wait_reached(job_file))

    def test_polished_candidate_branch_still_fires_without_a_pending_wait(self) -> None:
        """反证：闸门只拦在途等待，``TRANSLATE`` 不在等待态时原分支一字不变地生效。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._atomic_polished_job(root)
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    main(
                        [
                            "apply-translation",
                            str(job_file),
                            str(self._agent_response(job_file, root / "round1.json")),
                            "--target-language",
                            "zh-CN",
                        ]
                    ),
                    0,
                )
            self._drop_semantic_report(job_file)
            job = load_job(job_file)
            job.stages[StageName.TRANSLATE].status = StageStatus.SUCCEEDED
            save_job(job)

            plan = build_recovery_plan(job_file)

            self.assertEqual(plan.category, "retryable")
            self.assertIn("润色候选有效", plan.reason)
            assert plan.primary_command is not None
            self.assertIn("--translation-quality polished", plan.primary_command)

    # ---- 语义等待让路：只在整轮请求「真的是最新」时让位给 apply-translation ----

    def _apply_translation(self, job_file: Path, response: Path) -> None:
        with redirect_stdout(StringIO()):
            self.assertEqual(
                main(
                    [
                        "apply-translation",
                        str(job_file),
                        str(response),
                        "--target-language",
                        "zh-CN",
                    ]
                ),
                0,
            )

    def _normal_semantic_wait_job(self, root: Path) -> Path:
        """S1：初译已回填，Job 停在正常的 Agent 语义校对等待上。"""

        job_file = self._atomic_polished_job(root)
        self._apply_translation(job_file, self._agent_response(job_file, root / "round1.json"))
        self.assertTrue(self._semantic_wait_reached(job_file))
        return job_file

    def _semantic_response(self, job_file: Path, path: Path) -> Path:
        job = load_job(job_file)
        report_path = valid_artifact_path(job, "translation_semantic_qa")
        assert report_path is not None
        report = json.loads(report_path.read_text(encoding="utf-8"))
        payload = {
            "fingerprint": report["fingerprint"],
            "initial_results": [{"batch": 1, "issues": []}],
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_superseding_full_round_beats_a_still_valid_semantic_wait(self) -> None:
        """S2：语义报告仍有效，但用户又开了新一轮整轮请求 -> 推荐按新 manifest 回填。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._normal_semantic_wait_job(root)
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    main(
                        [
                            "translate",
                            str(job_file),
                            "--target-language",
                            "zh-CN",
                            "--provider",
                            "agent",
                            "--batch-size",
                            "2",
                        ]
                    ),
                    0,
                )
            job = load_job(job_file)
            self.assertIsNotNone(valid_artifact_path(job, "translation_semantic_qa"))
            manifest_path = valid_artifact_path(job, "translation_batches")
            assert manifest_path is not None
            self.assertEqual(json.loads(manifest_path.read_text(encoding="utf-8"))["batch_size"], 2)

            response = self._agent_response(job_file, root / "round2.json")
            payload = json.loads(response.read_text(encoding="utf-8"))
            for item in payload["items"]:
                item["text"] = f"二轮{item['id']}"
            response.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            self._apply_recommended_command(build_recovery_plan(job_file), response)

            # 推进回语义等待，且候选是新一轮（而不是第一轮）的内容。
            self.assertTrue(self._semantic_wait_reached(job_file))
            candidate = valid_artifact_path(load_job(job_file), "translation_polished_cues")
            assert candidate is not None
            self.assertIn("二轮1", candidate.read_text(encoding="utf-8"))

    def test_normal_semantic_wait_is_not_yielded_to_the_bare_full_round_predicate(self) -> None:
        """S1 护栏：正常语义等待上整轮谓词恒真，但整轮请求不是最新，必须仍推荐语义回填。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._normal_semantic_wait_job(root)
            plan = build_recovery_plan(job_file)
            self.assertEqual(plan.category, "awaiting_input")
            self.assertEqual(len(plan.prerequisite_commands), 1, plan.prerequisite_commands)
            argv = shlex.split(plan.prerequisite_commands[0])
            self.assertEqual(argv[:2], ["xvt", "apply-semantic-review"])

            self.assertIsNone(valid_artifact_path(load_job(job_file), "translated_cues"))
            response = self._semantic_response(job_file, root / "semantic.json")
            argv[argv.index("RESPONSE")] = str(response)
            with redirect_stdout(StringIO()):
                self.assertEqual(main(argv[1:]), 0)

            # 走通到出成品：译文落盘、语义等待消失（TRANSLATE 阶段的 awaiting 残留是
            # 原子 Job 既有表象，与本组判定无关，这里不断言它）。
            job = load_job(job_file)
            self.assertIsNotNone(valid_artifact_path(job, "translated_cues"))
            self.assertIsNotNone(valid_artifact_path(job, "translated_srt"))
            self.assertFalse(self._semantic_wait_reached(job_file))

    def test_superseding_full_round_beats_a_pending_human_semantic_review(self) -> None:
        """人工 review 分支（``semantic_review_waiting``）同口径让路。

        用 ``source_suspect`` 构造带 issue 且无需修复的真实报告：回填后 Job 停在人工
        review 上；此时再开一轮整轮请求，推荐必须改成按新 manifest 回填。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._normal_semantic_wait_job(root)
            report_path = valid_artifact_path(load_job(job_file), "translation_semantic_qa")
            assert report_path is not None
            fingerprint = json.loads(report_path.read_text(encoding="utf-8"))["fingerprint"]
            issue = {
                "category": "source_suspect",
                "cue_ids": [1],
                "severity": "review",
                "message": "源文可疑。",
                "suggested_items": [],
            }
            review = root / "semantic-issue.json"
            review.write_text(
                json.dumps(
                    {
                        "fingerprint": fingerprint,
                        "initial_results": [{"batch": 1, "issues": [issue]}],
                    }
                ),
                encoding="utf-8",
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(main(["apply-semantic-review", str(job_file), str(review)]), 0)
            plan = build_recovery_plan(job_file)
            self.assertEqual(plan.category, "awaiting_input")
            self.assertNotIn("apply-translation", " ".join(plan.prerequisite_commands))

            with redirect_stdout(StringIO()):
                self.assertEqual(
                    main(
                        [
                            "translate",
                            str(job_file),
                            "--target-language",
                            "zh-CN",
                            "--provider",
                            "agent",
                            "--batch-size",
                            "2",
                        ]
                    ),
                    0,
                )
            response = self._agent_response(job_file, root / "round2.json")
            self._apply_recommended_command(build_recovery_plan(job_file), response)
            self.assertTrue(self._semantic_wait_reached(job_file))


_THREE_CUE_SOURCE = (
    "1\n00:00:00,000 --> 00:00:02,000\nHello world.\n\n"
    "2\n00:00:02,500 --> 00:00:04,500\nThis is a test subtitle.\n\n"
    "3\n00:00:05,000 --> 00:00:07,000\nThe quick brown fox jumps.\n"
)


def _quiet_cli(testcase: unittest.TestCase, argv: list[str]) -> None:
    with redirect_stdout(StringIO()):
        testcase.assertEqual(main(argv), 0)


def _clear_translation_env(testcase: unittest.TestCase) -> None:
    """翻译链路钉在 Agent 模式，防止开发机 / CI 配齐三项环境变量后真的发请求。"""

    patcher = patch.dict(
        "os.environ",
        {
            "XVT_TRANSLATION_BASE_URL": "",
            "XVT_TRANSLATION_MODEL": "",
            "XVT_TRANSLATION_API_KEY": "",
        },
    )
    patcher.start()
    testcase.addCleanup(patcher.stop)


class FullTranslationRequestNewestValidityTest(unittest.TestCase):
    """整轮请求「是否最新」只在**有效**的取代 kind 之间比登记次序。

    登记仍在、文件已失效（被删 / 损坏）的重译 manifest 背后不可能挂着活等待，却会
    因为登记在 ``translation_batches`` 之后而把一次有效、可回填的整轮等待盖成「完成」。
    """

    def setUp(self) -> None:
        _clear_translation_env(self)

    def _cli(self, argv: list[str]) -> None:
        _quiet_cli(self, argv)

    def _response(self, job_file: Path, path: Path, *, prefix: str) -> Path:
        manifest_path = valid_artifact_path(load_job(job_file), "translation_batches")
        assert manifest_path is not None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload = {
            "items": [
                {"id": item["id"], "text": f"{prefix}{item['id']}"}
                for batch in manifest["batches"]
                for item in batch["items"]
            ],
            "job_id": manifest["job_id"],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def _satisfied_job(self, root: Path) -> Path:
        source = root / "src.srt"
        source.write_text(_THREE_CUE_SOURCE, encoding="utf-8")
        output = root / "out"
        self._cli(
            [
                "run",
                str(source),
                "--output",
                str(output),
                "--target-language",
                "zh-CN",
                "--provider",
                "agent",
            ]
        )
        job_file = next(output.rglob("job.json"))
        first = self._response(job_file, root / "r1.json", prefix="一轮")
        self._cli(["apply", str(job_file), str(first)])
        job = load_job(job_file)
        self.assertEqual(plan_advance_steps(job, require_workflow_config(job)), ())
        return job_file

    def _request_cue_retranslation(self, job_file: Path) -> None:
        self._cli(
            [
                "retranslate-cues",
                str(job_file),
                "--cue",
                "2",
                "--target-language",
                "zh-CN",
                "--provider",
                "agent",
            ]
        )

    def _deceptive_job(self, root: Path) -> Path:
        """满足的 Job → 第二轮整轮 → 局部重译 → 重译 manifest 文件被删（登记仍在）。"""

        job_file = self._satisfied_job(root)
        self._cli(["translate", str(job_file), "--target-language", "zh-CN", "--provider", "agent"])
        self._request_cue_retranslation(job_file)
        job = load_job(job_file)
        self.assertEqual(
            plan_advance_steps(job, require_workflow_config(job)), ("await_cue_retranslation",)
        )
        manifest = valid_artifact_path(job, "cue_retranslation_batches")
        assert manifest is not None
        manifest.unlink()
        job = load_job(job_file)
        self.assertIn("cue_retranslation_batches", [a.kind for a in job.artifacts])
        self.assertIsNone(valid_artifact_path(job, "cue_retranslation_batches"))
        return job_file

    def test_stale_cue_retranslation_registration_keeps_the_full_round_in_the_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._deceptive_job(Path(directory))
            job = load_job(job_file)
            self.assertEqual(
                plan_advance_steps(job, require_workflow_config(job)),
                ("await_agent_translation",),
            )

    def test_stale_cue_retranslation_registration_does_not_let_continue_complete(self) -> None:
        events: list[dict] = []
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._deceptive_job(Path(directory))
            result = continue_workflow(job_file, event_sink=events.append)
        self.assertEqual(result.status, "awaiting_input")
        self.assertNotIn(EventType.WORKFLOW_COMPLETED.value, [e.type for e in events])

    def test_recovery_command_for_the_full_round_wait_really_advances_the_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._deceptive_job(root)
            plan = build_recovery_plan(job_file)
            self.assertEqual(plan.category, "awaiting_input")
            self.assertEqual(plan.advance_steps, ("await_agent_translation",))

            # 把计划吐出的命令原样喂回 CLI：Job 真的推进到第二轮译文。
            self.assertEqual(len(plan.prerequisite_commands), 1, plan.prerequisite_commands)
            argv = shlex.split(plan.prerequisite_commands[0])
            self.assertEqual(argv[:2], ["xvt", "apply"])
            response = self._response(job_file, root / "r2.json", prefix="二轮")
            argv[argv.index("RESPONSE")] = str(response)
            self._cli(argv[1:])
            job = load_job(job_file)
            self.assertIs(job.stages[StageName.TRANSLATE].status, StageStatus.SUCCEEDED)
            translated = valid_artifact_path(job, "translated_cues")
            assert translated is not None
            self.assertIn("二轮1", translated.read_text(encoding="utf-8"))

    def test_valid_newer_cue_retranslation_request_still_supersedes(self) -> None:
        """对照：重译 manifest 有效时，更晚登记的它仍取代整轮请求。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._deceptive_job(root)
            # 复位成有效形态：重新发起一次局部重译（登记在整轮之后，文件有效）。
            self._request_cue_retranslation(job_file)
            job = load_job(job_file)
            self.assertIsNotNone(valid_artifact_path(job, "cue_retranslation_batches"))
            self.assertEqual(
                plan_advance_steps(job, require_workflow_config(job)),
                ("await_cue_retranslation",),
            )


class AlignmentPostQaDeadlockTest(unittest.TestCase):
    """强制对齐被**对齐后字幕 QA 的 error 门**拒绝时，导航必须给出真出口。

    这是「`status` 推荐一条必然失败的命令」的第 9 次：`--align-subtitles` 的第二种失败
    （对齐置信度达标、对齐后 QA 出 error 级 issue）此前照旧落到「存在 checksum 有效的
    转写 checkpoint，可以从断点继续」+ `primary_command=xvt continue`，而 `continue`
    拿的是同一份 `input_subtitles` 与同一份 `word_transcript`，对齐与 QA 都是确定性的，
    实测连跑两次 stderr 逐字相同、退出码都是 2。

    判据只看 Job 状态：对齐后 QA 的结论现在登记为 `aligned_subtitle_qa`
    （`aligned-subtitle-qa.json`，checksum 有效，在抛出之前就落盘），不需要猜运行期异常。
    """

    def _add_artifact(self, job, root: Path, kind: str, content: str = "{}") -> Path:
        path = root / f"{kind}.json"
        path.write_text(content, encoding="utf-8")
        job.artifacts.append(Artifact(kind, str(path), file_checksum(path)))
        return path

    _ERROR_REPORT = json.dumps(
        {
            "status": "failed",
            "cue_count": 4,
            "issues": [
                {
                    "code": "outside_media",
                    "severity": "error",
                    "cue_id": 4,
                    "message": "cue is outside media duration",
                },
                {
                    "code": "low_timing_confidence",
                    "severity": "review",
                    "cue_id": 4,
                    "message": "timeline requires manual review",
                },
            ],
            "layout": None,
        }
    )
    _WARNING_REPORT = json.dumps(
        {
            "status": "warning",
            "cue_count": 4,
            "issues": [
                {
                    "code": "too_short",
                    "severity": "warning",
                    "cue_id": 2,
                    "message": "cue is shorter than 600ms",
                }
            ],
            "layout": None,
        }
    )

    def _post_qa_deadlock_job(
        self,
        root: Path,
        *,
        report: str | None = None,
        confidence: float = 0.75,
        job_input: str = "/tmp/input.mp4",
        **config_overrides,
    ) -> Path:
        """构造「对齐后 QA 出 error」的断点态：转写 checkpoint 全在，只缺 source_cues。"""

        config = {
            "engine": "mlx",
            "source_language": "en",
            "source_strategy": "auto",
            "align_subtitles": True,
            "minimum_alignment_confidence": 0.55,
            "allow_low_confidence_alignment": False,
            "workflow": {"target_language": "zh-CN", "burn": False},
        }
        config.update(config_overrides)
        job = create_job(job_input, root, config)
        self._add_artifact(job, root, "media_info")
        self._add_artifact(job, root, "normalized_audio")
        self._add_artifact(job, root, "word_transcript")
        self._add_artifact(job, root, "input_subtitles")
        self._add_artifact(
            job, root, "subtitle_alignment", json.dumps({"confidence": confidence, "cues": []})
        )
        if report is not None:
            self._add_artifact(job, root, "aligned_subtitle_qa", report)
        job.stages[StageName.ALIGN].status = StageStatus.FAILED
        job.stages[StageName.ALIGN].error = (
            "aligned subtitles failed subtitle QA with error-level issues; rerun without "
            "--align-subtitles to keep the source subtitle timeline as-is, use "
            "--source-strategy asr to build the timeline from speech instead, or fix the "
            "source subtitles"
        )
        return save_job(job)

    def test_post_qa_error_does_not_recommend_a_doomed_continue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = build_recovery_plan(self._post_qa_deadlock_job(root, report=self._ERROR_REPORT))

        self.assertEqual(plan.category, "manual_fix")
        self.assertIn("rebuild-source", plan.primary_command or "")
        self.assertIn("--strategy asr", plan.primary_command or "")
        commands = (plan.primary_command or "", *plan.alternative_commands)
        self.assertFalse(
            any(command.split()[1:2] == ["continue"] for command in commands),
            f"注定失败的 continue 不得出现在恢复计划里: {commands}",
        )

    def test_reason_and_report_paths_point_at_the_registered_report(self) -> None:
        """明细是「对齐后 QA 报告落盘」刚解锁的能力：reason 点名报告、report_paths 给出绝对路径。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._post_qa_deadlock_job(root, report=self._ERROR_REPORT)
            report_path = valid_artifact_path(load_job(job_file), "aligned_subtitle_qa")

            plan = build_recovery_plan(job_file)

        self.assertIn("aligned-subtitle-qa.json", plan.reason)
        self.assertIn("--align-subtitles", plan.reason)
        assert report_path is not None
        self.assertEqual(plan.report_paths, (str(report_path),))

    def test_warning_only_aligned_report_is_still_retryable(self) -> None:
        """反证：对齐后 QA 只有 warning 时这条判定不得生效。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = build_recovery_plan(
                self._post_qa_deadlock_job(root, report=self._WARNING_REPORT)
            )

        self.assertEqual(plan.category, "retryable")
        self.assertIn("continue", plan.primary_command or "")
        self.assertEqual(plan.report_paths, ())

    def test_detection_requires_both_alignment_inputs(self) -> None:
        """缺一份对齐输入时重跑可能得到不同结果，不能断言「必然失败」。"""

        for missing in ("word_transcript", "input_subtitles"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                job_file = self._post_qa_deadlock_job(root, report=self._ERROR_REPORT)
                job = load_job(job_file)
                job.artifacts = [artifact for artifact in job.artifacts if artifact.kind != missing]
                save_job(job)

                plan = build_recovery_plan(job_file)

                self.assertEqual(plan.category, "retryable")
                self.assertIn("continue", plan.primary_command or "")

    def test_allow_low_confidence_flag_does_not_mask_the_post_qa_gate(self) -> None:
        """`--allow-low-confidence-alignment` 与这条无关：它放行不了 QA error 门。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = build_recovery_plan(
                self._post_qa_deadlock_job(
                    root,
                    report=self._ERROR_REPORT,
                    allow_low_confidence_alignment=True,
                )
            )

        self.assertEqual(plan.category, "manual_fix")
        self.assertIn("rebuild-source", plan.primary_command or "")

    def test_confidence_gate_keeps_its_own_reason_when_both_gates_trip(self) -> None:
        """两道门同时成立时，先抛出的置信度门保留自己的文案，不被本条抢走。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = build_recovery_plan(
                self._post_qa_deadlock_job(
                    root,
                    report=self._ERROR_REPORT,
                    confidence=0.231,
                )
            )

        self.assertEqual(plan.category, "manual_fix")
        self.assertIn("置信度", plan.reason)
        self.assertNotIn("aligned-subtitle-qa.json", plan.reason)
        self.assertEqual(plan.report_paths, ())

    def test_job_without_forced_alignment_is_left_alone(self) -> None:
        """`align_subtitles` 不为 True 时（自动策略会回落 ASR）不得被判死。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = build_recovery_plan(
                self._post_qa_deadlock_job(
                    root,
                    report=self._ERROR_REPORT,
                    align_subtitles=False,
                )
            )

        self.assertEqual(plan.category, "retryable")
        self.assertIn("continue", plan.primary_command or "")

    def test_unrebuildable_job_is_not_pointed_at_a_rebuild_that_cannot_run(self) -> None:
        """出口跑不起来时不出声：比置信度门多的那一道守卫。

        ``media_info`` 失效且本地媒体不在盘上时 `rebuild-source` 自己会以
        「source rebuild requires an available local media file」失败——推荐一条跑不起来
        的命令正是这条判定要修的毛病。这种 Job 维持本改动之前的既有去向。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._post_qa_deadlock_job(
                root,
                report=self._ERROR_REPORT,
                job_input=str(root / "gone.mp4"),
            )
            job = load_job(job_file)
            job.artifacts = [
                artifact for artifact in job.artifacts if artifact.kind != "media_info"
            ]
            save_job(job)

            plan = build_recovery_plan(job_file)

        self.assertNotEqual(plan.category, "manual_fix")
        self.assertNotIn("rebuild-source", plan.primary_command or "")
        self.assertEqual(plan.report_paths, ())

    def test_subtitle_only_job_is_left_alone(self) -> None:
        """纯字幕 Job 没有 ASR 重建出口，这条判定不得对它出声。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._post_qa_deadlock_job(
                root,
                report=self._ERROR_REPORT,
                input_kind="subtitle",
            )

            plan = build_recovery_plan(job_file)

        self.assertEqual(plan.category, "retryable")
        self.assertIn("continue", plan.primary_command or "")


class AlignmentPostQaDeadlockExitTest(unittest.TestCase):
    """自证推荐命令真能推进：全程真实 CLI，只打桩 ffprobe / ffmpeg 抽音 / ASR 模型。

    实证基线（真实媒体 + 真实 mlx-whisper，见 HANDOFF）：`xvt run … --align-subtitles`
    退出码 2，`status` 给 `retryable` + `continue`，连跑两次 `continue` 退出码都是 2 且
    stderr 逐字相同；`xvt rebuild-source --strategy asr` 退出码 0 后 Job 走到 complete、
    `xvt verify` 全项 PASS。本测试把同一条链路搬进默认测试套件。
    """

    def _media_info(self, media: Path) -> MediaInfo:
        return MediaInfo(
            path=str(media),
            duration_ms=3_000,
            size_bytes=media.stat().st_size,
            format_name="mp4",
            width=1920,
            height=1080,
            frame_rate=30.0,
            video_codec="h264",
            audio_codec="aac",
            has_video=True,
            has_audio=True,
        )

    # 三条能匹配上的 cue 挤在 0–0.7s，外加一条音频里根本没说的尾句：
    # 对齐把时间轴放大 2.8 倍，尾句被推到媒体时长之外（outside_media，error 级），
    # 而覆盖率 3/4 × 匹配分 1.0 = 0.75 仍高于默认门限 0.55，置信度门不参与。
    _WORDS = (
        Word("Hello", 0, 600, 0.9),
        Word("world", 700, 1_300, 0.9),
        Word("again", 1_400, 2_000, 0.9),
    )
    _SOURCE_SRT = (
        "1\n00:00:00,000 --> 00:00:00,200\nHello\n\n"
        "2\n00:00:00,250 --> 00:00:00,450\nworld\n\n"
        "3\n00:00:00,500 --> 00:00:00,700\nagain\n\n"
        "4\n00:00:01,200 --> 00:00:01,500\nGoodbye forever\n"
    )

    def _deadlocked_job(self, root: Path) -> Path:
        media = root / "input.mp4"
        media.write_bytes(b"video")
        source = root / "source.srt"
        source.write_text(self._SOURCE_SRT, encoding="utf-8")
        media_info = self._media_info(media)

        def run_ffmpeg(args, **_kwargs):
            Path(args[-1]).write_bytes(b"audio")
            return CommandResult(tuple(args), 0, "", "")

        with (
            patch("xvt.pipeline.common.probe_media", return_value=media_info),
            patch("xvt.pipeline.transcription.local.probe_media", return_value=media_info),
            patch("xvt.pipeline.transcription.local.run_command", side_effect=run_ffmpeg),
            patch(
                "xvt.pipeline.transcription.local.transcribe_audio",
                return_value=Transcript(language="en", engine="mlx", words=self._WORDS),
            ),
        ):
            # 强制对齐被对齐后 QA 的 error 门拒绝：退出码 2，ALIGN 记 failed。
            self.assertEqual(
                main(
                    [
                        "run",
                        str(media),
                        "--output",
                        str(root / "work"),
                        "--subtitles",
                        str(source),
                        "--align-subtitles",
                        "--language",
                        "en",
                        "--target-language",
                        "zh-CN",
                        "--provider",
                        "agent",
                    ]
                ),
                2,
            )
        job_file = next((root / "work").rglob("job.json"))
        job = load_job(job_file)
        self.assertIs(job.stages[StageName.ALIGN].status, StageStatus.FAILED)
        self.assertIsNone(valid_artifact_path(job, "source_cues"))
        report_path = valid_artifact_path(job, "aligned_subtitle_qa")
        self.assertIsNotNone(report_path)
        assert report_path is not None
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(
            [
                (issue["code"], issue["cue_id"])
                for issue in report["issues"]
                if issue["severity"] == "error"
            ],
            [("outside_media", 4)],
        )
        return job_file

    def test_continue_is_a_dead_end_and_is_not_recommended(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._deadlocked_job(root)
            media_info = self._media_info(root / "input.mp4")

            # `continue` 连跑两次：退出码与 Job 状态都不变，导航零推进。
            with (
                patch("xvt.pipeline.common.probe_media", return_value=media_info),
                patch("xvt.pipeline.transcription.local.probe_media", return_value=media_info),
            ):
                for _ in range(2):
                    self.assertEqual(main(["continue", str(job_file)]), 2)
                    job = load_job(job_file)
                    self.assertIs(job.stages[StageName.ALIGN].status, StageStatus.FAILED)
                    self.assertIsNone(valid_artifact_path(job, "source_cues"))

            plan = build_recovery_plan(job_file)

            self.assertEqual(plan.category, "manual_fix")
            assert plan.primary_command is not None
            self.assertIn("xvt rebuild-source", plan.primary_command)
            self.assertNotIn("xvt continue", plan.primary_command)
            self.assertFalse(
                any("xvt continue" in command for command in plan.alternative_commands),
                plan.alternative_commands,
            )

    def test_recommended_command_actually_drives_the_job_to_completed(self) -> None:
        """把 `primary_command` 原样 `shlex.split` 后喂回 CLI，断言 Job 真的走到完成。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._deadlocked_job(root)
            media_info = self._media_info(root / "input.mp4")

            plan = build_recovery_plan(job_file)
            assert plan.primary_command is not None
            argv = shlex.split(plan.primary_command)
            self.assertEqual(argv[:2], ["xvt", "rebuild-source"])
            self.assertEqual(Path(argv[2]), Path(job_file).resolve())
            self.assertEqual(argv[3:], ["--strategy", "asr"])

            with (
                patch("xvt.pipeline.common.probe_media", return_value=media_info),
                patch("xvt.pipeline.transcription.resume.probe_media", return_value=media_info),
            ):
                self.assertEqual(main(argv[1:]), 0)

                rebuilt = load_job(job_file)
                self.assertIs(rebuilt.stages[StageName.ALIGN].status, StageStatus.SKIPPED)
                self.assertIsNotNone(valid_artifact_path(rebuilt, "source_cues"))
                self.assertIs(rebuilt.config["align_subtitles"], False)

                # 重建之后的导航回到常规翻译推进，Agent 回填后 Job 走到完成。
                after_rebuild = build_recovery_plan(job_file)
                self.assertEqual(after_rebuild.category, "retryable")
                self.assertEqual(main(["continue", str(job_file)]), 0)

                manifest_path = valid_artifact_path(load_job(job_file), "translation_batches")
                assert manifest_path is not None
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                response = root / "response.json"
                response.write_text(
                    json.dumps(
                        {
                            "items": [
                                {"id": item["id"], "text": "你好。"}
                                for batch in manifest["batches"]
                                for item in batch["items"]
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                self.assertEqual(main(["apply", str(job_file), str(response), "--continue"]), 0)

            self.assertEqual(build_recovery_plan(job_file).category, "complete")
            self.assertEqual(verify_job(job_file)["status"], "pass")
            self.assertIs(
                load_job(job_file).stages[StageName.TRANSLATE].status,
                StageStatus.SUCCEEDED,
            )

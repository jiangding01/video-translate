import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xvt.job_store import create_job, get_job_file, load_job, save_job
from xvt.models import Artifact, StageName, StageStatus
from xvt.pipeline import TranslationRunResult, apply_subtitle_translation
from xvt.recovery import file_checksum
from xvt.translate import GlossaryTerm, TranslationPolicy
from xvt.workflow import (
    WorkflowOptions,
    _burn_satisfied,
    _record_burn_fingerprint,
    _stored_burn_fingerprint,
    continue_workflow,
    plan_advance_steps,
    planned_run_steps,
    run_workflow,
    workflow_goals,
)
from xvt.workflow_config import WorkflowConfig


class WorkflowTest(unittest.TestCase):
    def test_automatic_translation_and_burn_run_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job("/tmp/video.mp4", directory)
            events = []
            burned_path = Path(directory) / "burned.mp4"

            def fake_burn(job_file, **_kwargs):
                # 模拟真实烧录：写出成品并在 Job 上登记 checksum 有效的 artifact。
                burned_path.write_bytes(b"video")
                burned_job = load_job(job_file)
                burned_job.artifacts.append(
                    Artifact("burned_video", str(burned_path), file_checksum(burned_path))
                )
                save_job(burned_job)
                return burned_path

            with (
                patch("xvt.workflow.run_transcription", return_value=job) as transcribe,
                patch(
                    "xvt.workflow.run_translation",
                    return_value=TranslationRunResult(
                        "openai-compatible",
                        (Path("/tmp/translated.srt"), Path("/tmp/bilingual.ass")),
                    ),
                ) as translate,
                patch(
                    "xvt.workflow.burn_translated_job",
                    side_effect=fake_burn,
                ) as burn,
            ):
                result = run_workflow(
                    WorkflowOptions(
                        input="/tmp/video.mp4",
                        output=directory,
                        target_language="zh-CN",
                        translation_style="natural",
                        segmentation_preset="compact",
                        source_strategy="asr",
                        burn=True,
                    ),
                    event_sink=events.append,
                )

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.job_file, get_job_file(job))
        self.assertEqual(result.artifacts[-1], burned_path)
        self.assertFalse(transcribe.call_args.kwargs["emit_job_completed"])
        workflow_config = transcribe.call_args.kwargs["workflow_config"]
        self.assertEqual(workflow_config["target_language"], "zh-CN")
        self.assertEqual(workflow_config["translation_policy"], {"style": "natural", "terms": []})
        self.assertEqual(workflow_config["segmentation_preset"], "compact")
        self.assertEqual(transcribe.call_args.kwargs["segmentation_preset"], "compact")
        self.assertEqual(transcribe.call_args.kwargs["source_strategy"], "asr")
        self.assertEqual(
            workflow_config["requested_outputs"],
            ["translated_subtitles", "bilingual_subtitles", "burned_video"],
        )
        translate.assert_called_once()
        self.assertEqual(translate.call_args.kwargs["policy"], TranslationPolicy(style="natural"))
        burn.assert_called_once()
        self.assertEqual(events[0].type, "workflow_started")
        self.assertIsNone(events[0].data["job_id"])
        self.assertIsNone(events[0].data["job_file"])
        self.assertEqual(events[1].data["steps"], ["transcribe", "translate", "burn"])
        self.assertEqual(events[-1].type, "workflow_completed")
        self.assertEqual(events[-1].data["job_id"], job.id)
        self.assertEqual(events[-1].data["job_file"], str(get_job_file(job)))

    def test_workflow_failed_uses_error_envelope(self) -> None:
        events = []
        with patch("xvt.workflow.run_transcription", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                run_workflow(
                    WorkflowOptions(input="/tmp/video.mp4", output="/tmp/output"),
                    event_sink=events.append,
                )

        failed = events[-1]
        self.assertEqual(failed.type, "workflow_failed")
        self.assertEqual(failed.data["error"], {"category": "workflow", "message": "boom"})
        self.assertNotIn("error_type", failed.data)
        self.assertIsNone(failed.data["job_id"])
        self.assertIsNone(failed.data["job_file"])

    def test_agent_fallback_waits_and_does_not_burn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job("/tmp/video.mp4", directory)
            events = []
            with (
                patch("xvt.workflow.run_transcription", return_value=job),
                patch(
                    "xvt.workflow.run_translation",
                    return_value=TranslationRunResult(
                        "agent",
                        (Path("/tmp/translation-batches.json"),),
                        "missing API configuration",
                    ),
                ),
                patch("xvt.workflow.burn_translated_job") as burn,
            ):
                result = run_workflow(
                    WorkflowOptions(
                        input="/tmp/video.mp4",
                        output=directory,
                        target_language="zh-CN",
                        burn=True,
                    ),
                    event_sink=events.append,
                )

        self.assertEqual(result.status, "awaiting_input")
        self.assertIn("missing API", result.message or "")
        burn.assert_not_called()
        self.assertEqual(events[-1].type, "workflow_waiting")

    def test_external_translation_skips_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job("/tmp/video.mp4", directory)
            with (
                patch("xvt.workflow.run_transcription", return_value=job),
                patch(
                    "xvt.workflow.apply_subtitle_translation",
                    return_value=(Path("/tmp/bilingual.srt"),),
                ) as apply_translation,
                patch("xvt.workflow.run_translation") as translate,
            ):
                result = run_workflow(
                    WorkflowOptions(
                        input="/tmp/video.mp4",
                        output=directory,
                        subtitles="/tmp/source.srt",
                        translation_subtitles="/tmp/translated.srt",
                        target_language="zh-CN",
                    )
                )

        self.assertEqual(result.status, "completed")
        apply_translation.assert_called_once()
        translate.assert_not_called()

    def test_burn_requires_target_language(self) -> None:
        with self.assertRaisesRegex(ValueError, "--burn requires --target-language"):
            run_workflow(WorkflowOptions(input="/tmp/video.mp4", output="/tmp/output", burn=True))

    def test_subtitle_input_rejects_media_only_options(self) -> None:
        with self.assertRaisesRegex(ValueError, "--align-subtitles requires media input"):
            run_workflow(
                WorkflowOptions(
                    input="/tmp/source.srt",
                    output="/tmp/output",
                    align_subtitles=True,
                )
            )
        with self.assertRaisesRegex(ValueError, "--burn requires media input"):
            run_workflow(
                WorkflowOptions(
                    input="/tmp/source.srt",
                    output="/tmp/output",
                    target_language="zh-CN",
                    burn=True,
                )
            )

    def test_source_strategy_rejects_asr_for_subtitle_input_and_alignment(self) -> None:
        with self.assertRaisesRegex(ValueError, "--source-strategy asr requires media input"):
            WorkflowOptions(
                input="/tmp/source.srt", output="/tmp/output", source_strategy="asr"
            ).validate()
        with self.assertRaisesRegex(
            ValueError, "--source-strategy asr cannot be combined with --align-subtitles"
        ):
            WorkflowOptions(
                input="/tmp/video.mp4",
                output="/tmp/output",
                subtitles="/tmp/source.srt",
                source_strategy="asr",
                align_subtitles=True,
            ).validate()

    def test_subtitle_workflow_imports_translation_without_media(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,500\nHello world.\n",
                encoding="utf-8",
            )
            translated = root / "translated.srt"
            translated.write_text(
                "1\n00:00:00,000 --> 00:00:01,500\n你好，世界。\n",
                encoding="utf-8",
            )

            result = run_workflow(
                WorkflowOptions(
                    input=str(source),
                    output=root / "output",
                    language="en",
                    target_language="zh-CN",
                    translation_subtitles=translated,
                )
            )
            artifacts = {artifact.kind for artifact in load_job(result.job_file).artifacts}

        self.assertEqual(result.status, "completed")
        self.assertEqual(
            {path.name for path in result.artifacts},
            {"source.srt", "translated.srt", "bilingual.srt"},
        )
        self.assertNotIn("media_info", artifacts)
        self.assertIn("bilingual_srt", artifacts)

    def test_subtitle_workflow_continues_after_agent_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,500\nHello world.\n",
                encoding="utf-8",
            )
            translated = root / "translated.srt"
            translated.write_text(
                "1\n00:00:00,000 --> 00:00:01,500\n你好，世界。\n",
                encoding="utf-8",
            )

            waiting = run_workflow(
                WorkflowOptions(
                    input=str(source),
                    output=root / "output",
                    language="en",
                    target_language="zh-CN",
                    provider="agent",
                )
            )
            self.assertEqual(waiting.status, "awaiting_input")
            apply_subtitle_translation(
                waiting.job_file,
                translated,
                target_language="zh-CN",
            )
            completed = continue_workflow(waiting.job_file)

        self.assertEqual(completed.status, "completed")
        self.assertEqual(
            {path.name for path in completed.artifacts},
            {"source.srt", "translated.srt", "bilingual.srt"},
        )

    def test_rejects_unknown_segmentation_preset(self) -> None:
        with self.assertRaisesRegex(ValueError, "--segmentation-preset"):
            run_workflow(
                WorkflowOptions(
                    input="/tmp/video.mp4",
                    output="/tmp/output",
                    segmentation_preset="unknown",
                )
            )

    def test_continue_waits_for_agent_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job(
                "/tmp/video.mp4",
                directory,
                {"workflow": {"target_language": "zh-CN", "burn": True}},
            )
            job.stages[StageName.TRANSLATE].status = StageStatus.AWAITING_INPUT
            save_job(job)
            with patch("xvt.workflow.valid_artifact_path") as valid:
                valid.side_effect = lambda _job, kind: (
                    Path("/tmp/source-cues.json")
                    if kind in {"source_cues", "source_srt"}
                    else (
                        Path("/tmp/translation-batches.json")
                        if kind == "translation_batches"
                        else None
                    )
                )
                result = continue_workflow(get_job_file(job))

        self.assertEqual(result.status, "awaiting_input")
        self.assertEqual(result.artifacts, (Path("/tmp/translation-batches.json"),))

    def test_continue_reuses_persisted_translation_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job(
                "/tmp/video.mp4",
                directory,
                {
                    "workflow": {
                        "target_language": "zh-CN",
                        "translation_policy": {
                            "style": "natural",
                            "terms": [{"source": "OpenAI", "target": "OpenAI"}],
                        },
                        "burn": False,
                    }
                },
            )
            save_job(job)
            with (
                patch(
                    "xvt.workflow.valid_artifact_path",
                    side_effect=lambda _job, kind: (
                        Path("/tmp/source-cues.json")
                        if kind in {"source_cues", "source_srt"}
                        else None
                    ),
                ),
                patch(
                    "xvt.workflow.run_translation",
                    return_value=TranslationRunResult(
                        "openai-compatible", (Path("/tmp/translated.srt"),)
                    ),
                ) as translate,
            ):
                result = continue_workflow(get_job_file(job))

        self.assertEqual(result.status, "completed")
        self.assertEqual(
            translate.call_args.kwargs["policy"],
            TranslationPolicy(
                style="natural",
                terms=(GlossaryTerm("OpenAI", "OpenAI"),),
            ),
        )

    def test_continue_defaults_translation_policy_for_old_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job(
                "/tmp/video.mp4",
                directory,
                {"workflow": {"target_language": "zh-CN", "burn": False}},
            )
            save_job(job)
            with (
                patch(
                    "xvt.workflow.valid_artifact_path",
                    side_effect=lambda _job, kind: (
                        Path("/tmp/source-cues.json")
                        if kind in {"source_cues", "source_srt"}
                        else None
                    ),
                ),
                patch(
                    "xvt.workflow.run_translation",
                    return_value=TranslationRunResult(
                        "openai-compatible", (Path("/tmp/translated.srt"),)
                    ),
                ) as translate,
            ):
                result = continue_workflow(get_job_file(job))

        self.assertEqual(result.status, "completed")
        self.assertEqual(translate.call_args.kwargs["policy"], TranslationPolicy())

    def test_continue_burns_after_agent_translation_was_applied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job(
                "/tmp/video.mp4",
                directory,
                {
                    "workflow": {
                        "target_language": "zh-CN",
                        "burn": True,
                        "hardware_encoding": True,
                    }
                },
            )
            job.stages[StageName.TRANSLATE].status = StageStatus.AWAITING_INPUT
            save_job(job)
            with (
                patch("xvt.workflow.valid_artifact_path") as valid,
                patch(
                    "xvt.workflow.burn_translated_job",
                    return_value=Path("/tmp/burned.mp4"),
                ) as burn,
                patch("xvt.workflow._record_burn_fingerprint"),
            ):
                valid.side_effect = lambda _job, kind: (
                    Path(f"/tmp/{kind}.json")
                    if kind
                    in {
                        "source_cues",
                        "source_srt",
                        "translated_cues",
                        "translated_srt",
                        "bilingual_srt",
                    }
                    else None
                )
                result = continue_workflow(get_job_file(job))

        self.assertEqual(result.status, "completed")
        burn.assert_called_once()

    def test_continue_does_not_duplicate_current_burn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job(
                "/tmp/video.mp4",
                directory,
                {
                    "workflow": {
                        "target_language": "zh-CN",
                        "burn": True,
                        "burn_fingerprint": "current",
                    }
                },
            )
            job.stages[StageName.MEDIA_QA].status = StageStatus.SUCCEEDED
            save_job(job)
            with (
                patch("xvt.workflow.valid_artifact_path") as valid,
                patch("xvt.workflow._burn_fingerprint", return_value="current"),
                patch("xvt.workflow.burn_translated_job") as burn,
            ):
                valid.side_effect = lambda _job, kind: (
                    Path(f"/tmp/{kind}")
                    if kind
                    in {
                        "source_cues",
                        "source_srt",
                        "translated_cues",
                        "translated_srt",
                        "bilingual_srt",
                        "burned_video",
                    }
                    else None
                )
                result = continue_workflow(get_job_file(job))

        self.assertEqual(result.status, "completed")
        burn.assert_not_called()

    def test_continue_retries_burn_when_media_qa_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job(
                "/tmp/video.mp4",
                directory,
                {
                    "workflow": {
                        "target_language": "zh-CN",
                        "burn": True,
                        "burn_fingerprint": "current",
                    }
                },
            )
            job.stages[StageName.MEDIA_QA].status = StageStatus.FAILED
            save_job(job)
            with (
                patch("xvt.workflow.valid_artifact_path") as valid,
                patch("xvt.workflow._burn_fingerprint", return_value="current"),
                patch(
                    "xvt.workflow.burn_translated_job",
                    return_value=Path("/tmp/burned.mp4"),
                ) as burn,
                patch("xvt.workflow._record_burn_fingerprint"),
            ):
                valid.side_effect = lambda _job, kind: (
                    Path(f"/tmp/{kind}")
                    if kind
                    in {
                        "source_cues",
                        "source_srt",
                        "translated_cues",
                        "translated_srt",
                        "bilingual_srt",
                        "burned_video",
                    }
                    else None
                )
                result = continue_workflow(get_job_file(job))

        self.assertEqual(result.status, "completed")
        burn.assert_called_once()

    def test_continue_renders_srt_from_cues_after_review_without_retranslating(self) -> None:
        """译文 QA needs_review 阻断后：acknowledge + continue 必须写出 SRT，且不重打翻译 API。"""
        import json
        from dataclasses import asdict

        from xvt.models import Cue
        from xvt.pipeline import acknowledge_subtitle_review

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:02,000\nHello world.\n",
                encoding="utf-8",
            )
            job = create_job(
                str(source),
                root,
                {
                    "input_kind": "subtitle",
                    "workflow": {"target_language": "zh-CN", "burn": False},
                },
            )
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir(parents=True)

            source_cue = Cue(1, 0, 2000, "Hello world.", source_language="en")
            translated_cue = Cue(
                1,
                0,
                2000,
                "Hello world.",
                "你好世界。",
                source_language="en",
                target_language="zh-CN",
            )
            source_cues_path = artifacts / "source-cues.json"
            translated_cues_path = artifacts / "translated-cues.json"
            source_srt_path = artifacts / "source.srt"
            qa_path = artifacts / "qa-translated.json"
            source_cues_path.write_text(
                json.dumps([asdict(source_cue)], ensure_ascii=False),
                encoding="utf-8",
            )
            translated_cues_path.write_text(
                json.dumps([asdict(translated_cue)], ensure_ascii=False),
                encoding="utf-8",
            )
            source_srt_path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            qa_path.write_text(
                json.dumps(
                    {
                        "status": "needs_review",
                        "cue_count": 1,
                        "issues": [
                            {
                                "code": "bilingual_overflow_risk",
                                "severity": "review",
                                "cue_id": 1,
                                "message": "wide",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            job.artifacts.extend(
                [
                    Artifact("source_cues", str(source_cues_path), file_checksum(source_cues_path)),
                    Artifact(
                        "translated_cues",
                        str(translated_cues_path),
                        file_checksum(translated_cues_path),
                    ),
                    Artifact("source_srt", str(source_srt_path), file_checksum(source_srt_path)),
                    Artifact(
                        "translated_subtitle_qa",
                        str(qa_path),
                        file_checksum(qa_path),
                    ),
                ]
            )
            job.stages[StageName.TRANSLATE].status = StageStatus.SUCCEEDED
            job.stages[StageName.SUBTITLE_QA].status = StageStatus.FAILED
            job.stages[StageName.SUBTITLE_QA].error = "subtitle QA needs_review"
            job_file = save_job(job)

            acknowledge_subtitle_review(job_file)
            with (
                patch("xvt.workflow.run_translation") as translate,
                patch("xvt.workflow.resume_remote_transcription") as remote,
            ):
                result = continue_workflow(job_file)

            self.assertEqual(result.status, "completed")
            translate.assert_not_called()
            remote.assert_not_called()
            updated = load_job(job_file)
            kinds = {artifact.kind for artifact in updated.artifacts}
            self.assertIn("translated_srt", kinds)
            self.assertIn("bilingual_srt", kinds)
            self.assertEqual(updated.stages[StageName.SUBTITLE_QA].status, StageStatus.SUCCEEDED)
            self.assertTrue((artifacts / "translated.srt").is_file())
            self.assertTrue((artifacts / "bilingual.srt").is_file())

    def test_source_only_workflow_reports_source_subtitle_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job("/tmp/video.mp4", directory)
            source_srt = get_job_file(job).parent / "source.srt"
            source_srt.write_text("source", encoding="utf-8")
            job.artifacts.append(Artifact("source_srt", str(source_srt), file_checksum(source_srt)))
            save_job(job)
            events = []
            with patch("xvt.workflow.run_transcription", return_value=job):
                result = run_workflow(
                    WorkflowOptions(input="/tmp/video.mp4", output=directory),
                    event_sink=events.append,
                )

        self.assertEqual(result.artifacts, (source_srt,))
        self.assertEqual(events[-1].data["artifacts"], [str(source_srt)])

    def test_continue_stops_at_failed_subtitle_qa(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job(
                "/tmp/video.mp4",
                directory,
                {"workflow": {"target_language": "zh-CN", "burn": False}},
            )
            job.stages[StageName.SUBTITLE_QA].status = StageStatus.FAILED
            save_job(job)
            with (
                patch(
                    "xvt.workflow.valid_artifact_path",
                    return_value=Path("/tmp/source-cues.json"),
                ),
                patch("xvt.workflow.run_translation") as translate,
                patch("xvt.workflow.complete_translated_outputs") as complete,
            ):
                with self.assertRaisesRegex(RuntimeError, "字幕 QA"):
                    continue_workflow(get_job_file(job))

        translate.assert_not_called()
        complete.assert_not_called()

    def test_continue_retries_early_remote_acquisition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job(
                "https://youtu.be/abc",
                directory,
                {"workflow": {"target_language": None, "burn": False}},
            )
            job.stages[StageName.INSPECT].status = StageStatus.FAILED
            save_job(job)
            with (
                patch("xvt.workflow.resume_remote_transcription") as resume_remote,
                patch("xvt.workflow.resume_job") as resume,
            ):
                result = continue_workflow(get_job_file(job))

        self.assertEqual(result.status, "completed")
        resume_remote.assert_called_once()
        resume.assert_not_called()


class BurnFingerprintArtifactTest(unittest.TestCase):
    def test_record_writes_artifact_and_clears_workflow_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "video.mp4"
            media.write_bytes(b"media")
            job = create_job(str(media), directory)
            job.config["workflow"] = {
                "target_language": "zh-CN",
                "burn": True,
                "burn_fingerprint": "legacy-should-be-cleared",
                "hardware_encoding": True,
            }
            cues = Path(directory) / "translated-cues.json"
            cues.write_text("[]", encoding="utf-8")
            job.artifacts.append(Artifact("translated_cues", str(cues), file_checksum(cues)))
            job_file = save_job(job)

            _record_burn_fingerprint(job_file)
            job = load_job(job_file)

            self.assertIsNone(job.config["workflow"].get("burn_fingerprint"))
            stored = _stored_burn_fingerprint(job)
            self.assertIsNotNone(stored)
            self.assertTrue(any(item.kind == "burn_fingerprint" for item in job.artifacts))
            path = next(item.path for item in job.artifacts if item.kind == "burn_fingerprint")
            self.assertTrue(Path(path).is_file())

    def test_stored_fingerprint_falls_back_to_workflow_field(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job("/tmp/video.mp4", directory)
            job.config["workflow"] = {
                "target_language": "zh-CN",
                "burn": True,
                "burn_fingerprint": "legacy-fp",
            }
            save_job(job)
            self.assertEqual(_stored_burn_fingerprint(job), "legacy-fp")

    def test_burn_satisfied_uses_stored_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "video.mp4"
            media.write_bytes(b"media")
            job = create_job(str(media), directory)
            job.config["workflow"] = {
                "target_language": "zh-CN",
                "burn": True,
                "hardware_encoding": True,
            }
            cues = Path(directory) / "translated-cues.json"
            cues.write_text("[]", encoding="utf-8")
            burned = Path(directory) / "burned.mp4"
            burned.write_bytes(b"out")
            job.artifacts.extend(
                [
                    Artifact("translated_cues", str(cues), file_checksum(cues)),
                    Artifact("burned_video", str(burned), file_checksum(burned)),
                ]
            )
            job.stages[StageName.MEDIA_QA].status = StageStatus.SUCCEEDED
            job_file = save_job(job)
            _record_burn_fingerprint(job_file)
            job = load_job(job_file)
            config = WorkflowConfig.from_dict(job.config["workflow"])

            self.assertTrue(_burn_satisfied(job, config))
            # 破坏指纹文件内容但保留登记 → checksum 失效 → 视为未满足。
            fingerprint_path = Path(
                next(item.path for item in job.artifacts if item.kind == "burn_fingerprint")
            )
            fingerprint_path.write_text('{"fingerprint": "tampered"}\n', encoding="utf-8")
            self.assertFalse(_burn_satisfied(job, config))


class WorkflowPlannerTest(unittest.TestCase):
    def test_workflow_goals_from_config_flags(self) -> None:
        goals = workflow_goals(
            WorkflowConfig(target_language="zh-CN", burn=True, requested_outputs=("burned_video",))
        )
        self.assertTrue(goals.needs_translation)
        self.assertTrue(goals.needs_burn)
        goals_source_only = workflow_goals(WorkflowConfig())
        self.assertFalse(goals_source_only.needs_translation)
        self.assertFalse(goals_source_only.needs_burn)

    def test_plan_advance_steps_orders_translate_then_burn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job(
                "/tmp/video.mp4",
                directory,
                {"workflow": {"target_language": "zh-CN", "burn": True}},
            )
            save_job(job)
            with patch(
                "xvt.workflow.valid_artifact_path",
                return_value=None,
            ):
                self.assertEqual(
                    plan_advance_steps(job, WorkflowConfig(target_language="zh-CN", burn=True)),
                    ("translate", "burn"),
                )

    def test_plan_advance_steps_awaits_agent_without_burn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job(
                "/tmp/video.mp4",
                directory,
                {"workflow": {"target_language": "zh-CN", "burn": True}},
            )
            job.stages[StageName.TRANSLATE].status = StageStatus.AWAITING_INPUT
            save_job(job)
            with patch("xvt.workflow.valid_artifact_path", return_value=None):
                self.assertEqual(
                    plan_advance_steps(job, WorkflowConfig(target_language="zh-CN", burn=True)),
                    ("await_agent_translation",),
                )

    def test_plan_advance_steps_skips_satisfied_burn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job(
                "/tmp/video.mp4",
                directory,
                {
                    "workflow": {
                        "target_language": "zh-CN",
                        "burn": True,
                        "burn_fingerprint": "fp",
                    }
                },
            )
            job.stages[StageName.MEDIA_QA].status = StageStatus.SUCCEEDED
            save_job(job)
            config = WorkflowConfig(
                target_language="zh-CN",
                burn=True,
                burn_fingerprint="fp",
            )
            with (
                patch(
                    "xvt.workflow.valid_artifact_path",
                    side_effect=lambda _job, kind: (
                        Path(f"/tmp/{kind}")
                        if kind
                        in {
                            "translated_cues",
                            "translated_srt",
                            "bilingual_srt",
                            "burned_video",
                        }
                        else None
                    ),
                ),
                patch("xvt.workflow._burn_fingerprint", return_value="fp"),
            ):
                self.assertEqual(plan_advance_steps(job, config), ())

    def test_plan_advance_steps_render_when_cues_exist_without_srt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job(
                "/tmp/video.mp4",
                directory,
                {"workflow": {"target_language": "zh-CN", "burn": True}},
            )
            save_job(job)
            with patch(
                "xvt.workflow.valid_artifact_path",
                side_effect=lambda _job, kind: (
                    Path(f"/tmp/{kind}") if kind == "translated_cues" else None
                ),
            ):
                self.assertEqual(
                    plan_advance_steps(job, WorkflowConfig(target_language="zh-CN", burn=True)),
                    ("render", "burn"),
                )

    def test_plan_advance_steps_import_translation_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job("/tmp/video.mp4", directory)
            save_job(job)
            config = WorkflowConfig(
                target_language="zh-CN",
                translation_subtitles="/tmp/translated.srt",
                burn=False,
            )
            with patch("xvt.workflow.valid_artifact_path", return_value=None):
                self.assertEqual(plan_advance_steps(job, config), ("import_translation",))

    def test_planned_run_steps_matches_event_contract(self) -> None:
        self.assertEqual(
            planned_run_steps(
                WorkflowOptions(
                    input="/tmp/video.mp4",
                    output="/tmp/out",
                    target_language="zh-CN",
                    burn=True,
                )
            ),
            ["transcribe", "translate", "burn"],
        )
        self.assertEqual(
            planned_run_steps(
                WorkflowOptions(
                    input="/tmp/source.srt",
                    output="/tmp/out",
                    target_language="zh-CN",
                    translation_subtitles="/tmp/zh.srt",
                )
            ),
            ["import_subtitles", "import_translation"],
        )


if __name__ == "__main__":
    unittest.main()

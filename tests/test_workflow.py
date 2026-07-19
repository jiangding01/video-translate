import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xvt.job_store import create_job, get_job_file, load_job, save_job
from xvt.models import Artifact, StageName, StageStatus
from xvt.pipeline import TranslationRunResult, apply_subtitle_translation
from xvt.recovery import file_checksum
from xvt.translate import GlossaryTerm, TranslationPolicy
from xvt.workflow import WorkflowOptions, continue_workflow, run_workflow


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
                        Path("/tmp/source-cues.json") if kind in {"source_cues", "source_srt"} else None
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
                        Path("/tmp/source-cues.json") if kind in {"source_cues", "source_srt"} else None
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
                    if kind in {"source_cues", "source_srt", "translated_cues"}
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
                    if kind in {"source_cues", "source_srt", "translated_cues", "burned_video"}
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
                    if kind in {"source_cues", "source_srt", "translated_cues", "burned_video"}
                    else None
                )
                result = continue_workflow(get_job_file(job))

        self.assertEqual(result.status, "completed")
        burn.assert_called_once()

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
            ):
                with self.assertRaisesRegex(RuntimeError, "subtitle QA failed"):
                    continue_workflow(get_job_file(job))

        translate.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()

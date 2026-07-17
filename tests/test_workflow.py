import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xvt.job_store import create_job, get_job_file, save_job
from xvt.models import StageName, StageStatus
from xvt.pipeline import TranslationRunResult
from xvt.translate import GlossaryTerm, TranslationPolicy
from xvt.workflow import WorkflowOptions, continue_workflow, run_workflow


class WorkflowTest(unittest.TestCase):
    def test_automatic_translation_and_burn_run_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job("/tmp/video.mp4", directory)
            events = []
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
                    return_value=Path("/tmp/burned.mp4"),
                ) as burn,
            ):
                result = run_workflow(
                    WorkflowOptions(
                        input="/tmp/video.mp4",
                        output=directory,
                        target_language="zh-CN",
                        translation_style="natural",
                        burn=True,
                    ),
                    event_sink=events.append,
                )

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.job_file, get_job_file(job))
        self.assertEqual(result.artifacts[-1], Path("/tmp/burned.mp4"))
        self.assertFalse(transcribe.call_args.kwargs["emit_job_completed"])
        workflow_config = transcribe.call_args.kwargs["workflow_config"]
        self.assertEqual(workflow_config["target_language"], "zh-CN")
        self.assertEqual(workflow_config["translation_policy"], {"style": "natural", "terms": []})
        self.assertEqual(
            workflow_config["requested_outputs"],
            ["translated_subtitles", "bilingual_subtitles", "burned_video"],
        )
        translate.assert_called_once()
        self.assertEqual(translate.call_args.kwargs["policy"], TranslationPolicy(style="natural"))
        burn.assert_called_once()
        self.assertEqual(events[0].type, "workflow_started")
        self.assertEqual(events[1].data["steps"], ["transcribe", "translate", "burn"])
        self.assertEqual(events[-1].type, "workflow_completed")

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
                    if kind == "source_cues"
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
                        Path("/tmp/source-cues.json") if kind == "source_cues" else None
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
                    if kind in {"source_cues", "translated_cues"}
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
            save_job(job)
            with (
                patch("xvt.workflow.valid_artifact_path") as valid,
                patch("xvt.workflow._burn_fingerprint", return_value="current"),
                patch("xvt.workflow.burn_translated_job") as burn,
            ):
                valid.side_effect = lambda _job, kind: (
                    Path(f"/tmp/{kind}")
                    if kind in {"source_cues", "translated_cues", "burned_video"}
                    else None
                )
                result = continue_workflow(get_job_file(job))

        self.assertEqual(result.status, "completed")
        burn.assert_not_called()

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

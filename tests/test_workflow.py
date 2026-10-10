import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from xvt.errors import UserInputError
from xvt.events import EventType
from xvt.job_store import create_job, get_job_directory, get_job_file, load_job, save_job
from xvt.media import MediaInfo
from xvt.models import Artifact, StageName, StageStatus, Transcript, Word
from xvt.pipeline import (
    TranslationRunResult,
    apply_agent_semantic_review,
    apply_agent_translation,
    apply_cue_retranslation,
    apply_subtitle_translation,
    burn_translated_job,
    cue_retranslation_waiting,
    prepare_agent_semantic_review,
    prepare_agent_translation,
    retranslate_cues,
    run_local_transcription,
    run_subtitle_transcription,
    run_translation_polish,
    source_review_input_path,
    translation_review_input_path,
)
from xvt.pipeline.common import load_cues
from xvt.process import CommandResult
from xvt.recovery import build_recovery_plan, file_checksum, valid_artifact_path, verify_job
from xvt.translate import GlossaryTerm, SemanticIssue, TranslationPolicy
from xvt.translation_provider import TranslationProviderError
from xvt.workflow import (
    WorkflowOptions,
    _burn_satisfied,
    _record_burn_fingerprint,
    _stored_burn_fingerprint,
    _translation_review_waiting_reason,
    burned_output_relink_recovery_available,
    continue_workflow,
    plan_advance_steps,
    planned_run_steps,
    relink_burned_output,
    run_workflow,
    workflow_goals,
)
from xvt.workflow_config import (
    WorkflowConfig,
    get_workflow_config,
    require_workflow_config,
    set_workflow_config,
)


class WorkflowTest(unittest.TestCase):
    def test_polished_api_stub_runs_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello.\n",
                encoding="utf-8",
            )
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch(
                    "xvt.pipeline.translation.translate_batch",
                    return_value={1: "草稿"},
                ) as translate,
                patch(
                    "xvt.pipeline.polish.polish_batch",
                    return_value={1: "润色"},
                ) as polish,
                patch(
                    "xvt.pipeline.semantic.semantic_review_batch",
                    return_value=(),
                ) as semantic_review,
            ):
                result = run_workflow(
                    WorkflowOptions(
                        input=str(source),
                        output=root / "output",
                        target_language="zh-CN",
                        translation_quality="polished",
                        provider="openai-compatible",
                        base_url="https://example.com/v1",
                        model="model",
                        api_key_env="TEST_TRANSLATION_API_KEY",
                    )
                )

            job = load_job(result.job_file)
            kinds = {artifact.kind for artifact in job.artifacts}
            self.assertEqual(result.status, "completed")
            self.assertEqual(job.config["workflow"]["translation_quality"], "polished")
            self.assertTrue(
                {
                    "translation_draft_cues",
                    "translation_polish_progress",
                    "translation_polished_cues",
                    "translated_cues",
                    "translated_srt",
                    "bilingual_srt",
                    "translated_subtitle_qa",
                }
                <= kinds
            )
            translate.assert_called_once()
            polish.assert_called_once()
            semantic_review.assert_called_once()

    def test_continue_reuses_translation_progress_after_environment_model_changes(self) -> None:
        # 端到端：run 首批成功、次批 4xx 中断后，换一个 XVT_TRANSLATION_MODEL 不同的终端
        # 跑真实 continue 路径。provider 身份已固化进 Job，指纹不变：首批零调用复用，
        # 只补跑次批。
        provider_env = {
            "XVT_TRANSLATION_BASE_URL": "https://provider.example.com/v1",
            "XVT_TRANSLATION_MODEL": "model-a",
            "XVT_TRANSLATION_API_KEY": "secret",
        }
        stale_env = {**provider_env, "XVT_TRANSLATION_MODEL": "model-stale"}
        seen: list[tuple[int, str]] = []

        def first_run(batch, config, *, correction=None, **_kwargs):
            seen.append((batch.id, config.model))
            if batch.id != 1:
                raise TranslationProviderError(
                    "translation provider returned HTTP 400", retryable=False
                )
            return {item.id: "你好。" for item in batch.items}

        def continued_run(batch, config, *, correction=None, **_kwargs):
            seen.append((batch.id, config.model))
            return {item.id: "世界。" for item in batch.items}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello.\n\n"
                "2\n00:00:03,000 --> 00:00:04,000\nWorld.\n",
                encoding="utf-8",
            )
            options = WorkflowOptions(
                input=str(source),
                output=root / "output",
                target_language="zh-CN",
                batch_size=1,
            )
            with (
                patch.dict("os.environ", provider_env),
                patch("xvt.pipeline.translation.translate_batch", side_effect=first_run),
                self.assertRaises(TranslationProviderError),
            ):
                run_workflow(options)
            job_file = next((root / "output" / "jobs").glob("*/job.json"))
            persisted = get_workflow_config(load_job(job_file))
            assert persisted is not None
            self.assertEqual(persisted.model, "model-a")
            self.assertEqual(seen, [(1, "model-a"), (2, "model-a")])

            with (
                patch.dict("os.environ", stale_env),
                patch("xvt.pipeline.translation.translate_batch", side_effect=continued_run),
            ):
                result = continue_workflow(job_file)

            translated = load_cues(valid_artifact_path(load_job(job_file), "translated_cues"))

        self.assertEqual(result.status, "completed")
        self.assertEqual(seen[2:], [(2, "model-a")])
        # 收尾阶段会按字幕风格去掉句末句号，这里只关心两条译文分别来自首次 run 与 continue。
        self.assertEqual([cue.target_text.rstrip("。") for cue in translated], ["你好", "世界"])

    def test_source_suspect_workflow_exposes_source_first_recovery_and_lazy_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nCloud Code.\n",
                encoding="utf-8",
            )
            issue = SemanticIssue("source_suspect", (1,), "review", "check source", ())
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch("xvt.pipeline.translation.translate_batch", return_value={1: "Cloud Code。"}),
                patch("xvt.pipeline.polish.polish_batch", return_value={1: "Cloud Code。"}),
                patch("xvt.pipeline.semantic.semantic_review_batch", return_value=(issue,)),
            ):
                result = run_workflow(
                    WorkflowOptions(
                        input=str(source),
                        output=root / "output",
                        target_language="zh-CN",
                        translation_quality="polished",
                        provider="openai-compatible",
                        base_url="https://example.com/v1",
                        model="model",
                        api_key_env="TEST_TRANSLATION_API_KEY",
                    )
                )

            job = load_job(result.job_file)
            source_input = source_review_input_path(job, [1])
            translation_input = translation_review_input_path(job)
            assert source_input is not None
            assert translation_input is not None
            plan = build_recovery_plan(job)
            self.assertEqual(result.status, "awaiting_input")
            self.assertEqual(
                plan.input_paths,
                (
                    str(source_input),
                    str(source_input.with_suffix(".srt")),
                    str(translation_input),
                    str(translation_input.with_suffix(".srt")),
                ),
            )
            self.assertIn("apply-source-review-fixes", plan.primary_command or "")
            translation_alternative = next(
                command for command in plan.alternative_commands if "--continue" in command
            )
            self.assertTrue(translation_alternative.startswith("xvt apply "))
            self.assertIn(str(translation_input), translation_alternative)
            self.assertIn(
                "--acknowledge-translation-review",
                " ".join(plan.alternative_commands),
            )

            source_input.unlink()
            before_status = result.job_file.read_bytes()
            legacy_plan = build_recovery_plan(result.job_file)
            self.assertIn("continue", legacy_plan.primary_command or "")
            self.assertNotIn("--cue", legacy_plan.primary_command or "")
            self.assertFalse(source_input.exists())
            self.assertEqual(result.job_file.read_bytes(), before_status)

            continued = continue_workflow(result.job_file)

            self.assertEqual(continued.status, "awaiting_input")
            self.assertIsNotNone(source_review_input_path(load_job(result.job_file), [1]))

    def test_polished_options_validate_and_plan_two_translation_phases(self) -> None:
        options = WorkflowOptions(
            input="/tmp/video.mp4",
            output="/tmp/output",
            target_language="zh-CN",
            translation_quality="polished",
        )

        options.validate()

        self.assertEqual(options.persisted_config().translation_quality, "polished")
        self.assertEqual(
            planned_run_steps(options),
            ["transcribe", "translate", "polish", "semantic_review"],
        )
        with self.assertRaisesRegex(ValueError, "translation-subtitles"):
            WorkflowOptions(
                input="/tmp/video.mp4",
                output="/tmp/output",
                target_language="zh-CN",
                translation_subtitles="/tmp/translated.srt",
                translation_quality="polished",
            ).validate()

    def test_agent_polished_workflow_waits_once_then_renders_without_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello.\n",
                encoding="utf-8",
            )
            job = run_subtitle_transcription(source, root, language="en")
            set_workflow_config(
                job,
                WorkflowConfig(target_language="zh-CN", translation_quality="polished"),
            )
            save_job(job)
            job_file = get_job_file(job)
            manifest_path = prepare_agent_translation(
                job_file,
                source_language="en",
                target_language="zh-CN",
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(
                plan_advance_steps(
                    load_job(job_file),
                    WorkflowConfig.from_dict(job.config["workflow"]),
                ),
                ("await_agent_translation",),
            )
            original_manifest = manifest_path.read_bytes()
            manifest_path.write_bytes(original_manifest + b" ")
            self.assertEqual(
                plan_advance_steps(
                    load_job(job_file),
                    WorkflowConfig.from_dict(job.config["workflow"]),
                ),
                ("translate",),
            )
            manifest_path.write_bytes(original_manifest)

            response = root / "response.json"
            response.write_text(
                json.dumps(
                    {
                        "fingerprint": manifest["fingerprint"],
                        "items": [{"id": 1, "text": "你好。"}],
                    }
                ),
                encoding="utf-8",
            )
            apply_agent_translation(job_file, response, target_language="zh-CN")
            semantic_manifest_path = prepare_agent_semantic_review(job_file)
            semantic_manifest = json.loads(semantic_manifest_path.read_text(encoding="utf-8"))
            semantic_response = root / "semantic-response.json"
            semantic_response.write_text(
                json.dumps(
                    {
                        "fingerprint": semantic_manifest["fingerprint"],
                        "initial_results": [
                            {"batch": batch["id"], "issues": []}
                            for batch in semantic_manifest["batches"]
                        ],
                        "repair_results": [],
                        "final_results": [],
                    }
                ),
                encoding="utf-8",
            )
            apply_agent_semantic_review(job_file, semantic_response)
            completed = load_job(job_file)
            completed.artifacts = [
                artifact
                for artifact in completed.artifacts
                if artifact.kind not in {"translated_srt", "bilingual_srt", "bilingual_ass"}
            ]
            save_job(completed)

            self.assertEqual(
                plan_advance_steps(
                    load_job(job_file), WorkflowConfig.from_dict(job.config["workflow"])
                ),
                ("render",),
            )

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

    def test_glossary_rejects_translation_subtitles_combination(self) -> None:
        """术语表与导入译文互斥：术语校验的语义前提是「模型按表翻的」，导入译文不成立。"""

        with self.assertRaisesRegex(
            ValueError, r"--glossary cannot be combined with --translation-subtitles"
        ):
            WorkflowOptions(
                input="/tmp/source.srt",
                output="/tmp/output",
                target_language="zh-CN",
                translation_subtitles="/tmp/translated.srt",
                glossary="/tmp/glossary.json",
            ).validate()
        # 单独给术语表仍然合法，拒绝只针对组合本身。
        WorkflowOptions(
            input="/tmp/source.srt",
            output="/tmp/output",
            target_language="zh-CN",
            glossary="/tmp/glossary.json",
        ).validate()

    def test_imported_translation_persists_requested_translation_style(self) -> None:
        """导入译文的 Job 也要落策略快照：显式 --translation-style 必须对后续命令生效。"""

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
                    translation_style="natural",
                )
            )
            job = load_job(result.job_file)

        self.assertEqual(result.status, "completed")
        self.assertEqual(
            job.config["workflow"]["translation_policy"],
            {"style": "natural", "terms": []},
        )
        workflow = get_workflow_config(job)
        assert workflow is not None
        assert workflow.translation_policy is not None
        self.assertEqual(workflow.translation_policy.style, "natural")
        self.assertEqual(workflow.translation_policy.terms, ())

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
                        "translated_subtitle_qa",
                    }
                    else None
                )
                result = continue_workflow(get_job_file(job))

        self.assertEqual(result.status, "completed")
        burn.assert_called_once()

    def test_new_burn_layout_review_returns_waiting_without_completion_or_proof(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job(
                "/tmp/video.mp4",
                directory,
                {
                    "workflow": {"target_language": "zh-CN", "burn": True},
                },
            )
            save_job(job)
            events = []

            def blocked_burn(job_file, **_kwargs):
                current = load_job(job_file)
                current.stages[StageName.SUBTITLE_QA].status = StageStatus.AWAITING_INPUT
                save_job(current)
                raise UserInputError("layout requires review")

            with (
                patch(
                    "xvt.workflow.valid_artifact_path",
                    side_effect=lambda _job, kind: (
                        Path(f"/tmp/{kind}.json")
                        if kind
                        in {
                            "source_cues",
                            "source_srt",
                            "translated_cues",
                            "translated_srt",
                            "bilingual_srt",
                            "translated_subtitle_qa",
                        }
                        else None
                    ),
                ),
                patch("xvt.workflow.burn_translated_job", side_effect=blocked_burn),
                patch("xvt.workflow._record_burn_fingerprint") as proof,
            ):
                result = continue_workflow(get_job_file(job), event_sink=events.append)
            self.assertEqual(result.status, "awaiting_input")
            self.assertIn("workflow_waiting", [event.type for event in events])
            self.assertNotIn("workflow_completed", [event.type for event in events])
            proof.assert_not_called()

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
            media_qa = Path(directory) / "media-qa.json"
            media_qa.write_text('{"status":"pass"}\n', encoding="utf-8")
            with (
                patch("xvt.workflow.valid_artifact_path") as valid,
                patch("xvt.workflow._burn_fingerprint", return_value="current"),
                patch("xvt.workflow.burn_translated_job") as burn,
            ):
                valid.side_effect = lambda _job, kind: (
                    media_qa
                    if kind == "media_qa"
                    else Path(f"/tmp/{kind}")
                    if kind
                    in {
                        "source_cues",
                        "source_srt",
                        "translated_cues",
                        "translated_srt",
                        "bilingual_srt",
                        "translated_subtitle_qa",
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
                        "translated_subtitle_qa",
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

    def _standard_job_with_translated_outputs(
        self,
        root: Path,
        target_text: str,
    ) -> tuple[Path, Path]:
        """构造 standard 质量、译文四产物齐备的端到端 Job；返回 (job_file, qa_path)。"""

        from dataclasses import asdict

        from xvt.models import Cue
        from xvt.qa import check_subtitles

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
            target_text,
            source_language="en",
            target_language="zh-CN",
        )
        paths = {
            "source_cues": (artifacts / "source-cues.json", [asdict(source_cue)]),
            "translated_cues": (artifacts / "translated-cues.json", [asdict(translated_cue)]),
        }
        for path, payload in paths.values():
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        source_srt_path = artifacts / "source.srt"
        source_srt_path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        translated_srt_path = artifacts / "translated.srt"
        translated_srt_path.write_text("stale-render", encoding="utf-8")
        bilingual_srt_path = artifacts / "bilingual.srt"
        bilingual_srt_path.write_text("stale-render", encoding="utf-8")
        qa_path = artifacts / "qa-translated.json"
        qa_path.write_text(
            json.dumps(
                asdict(check_subtitles([translated_cue], require_translation=True)),
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        job.artifacts.extend(
            [
                Artifact(kind, str(path), file_checksum(path))
                for kind, (path, _payload) in paths.items()
            ]
        )
        job.artifacts.extend(
            [
                Artifact("source_srt", str(source_srt_path), file_checksum(source_srt_path)),
                Artifact(
                    "translated_srt",
                    str(translated_srt_path),
                    file_checksum(translated_srt_path),
                ),
                Artifact(
                    "bilingual_srt",
                    str(bilingual_srt_path),
                    file_checksum(bilingual_srt_path),
                ),
                Artifact("translated_subtitle_qa", str(qa_path), file_checksum(qa_path)),
            ]
        )
        for stage in (
            StageName.IMPORT_SUBTITLES,
            StageName.TRANSLATE,
            StageName.SUBTITLE_QA,
            StageName.RENDER,
        ):
            job.stages[stage].status = StageStatus.SUCCEEDED
        return save_job(job), qa_path

    def test_atomic_burn_layout_failure_preserves_workflow_qa_and_recovery(self) -> None:
        for duration, width, height, font, target_width in (
            (500, 640, 360, None, 40),
            (4000, 360, 640, None, 600),
            (4000, 640, 360, "Wide Font", 600),
        ):
            with self.subTest(duration=duration, width=width, font=font):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    job_file, qa_path = self._standard_job_with_translated_outputs(root, "你好世界")
                    original = root / "original.mp4"
                    original.write_bytes(b"original")
                    alternate = root / "alternate.mp4"
                    alternate.write_bytes(b"alternate")
                    selected_media = original if font else alternate
                    job = load_job(job_file)
                    job.input = str(original)
                    job.config["input_kind"] = "video"
                    job.config["workflow"]["burn"] = True
                    save_job(job)
                    before_job, before_qa = job_file.read_bytes(), qa_path.read_bytes()
                    info = MediaInfo(
                        str(selected_media),
                        duration,
                        5,
                        "mp4",
                        width,
                        height,
                        30,
                        "h264",
                        "aac",
                        True,
                        True,
                    )
                    with (
                        patch("xvt.pipeline.burnin.probe_media", return_value=info),
                        patch(
                            "xvt.qa.measure_bilingual_line_widths",
                            return_value={
                                (1, "target"): (target_width, 492),
                                (1, "source"): (55, 492),
                            },
                        ),
                        patch("xvt.pipeline.burnin.burn_subtitles") as burn,
                        self.assertRaises(UserInputError),
                    ):
                        burn_translated_job(job_file, media=selected_media, target_font=font)
                    burn.assert_not_called()
                    self.assertEqual(qa_path.read_bytes(), before_qa)
                    self.assertEqual(job_file.read_bytes(), before_job)
                    with (
                        patch("xvt.workflow.burn_translated_job") as workflow_burn,
                        patch("xvt.workflow._record_burn_fingerprint"),
                    ):
                        result = continue_workflow(job_file)
                    self.assertEqual(result.status, "completed")
                    workflow_burn.assert_called_once()
                    self.assertIsNone(workflow_burn.call_args.kwargs["media"])
                    self.assertTrue(workflow_burn.call_args.kwargs["persist_workflow_qa"])

    def test_continue_rebuilds_translated_qa_report_without_retranslating(self) -> None:
        """译文 QA 报告单独失效必须能被 continue 零 API 成本重建，且二次 continue 幂等。

        修复前：三产物有效 → plan 为空 → continue 报 completed 什么也不修，而
        burn/preview 门又因缺有效 QA 报告拒绝放行，形成闭环死锁。
        """

        config = WorkflowConfig(target_language="zh-CN", burn=False)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, qa_path = self._standard_job_with_translated_outputs(root, "你好世界。")

            # 只让 QA 报告失效：cue 与两份 SRT 全部保持有效。
            qa_path.write_text("{}", encoding="utf-8")
            stale = load_job(job_file)
            self.assertIsNone(valid_artifact_path(stale, "translated_subtitle_qa"))
            self.assertIsNotNone(valid_artifact_path(stale, "translated_cues"))
            self.assertIsNotNone(valid_artifact_path(stale, "translated_srt"))
            self.assertIsNotNone(valid_artifact_path(stale, "bilingual_srt"))
            self.assertEqual(plan_advance_steps(stale, config), ("render",))

            with (
                patch("xvt.workflow.run_translation") as translate,
                patch("xvt.workflow.apply_subtitle_translation") as import_translation,
            ):
                result = continue_workflow(job_file)

            self.assertEqual(result.status, "completed")
            translate.assert_not_called()
            import_translation.assert_not_called()

            rebuilt = load_job(job_file)
            rebuilt_qa = valid_artifact_path(rebuilt, "translated_subtitle_qa")
            self.assertIsNotNone(rebuilt_qa)
            self.assertEqual(
                json.loads(rebuilt_qa.read_text(encoding="utf-8"))["status"],
                "pass",
            )
            self.assertEqual(rebuilt.stages[StageName.SUBTITLE_QA].status, StageStatus.SUCCEEDED)
            # 幂等：重建后目标已满足，再次 continue 不再有任何步骤。
            self.assertEqual(plan_advance_steps(rebuilt, config), ())
            with patch("xvt.workflow.run_translation") as translate_again:
                repeated = continue_workflow(job_file)
            self.assertEqual(repeated.status, "completed")
            translate_again.assert_not_called()
            self.assertEqual(plan_advance_steps(load_job(job_file), config), ())

    def test_continue_rebuilt_qa_keeps_stored_review_acknowledgment(self) -> None:
        """重算出 needs_review 时，已存量确认的老 Job 必须仍被放行而不是二次阻断。"""

        from xvt.pipeline import acknowledge_subtitle_review

        # 宽度足够大才会产生 review 级 bilingual_overflow_risk（needs_review）。
        overflowing = "这是一句非常长的中文译文，用来触发双语行宽度溢出风险" + "哦" * 30
        config = WorkflowConfig(target_language="zh-CN", burn=False)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, qa_path = self._standard_job_with_translated_outputs(root, overflowing)
            self.assertEqual(
                json.loads(qa_path.read_text(encoding="utf-8"))["status"],
                "needs_review",
            )
            acknowledge_subtitle_review(job_file)

            qa_path.write_text("{}", encoding="utf-8")
            self.assertEqual(plan_advance_steps(load_job(job_file), config), ("render",))

            with patch("xvt.workflow.run_translation") as translate:
                result = continue_workflow(job_file)

            self.assertEqual(result.status, "completed")
            translate.assert_not_called()
            rebuilt = load_job(job_file)
            rebuilt_qa = valid_artifact_path(rebuilt, "translated_subtitle_qa")
            self.assertIsNotNone(rebuilt_qa)
            self.assertEqual(
                json.loads(rebuilt_qa.read_text(encoding="utf-8"))["status"],
                "needs_review",
            )
            self.assertEqual(rebuilt.stages[StageName.SUBTITLE_QA].status, StageStatus.SUCCEEDED)
            self.assertEqual(plan_advance_steps(rebuilt, config), ())

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

    def _polished_job_with_invalid_draft(self, root: Path):
        """构造 repair_polish_draft 断点态：draft checksum 已失效，progress checkpoint 完整。

        draft 与 progress 都由真实 workflow 路径产出，因此 fingerprint 与持久化
        目标严格一致（不在测试里复刻 fingerprint 计算）。
        """
        source = root / "source.srt"
        source.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello.\n", encoding="utf-8")
        options = WorkflowOptions(
            input=str(source),
            output=root / "output",
            target_language="zh-CN",
            translation_quality="polished",
            provider="openai-compatible",
            base_url="https://example.com/v1",
            model="model",
            api_key_env="TEST_TRANSLATION_API_KEY",
        )
        with (
            patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
            patch("xvt.pipeline.translation.translate_batch", return_value={1: "草稿"}),
            patch("xvt.pipeline.polish.polish_batch", side_effect=RuntimeError("polish crashed")),
        ):
            with self.assertRaisesRegex(RuntimeError, "polish crashed"):
                run_workflow(options)

        job_file = next((root / "output" / "jobs").iterdir()) / "job.json"
        job = load_job(job_file)
        draft = valid_artifact_path(job, "translation_draft_cues")
        progress = valid_artifact_path(job, "translation_progress")
        self.assertIsNotNone(draft)
        self.assertIsNotNone(progress)
        assert draft is not None and progress is not None
        # 只破坏 draft 内容（checksum 随之失效），progress checkpoint 保持完整。
        draft.write_text("corrupt", encoding="utf-8")
        config = WorkflowConfig.from_dict(load_job(job_file).config["workflow"])
        self.assertEqual(plan_advance_steps(load_job(job_file), config), ("repair_polish_draft",))
        return job_file, progress

    def test_continue_rebuilds_polish_draft_from_complete_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, _progress = self._polished_job_with_invalid_draft(root)

            with patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}):
                plan = build_recovery_plan(job_file)
            self.assertEqual(plan.category, "retryable")
            self.assertIn("continue", plan.primary_command or "")
            self.assertEqual(plan.advance_steps, ("repair_polish_draft",))

            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch("xvt.pipeline.translation.translate_batch") as translate,
                patch("xvt.pipeline.polish.polish_batch", return_value={1: "润色"}),
                patch("xvt.pipeline.semantic.semantic_review_batch", return_value=()),
            ):
                result = continue_workflow(job_file)

            # 零 API 成本重建：全部批次都已在 checkpoint 中完成。
            translate.assert_not_called()
            self.assertEqual(result.status, "completed")
            rebuilt = load_job(job_file)
            self.assertIsNotNone(valid_artifact_path(rebuilt, "translation_draft_cues"))

    def test_continue_blocks_repair_polish_draft_without_usable_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, progress = self._polished_job_with_invalid_draft(root)
            # checkpoint 也失效：继续推进只能靠真实 API 全量重翻，必须拦截。
            progress.write_text("corrupt", encoding="utf-8")

            with patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}):
                plan = build_recovery_plan(job_file)
            self.assertEqual(plan.category, "manual_fix")
            self.assertIsNone(plan.primary_command)

            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch("xvt.pipeline.translation.translate_batch") as translate,
                patch("xvt.pipeline.polish.polish_batch") as polish,
            ):
                with self.assertRaisesRegex(RuntimeError, "润色草稿"):
                    continue_workflow(job_file)

            translate.assert_not_called()
            polish.assert_not_called()

    def test_continue_blocks_repair_polish_draft_when_provider_unresolved(self) -> None:
        """provider 无法解析（Agent 模式）时没有确定性零成本重建路径，必须 fail-closed。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, _progress = self._polished_job_with_invalid_draft(root)

            # 不提供 API key：resolve_provider 回退到 agent 模式。
            with patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": ""}):
                plan = build_recovery_plan(job_file)
                self.assertEqual(plan.category, "manual_fix")
                with (
                    patch("xvt.pipeline.translation.translate_batch") as translate,
                    self.assertRaisesRegex(RuntimeError, "润色草稿"),
                ):
                    continue_workflow(job_file)

            translate.assert_not_called()

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


class TranslationReviewWaitingReasonTest(unittest.TestCase):
    """等待原因必须与恢复计划同源，避免与真实阻断原因错位。"""

    def test_waiting_event_reason_matches_the_recovery_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nCloud Code.\n",
                encoding="utf-8",
            )
            issue = SemanticIssue("source_suspect", (1,), "review", "check source", ())
            events = []
            with (
                patch.dict("os.environ", {"TEST_TRANSLATION_API_KEY": "secret"}),
                patch("xvt.pipeline.translation.translate_batch", return_value={1: "Cloud Code。"}),
                patch("xvt.pipeline.polish.polish_batch", return_value={1: "Cloud Code。"}),
                patch("xvt.pipeline.semantic.semantic_review_batch", return_value=(issue,)),
            ):
                result = run_workflow(
                    WorkflowOptions(
                        input=str(source),
                        output=root / "output",
                        target_language="zh-CN",
                        translation_quality="polished",
                        provider="openai-compatible",
                        base_url="https://example.com/v1",
                        model="model",
                        api_key_env="TEST_TRANSLATION_API_KEY",
                    ),
                    event_sink=events.append,
                )

            self.assertEqual(result.status, "awaiting_input")
            waiting = [event for event in events if event.type == "workflow_waiting"]
            self.assertTrue(waiting)
            plan = build_recovery_plan(load_job(result.job_file))
            self.assertEqual(waiting[-1].data["reason"], plan.reason)
            self.assertNotEqual(waiting[-1].data["reason"], "")

    def test_unrelated_plan_category_falls_back_to_the_english_constant(self) -> None:
        job = create_job("/tmp/source.srt", "/tmp", {"input_kind": "subtitle"})
        plan = SimpleNamespace(category="retryable", reason="不相关的分类")
        with patch("xvt.workflow.build_recovery_plan", return_value=plan):
            self.assertEqual(
                _translation_review_waiting_reason(job),
                "semantic review requires user acknowledgment",
            )

    def test_recoverable_plan_failure_falls_back_to_the_english_constant(self) -> None:
        job = create_job("/tmp/source.srt", "/tmp", {"input_kind": "subtitle"})
        with patch("xvt.workflow.build_recovery_plan", side_effect=ValueError("broken job")):
            self.assertEqual(
                _translation_review_waiting_reason(job),
                "semantic review requires user acknowledgment",
            )

    def test_programming_error_from_the_plan_is_not_swallowed(self) -> None:
        job = create_job("/tmp/source.srt", "/tmp", {"input_kind": "subtitle"})
        with patch("xvt.workflow.build_recovery_plan", side_effect=AttributeError("bug")):
            with self.assertRaises(AttributeError):
                _translation_review_waiting_reason(job)

    def test_empty_plan_reason_falls_back_to_the_english_constant(self) -> None:
        job = create_job("/tmp/source.srt", "/tmp", {"input_kind": "subtitle"})
        plan = SimpleNamespace(category="awaiting_input", reason="")
        with patch("xvt.workflow.build_recovery_plan", return_value=plan):
            self.assertEqual(
                _translation_review_waiting_reason(job),
                "semantic review requires user acknowledgment",
            )


class BurnFingerprintArtifactTest(unittest.TestCase):
    def _relinkable_job(self, root: Path) -> tuple[Path, Path, Path]:
        media = root / "video.mp4"
        media.write_bytes(b"media")
        burned = root / "burned.mp4"
        burned.write_bytes(b"burned-output")
        candidate = root / "saved.mp4"
        candidate.write_bytes(burned.read_bytes())
        cues = root / "translated-cues.json"
        cues.write_text("[]", encoding="utf-8")
        source_cues = root / "source-cues.json"
        source_cues.write_text("[]", encoding="utf-8")
        translated_srt = root / "translated.srt"
        translated_srt.write_text("", encoding="utf-8")
        bilingual_srt = root / "bilingual.srt"
        bilingual_srt.write_text("", encoding="utf-8")
        translated_qa = root / "qa-translated.json"
        translated_qa.write_text('{"status":"pass","issues":[]}\n', encoding="utf-8")
        media_qa = root / "media-qa.json"
        media_qa.write_text('{"status":"pass","issues":[]}\n', encoding="utf-8")
        job = create_job(
            str(media),
            root,
            {
                "workflow": {
                    "target_language": "zh-CN",
                    "burn": True,
                    "hardware_encoding": True,
                }
            },
        )
        job.artifacts.extend(
            [
                Artifact("translated_cues", str(cues), file_checksum(cues)),
                Artifact("source_cues", str(source_cues), file_checksum(source_cues)),
                Artifact("translated_srt", str(translated_srt), file_checksum(translated_srt)),
                Artifact("bilingual_srt", str(bilingual_srt), file_checksum(bilingual_srt)),
                Artifact(
                    "translated_subtitle_qa",
                    str(translated_qa),
                    file_checksum(translated_qa),
                ),
                Artifact("burned_video", str(burned), file_checksum(burned)),
                Artifact("media_qa", str(media_qa), file_checksum(media_qa)),
            ]
        )
        job.stages[StageName.MEDIA_QA].status = StageStatus.SUCCEEDED
        job_file = save_job(job)
        _record_burn_fingerprint(job_file)
        return job_file, burned, candidate

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
            burned = Path(directory) / "burned.mp4"
            burned.write_bytes(b"burned")
            job.artifacts.extend(
                [
                    Artifact("translated_cues", str(cues), file_checksum(cues)),
                    Artifact("burned_video", str(burned), file_checksum(burned)),
                ]
            )
            job_file = save_job(job)

            _record_burn_fingerprint(job_file)
            job = load_job(job_file)

            self.assertIsNone(job.config["workflow"].get("burn_fingerprint"))
            stored = _stored_burn_fingerprint(job)
            self.assertIsNotNone(stored)
            self.assertTrue(any(item.kind == "burn_fingerprint" for item in job.artifacts))
            path = next(item.path for item in job.artifacts if item.kind == "burn_fingerprint")
            self.assertTrue(Path(path).is_file())
            self.assertEqual(
                json.loads(Path(path).read_text(encoding="utf-8")),
                {
                    "version": 2,
                    "fingerprint": stored,
                    "burned_video_checksum": file_checksum(burned),
                    "source_media_checksum": file_checksum(media),
                },
            )

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
            media_qa = Path(directory) / "media-qa.json"
            media_qa.write_text('{"status":"pass"}\n', encoding="utf-8")
            job.artifacts.extend(
                [
                    Artifact("translated_cues", str(cues), file_checksum(cues)),
                    Artifact("burned_video", str(burned), file_checksum(burned)),
                    Artifact("media_qa", str(media_qa), file_checksum(media_qa)),
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

    def test_relink_burned_output_restores_artifact_validity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file, burned, candidate = self._relinkable_job(Path(directory))
            burned.unlink()
            events = []

            result = relink_burned_output(job_file, candidate, event_sink=events.append)
            job = load_job(job_file)
            artifact = next(item for item in job.artifacts if item.kind == "burned_video")
            config = WorkflowConfig.from_dict(job.config["workflow"])
            self.assertEqual(result, candidate.resolve())
            self.assertEqual(artifact.path, str(candidate.resolve()))
            self.assertEqual(artifact.checksum, file_checksum(candidate))
            self.assertTrue(_burn_satisfied(job, config))
            self.assertEqual(verify_job(job_file)["status"], "pass")
            self.assertEqual(build_recovery_plan(job_file).category, "complete")
            self.assertEqual(
                [event.type for event in events],
                [EventType.ARTIFACT_INVALIDATED, EventType.ARTIFACT_CREATED],
            )

    def test_relink_burned_output_rejects_unverified_candidates_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for case in (
                "checksum",
                "media_qa_stage",
                "media_qa_missing",
                "media_qa_checksum",
                "media_qa_status",
                "translated_cues",
                "fingerprint",
            ):
                with self.subTest(case=case):
                    case_root = root / case
                    case_root.mkdir()
                    job_file, burned, candidate = self._relinkable_job(case_root)
                    burned.unlink()
                    if case == "checksum":
                        candidate.write_bytes(b"different")
                    elif case == "media_qa_stage":
                        job = load_job(job_file)
                        job.stages[StageName.MEDIA_QA].status = StageStatus.FAILED
                        save_job(job)
                    elif case == "media_qa_missing":
                        job = load_job(job_file)
                        job.artifacts = [
                            artifact for artifact in job.artifacts if artifact.kind != "media_qa"
                        ]
                        save_job(job)
                    elif case == "media_qa_checksum":
                        job = load_job(job_file)
                        media_qa_path = Path(
                            next(
                                artifact.path
                                for artifact in job.artifacts
                                if artifact.kind == "media_qa"
                            )
                        )
                        media_qa_path.write_text('{"status":"failed"}\n', encoding="utf-8")
                    elif case == "media_qa_status":
                        job = load_job(job_file)
                        media_qa_path = Path(
                            next(
                                artifact.path
                                for artifact in job.artifacts
                                if artifact.kind == "media_qa"
                            )
                        )
                        media_qa_path.write_text('{"status":"failed"}\n', encoding="utf-8")
                        job.artifacts = [
                            Artifact("media_qa", str(media_qa_path), file_checksum(media_qa_path))
                            if artifact.kind == "media_qa"
                            else artifact
                            for artifact in job.artifacts
                        ]
                        save_job(job)
                    elif case == "translated_cues":
                        job = load_job(job_file)
                        cues_path = Path(
                            next(
                                item.path
                                for item in job.artifacts
                                if item.kind == "translated_cues"
                            )
                        )
                        cues_path.write_text('[{"text":"tampered"}]', encoding="utf-8")
                    else:
                        job = load_job(job_file)
                        job.config["workflow"]["hardware_encoding"] = False
                        save_job(job)
                    if case.startswith("media_qa"):
                        job = load_job(job_file)
                        config = WorkflowConfig.from_dict(job.config["workflow"])
                        self.assertFalse(_burn_satisfied(job, config))
                        self.assertIn("burn", build_recovery_plan(job_file).advance_steps)
                    if case != "checksum":
                        plan = build_recovery_plan(job_file)
                        self.assertNotEqual(plan.category, "awaiting_input")
                        self.assertNotIn("relink-burned-output", plan.primary_command or "")
                    before = job_file.read_bytes()

                    with self.assertRaisesRegex(
                        UserInputError,
                        "checksum|media QA|translated subtitles|fingerprint",
                    ):
                        relink_burned_output(job_file, candidate)

                    self.assertEqual(job_file.read_bytes(), before)

    def test_recovery_does_not_hash_source_media_for_v2_proof(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file, _burned, _candidate = self._relinkable_job(Path(directory))

            with patch("xvt.workflow._burn_source_media_checksum") as source_checksum:
                plan = build_recovery_plan(job_file)

            self.assertEqual(plan.category, "complete")
            source_checksum.assert_not_called()

    def test_recovery_offers_relink_for_missing_v2_burned_output_without_hashing_source(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file, burned, _candidate = self._relinkable_job(Path(directory))
            burned.unlink()

            with patch("xvt.workflow._burn_source_media_checksum") as source_checksum:
                plan = build_recovery_plan(job_file)
            job = load_job(job_file)
            config = WorkflowConfig.from_dict(job.config["workflow"])
            with patch(
                "xvt.workflow.valid_artifact_path",
                wraps=valid_artifact_path,
            ) as valid_artifact_path_mock:
                self.assertTrue(
                    burned_output_relink_recovery_available(
                        job,
                        config,
                        burned_video_valid=False,
                    )
                )

        self.assertEqual(plan.category, "awaiting_input")
        self.assertEqual(
            plan.reason,
            "烧录成品可能可重新关联；执行命令时会再次校验源媒体内容。",
        )
        self.assertIn("relink-burned-output", plan.primary_command or "")
        self.assertIn("EXISTING_VIDEO", plan.primary_command or "")
        self.assertIn("continue", plan.alternative_commands[0])
        self.assertEqual(plan.advance_steps, ("burn",))
        source_checksum.assert_not_called()
        self.assertNotIn(
            "burned_video",
            [call.args[1] for call in valid_artifact_path_mock.call_args_list],
        )

    def test_recovery_retries_burn_when_v2_proof_fingerprint_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file, burned, _candidate = self._relinkable_job(Path(directory))
            job = load_job(job_file)
            job.config["workflow"]["hardware_encoding"] = False
            save_job(job)
            burned.unlink()

            plan = build_recovery_plan(job_file)

        self.assertEqual(plan.category, "retryable")
        self.assertIn("continue", plan.primary_command or "")
        self.assertNotIn("relink-burned-output", plan.primary_command or "")
        self.assertEqual(plan.advance_steps, ("burn",))

    def test_relink_burned_output_rejects_replaced_burned_checksum_proof(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, burned, candidate = self._relinkable_job(root)
            burned.unlink()
            candidate.write_bytes(b"replacement")
            job = load_job(job_file)
            job.artifacts = [
                Artifact("burned_video", str(candidate), file_checksum(candidate))
                if artifact.kind == "burned_video"
                else artifact
                for artifact in job.artifacts
            ]
            save_job(job)
            self.assertFalse(_burn_satisfied(job, WorkflowConfig.from_dict(job.config["workflow"])))
            plan = build_recovery_plan(job_file)
            self.assertNotEqual(plan.category, "awaiting_input")
            self.assertNotIn("relink-burned-output", plan.primary_command or "")
            before = job_file.read_bytes()

            with self.assertRaisesRegex(UserInputError, "checksum"):
                relink_burned_output(job_file, candidate)

            self.assertEqual(job_file.read_bytes(), before)

    def test_relink_burned_output_rejects_source_content_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, burned, candidate = self._relinkable_job(root)
            media = root / "video.mp4"
            stat = media.stat()
            media.write_bytes(b"other")
            os.utime(media, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            self.assertEqual(media.stat().st_size, stat.st_size)
            self.assertEqual(media.stat().st_mtime_ns, stat.st_mtime_ns)
            burned.unlink()
            plan = build_recovery_plan(job_file)
            self.assertEqual(plan.category, "awaiting_input")
            self.assertIn("relink-burned-output", plan.primary_command or "")
            before = job_file.read_bytes()

            with self.assertRaisesRegex(UserInputError, "fingerprint"):
                relink_burned_output(job_file, candidate)

            self.assertEqual(job_file.read_bytes(), before)

    def test_relink_burned_output_rejects_legacy_proofs_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for mode in ("artifact", "workflow", "invalid_checksum"):
                with self.subTest(mode=mode):
                    case_root = root / mode
                    case_root.mkdir()
                    job_file, burned, candidate = self._relinkable_job(case_root)
                    job = load_job(job_file)
                    if mode == "artifact":
                        proof_path = Path(
                            next(
                                artifact.path
                                for artifact in job.artifacts
                                if artifact.kind == "burn_fingerprint"
                            )
                        )
                        proof_path.write_text(
                            json.dumps({"fingerprint": _stored_burn_fingerprint(job)}) + "\n",
                            encoding="utf-8",
                        )
                        job.artifacts = [
                            Artifact("burn_fingerprint", str(proof_path), file_checksum(proof_path))
                            if artifact.kind == "burn_fingerprint"
                            else artifact
                            for artifact in job.artifacts
                        ]
                    elif mode == "workflow":
                        job.artifacts = [
                            artifact
                            for artifact in job.artifacts
                            if artifact.kind != "burn_fingerprint"
                        ]
                        job.config["workflow"]["burn_fingerprint"] = "legacy"
                    else:
                        proof_path = Path(
                            next(
                                artifact.path
                                for artifact in job.artifacts
                                if artifact.kind == "burn_fingerprint"
                            )
                        )
                        proof_path.write_text('{"version":2}\n', encoding="utf-8")
                    save_job(job)
                    burned.unlink()
                    plan = build_recovery_plan(job_file)
                    self.assertEqual(plan.category, "retryable")
                    self.assertIn("continue", plan.primary_command or "")
                    self.assertNotIn("relink-burned-output", plan.primary_command or "")
                    before = job_file.read_bytes()

                    with self.assertRaisesRegex(UserInputError, "v2 burn proof"):
                        relink_burned_output(job_file, candidate)

                    self.assertEqual(job_file.read_bytes(), before)

    def test_relink_burned_output_rejects_duplicate_burned_artifacts_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, burned, candidate = self._relinkable_job(root)
            job = load_job(job_file)
            job.artifacts.append(Artifact("burned_video", str(candidate), file_checksum(candidate)))
            save_job(job)
            burned.unlink()
            plan = build_recovery_plan(job_file)
            self.assertNotEqual(plan.category, "awaiting_input")
            self.assertNotIn("relink-burned-output", plan.primary_command or "")
            before = job_file.read_bytes()

            with self.assertRaisesRegex(UserInputError, "exactly one"):
                relink_burned_output(job_file, candidate)

            self.assertEqual(job_file.read_bytes(), before)

    def test_relink_burned_output_accepts_remote_source_video_proof(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, burned, candidate = self._relinkable_job(root)
            job = load_job(job_file)
            job.input = "https://example.com/video.mp4"
            source_video = root / "source-video.mp4"
            source_video.write_bytes((root / "video.mp4").read_bytes())
            job.artifacts.append(
                Artifact("source_video", str(source_video), file_checksum(source_video))
            )
            save_job(job)
            _record_burn_fingerprint(job_file)
            burned.unlink()

            self.assertEqual(relink_burned_output(job_file, candidate), candidate.resolve())

    def test_relink_burned_output_uses_explicit_burn_media_proof(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, burned, candidate = self._relinkable_job(root)
            burn_media = root / "burn-media.mp4"
            burn_media.write_bytes(b"explicit-media")
            job = load_job(job_file)
            job.config["workflow"]["burn_media"] = str(burn_media)
            save_job(job)
            _record_burn_fingerprint(job_file)
            burned.unlink()

            self.assertEqual(relink_burned_output(job_file, candidate), candidate.resolve())

    def test_relink_burned_output_rejects_copied_job_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, burned, candidate = self._relinkable_job(root)
            burned.unlink()
            copied_job = root / "copied-job.json"
            copied_job.write_bytes(job_file.read_bytes())

            with self.assertRaisesRegex(UserInputError, "persisted identity"):
                relink_burned_output(copied_job, candidate)

            self.assertEqual(copied_job.read_bytes(), job_file.read_bytes())

    def test_relink_burned_output_rejects_job_without_workflow_goal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "saved.mp4"
            candidate.write_bytes(b"video")
            job = create_job(str(root / "input.mp4"), root)
            job_file = save_job(job)
            before = job_file.read_bytes()

            with self.assertRaisesRegex(UserInputError, "workflow goal"):
                relink_burned_output(job_file, candidate)

            self.assertEqual(job_file.read_bytes(), before)


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

    def test_plan_advance_steps_polishes_draft_before_burn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job("/tmp/video.mp4", directory)
            save_job(job)
            config = WorkflowConfig(
                target_language="zh-CN",
                translation_quality="polished",
                burn=True,
            )
            with patch(
                "xvt.workflow.valid_artifact_path",
                side_effect=lambda _job, kind: (
                    Path(f"/tmp/{kind}") if kind == "translation_draft_cues" else None
                ),
            ):
                self.assertEqual(plan_advance_steps(job, config), ("polish", "burn"))

    def test_plan_advance_steps_awaits_agent_polish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = create_job("/tmp/video.mp4", directory)
            job.stages[StageName.TRANSLATE].status = StageStatus.AWAITING_INPUT
            save_job(job)
            config = WorkflowConfig(
                target_language="zh-CN",
                translation_quality="polished",
            )
            with (
                patch(
                    "xvt.workflow.valid_artifact_path",
                    side_effect=lambda _job, kind: (
                        Path(f"/tmp/{kind}")
                        if kind in {"translation_draft_cues", "translation_polish_batches"}
                        else None
                    ),
                ),
                patch("xvt.workflow.agent_translation_polish_manifest_valid", return_value=True),
            ):
                self.assertEqual(plan_advance_steps(job, config), ("await_agent_polish",))

    def test_polish_waiting_exposes_only_polish_manifest(self) -> None:
        from xvt.workflow import _current_artifacts

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/video.mp4", root)
            translation = root / "translation-batches.json"
            polish = root / "translation-polish-batches.json"
            translation.write_text("{}", encoding="utf-8")
            polish.write_text("{}", encoding="utf-8")
            job.artifacts.extend(
                [
                    Artifact(
                        "translation_batches",
                        str(translation),
                        file_checksum(translation),
                    ),
                    Artifact(
                        "translation_polish_batches",
                        str(polish),
                        file_checksum(polish),
                    ),
                ]
            )

            artifacts = _current_artifacts(job, waiting=True, polishing=True)

        self.assertEqual(artifacts, (polish,))

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
            media_qa = Path(directory) / "media-qa.json"
            media_qa.write_text('{"status":"pass"}\n', encoding="utf-8")
            config = WorkflowConfig(
                target_language="zh-CN",
                burn=True,
                burn_fingerprint="fp",
            )
            with (
                patch(
                    "xvt.workflow.valid_artifact_path",
                    side_effect=lambda _job, kind: (
                        media_qa
                        if kind == "media_qa"
                        else Path(f"/tmp/{kind}")
                        if kind
                        in {
                            "translated_cues",
                            "translated_srt",
                            "bilingual_srt",
                            "translated_subtitle_qa",
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


class CueRetranslationWaitingTest(unittest.TestCase):
    """局部重译等待必须对 planner 与 continue 可见，且不得盖掉其他等待态。"""

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
                        {"id": cue_id, "text": f"译文{cue_id}"}
                        for cue_id in range(1, cue_count + 1)
                    ]
                }
            ),
            encoding="utf-8",
        )
        apply_agent_translation(job_file, response, target_language="zh-CN")
        return job_file

    def _request_retranslation(self, job_file: Path, cue_id: int = 1) -> Path:
        return retranslate_cues(
            job_file,
            cue_ids=(cue_id,),
            target_language="zh-CN",
            provider="agent",
        ).paths[0]

    def test_plan_and_continue_report_the_pending_cue_retranslation(self) -> None:
        """端到端目标已满足的 Job 上，重译等待不得被 status / continue 报成已完成。"""

        events: list[dict] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root)
            job = load_job(job_file)
            config = require_workflow_config(job)
            self.assertEqual(plan_advance_steps(job, config), ())

            manifest_path = self._request_retranslation(job_file)

            job = load_job(job_file)
            self.assertEqual(plan_advance_steps(job, config), ("await_cue_retranslation",))

            result = continue_workflow(job_file, event_sink=events.append)

        self.assertEqual(result.status, "awaiting_input")
        self.assertEqual(result.artifacts, (manifest_path,))
        types = [event.type for event in events]
        self.assertIn(EventType.WORKFLOW_WAITING.value, types)
        self.assertNotIn(EventType.WORKFLOW_COMPLETED.value, types)

    def test_waiting_is_visible_without_a_persisted_target_language(self) -> None:
        """重译等待分支必须排在 needs_translation 守卫之前。

        用户 `run` 时没给目标语言、后来用原子命令补翻的 Job，其 workflow 目标里
        `target_language` 为 None，走不进被 `goals.needs_translation` 守卫的等待分支。
        """

        events: list[dict] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nFirst.\n"
                "\n2\n00:00:01,000 --> 00:00:02,000\nSecond.\n",
                encoding="utf-8",
            )
            job = run_subtitle_transcription(source, root, language="en")
            job_file = get_job_file(job)
            # 无目标语言的端到端目标：转写腿已完成，翻译由原子命令补上。
            set_workflow_config(job, WorkflowConfig())
            translated_path = get_job_directory(job) / "artifacts" / "translated-cues.json"
            source_cues = json.loads(
                (get_job_directory(job) / "artifacts" / "source-cues.json").read_text(
                    encoding="utf-8"
                )
            )
            translated_path.write_text(
                json.dumps(
                    [
                        {**cue, "target_text": f"译文{cue['id']}", "target_language": "zh-CN"}
                        for cue in source_cues
                    ]
                ),
                encoding="utf-8",
            )
            job.artifacts.append(
                Artifact("translated_cues", str(translated_path), file_checksum(translated_path))
            )
            save_job(job)
            self.assertIsNone(require_workflow_config(load_job(job_file)).target_language)
            # 请求重译之前，这个 Job 对 continue 而言确实是「已完成」。
            self.assertEqual(continue_workflow(job_file).status, "completed")

            manifest_path = self._request_retranslation(job_file)
            job = load_job(job_file)
            self.assertEqual(
                plan_advance_steps(job, require_workflow_config(job)),
                ("await_cue_retranslation",),
            )

            result = continue_workflow(job_file, event_sink=events.append)

        self.assertEqual(result.status, "awaiting_input")
        self.assertEqual(result.artifacts, (manifest_path,))
        self.assertIn(EventType.WORKFLOW_WAITING.value, [event.type for event in events])

    def test_stale_manifest_does_not_mask_the_polish_agent_wait(self) -> None:
        """回归护栏：重译回填后再进入润色等待，残留 manifest 不得盖掉 await_agent_polish。

        ``cue_retranslation_batches`` 在依赖图上只派生自 ``source_cues``，译文被改写时
        不会失效，因此回填后它仍然 checksum 有效——严格谓词靠「manifest 记录的译文
        checksum 与当前一致」把这份残留排除掉。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root, cue_count=1)
            manifest_path = self._request_retranslation(job_file)
            fingerprint = json.loads(manifest_path.read_text(encoding="utf-8"))["fingerprint"]
            response = root / "cue-response.json"
            response.write_text(
                json.dumps({"fingerprint": fingerprint, "items": [{"id": 1, "text": "重译一"}]}),
                encoding="utf-8",
            )
            apply_cue_retranslation(
                job_file,
                response,
                cue_ids=(1,),
                target_language="zh-CN",
            )
            run_translation_polish(job_file)

            job = load_job(job_file)
            config = require_workflow_config(job)
            # 残留 manifest 仍然 checksum 有效，谓词只能靠译文 checksum 把它排除。
            self.assertIsNotNone(valid_artifact_path(job, "cue_retranslation_batches"))
            self.assertIs(
                job.stages[StageName.TRANSLATE].status,
                StageStatus.AWAITING_INPUT,
            )
            self.assertFalse(cue_retranslation_waiting(job))
            self.assertEqual(plan_advance_steps(job, config), ("await_agent_polish",))

    def test_translated_cue_changes_invalidate_the_waiting_predicate(self) -> None:
        """manifest 记录的译文 checksum 与当前不一致时谓词为假，计划回到既有结果。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root, cue_count=1)
            self._request_retranslation(job_file)
            job = load_job(job_file)
            self.assertTrue(cue_retranslation_waiting(job))

            translated = next(
                artifact for artifact in job.artifacts if artifact.kind == "translated_cues"
            )
            path = Path(translated.path)
            cues = json.loads(path.read_text(encoding="utf-8"))
            cues[0]["target_text"] = "等待期间被改过"
            path.write_text(json.dumps(cues), encoding="utf-8")
            # 重新登记 checksum：译文本身仍是「有效产物」，失配的只有 manifest 的记录。
            translated.checksum = file_checksum(path)
            save_job(job)

            job = load_job(job_file)
            self.assertIsNotNone(valid_artifact_path(job, "translated_cues"))
            self.assertFalse(cue_retranslation_waiting(job))
            # 计划回到既有结果：译文目标的四件产物仍全部有效，因此仍是空计划。
            # 「TRANSLATE stage 停在 awaiting_input」是既有表象问题，本批次不动（放弃
            # 一次重译目前仍靠手工删除 manifest）。
            self.assertEqual(plan_advance_steps(job, require_workflow_config(job)), ())


class QaBlockedAgentWaitTest(unittest.TestCase):
    """SUBTITLE_QA 停在 failed 时，仍然挂着的 Agent 等待必须压过那段旧的 QA 引导。

    ``SUBTITLE_QA`` 不会因为随后开了一次新的等待而自动复位，因此「QA 阻断」与「在途
    Agent 等待」完全可以同时为真；此时 status / continue / 统一 apply 三侧必须一致地
    报等待，否则 batch-continue 会把条目误记成 failed 而不是停在 awaiting_input。
    """

    def _glossary_blocked_job(self, root: Path) -> Path:
        """真实形态的术语死锁 Job：译文 QA 只因术语违规而 failed，因此没有渲染产物。"""

        source = root / "source.srt"
        source.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nSource 1.\n\n",
            encoding="utf-8",
        )
        job = run_subtitle_transcription(source, root, language="en")
        job_file = get_job_file(job)
        set_workflow_config(
            job,
            WorkflowConfig(
                target_language="zh-CN",
                requested_outputs=("translated_subtitles", "bilingual_subtitles"),
                provider="agent",
                batch_size=1,
                translation_policy=TranslationPolicy(terms=(GlossaryTerm("Source", "小组件"),)),
            ),
        )
        save_job(job)
        prepare_agent_translation(
            job_file,
            source_language="en",
            target_language="zh-CN",
            await_input=False,
        )
        response = root / "translation-response.json"
        response.write_text(
            json.dumps({"items": [{"id": 1, "text": "译文一"}]}),
            encoding="utf-8",
        )
        # 译文 QA 失败由 complete_qa_stage 抛出；stage 与报告在抛之前已经落盘。
        with self.assertRaises(RuntimeError):
            apply_agent_translation(job_file, response, target_language="zh-CN")
        job = load_job(job_file)
        self.assertIs(job.stages[StageName.SUBTITLE_QA].status, StageStatus.FAILED)
        self.assertIsNone(valid_artifact_path(job, "translated_srt"))
        return job_file

    def test_qa_failure_guidance_still_blocks_a_job_without_a_pending_wait(self) -> None:
        """护栏：没有在途等待时，QA 引导必须照旧拦住 continue。"""

        with tempfile.TemporaryDirectory() as directory:
            job_file = self._glossary_blocked_job(Path(directory))
            with self.assertRaises(RuntimeError) as raised:
                continue_workflow(job_file)

        self.assertIn("术语表冲突", str(raised.exception))

    def test_a_new_full_round_is_not_shadowed_by_the_stale_qa_failure(self) -> None:
        """整轮重翻取代术语死锁：planner / status / continue 必须都走统一 apply 出口。"""

        events: list[dict] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._glossary_blocked_job(root)
            prepare_agent_translation(
                job_file,
                source_language="en",
                target_language="zh-CN",
                await_input=True,
            )

            job = load_job(job_file)
            # 旧译文与旧 QA 报告刻意保留：等待期间它们对用户仍有价值。
            self.assertIsNotNone(valid_artifact_path(job, "translated_cues"))
            self.assertIs(job.stages[StageName.SUBTITLE_QA].status, StageStatus.FAILED)
            self.assertEqual(
                plan_advance_steps(job, require_workflow_config(job)),
                ("await_agent_translation",),
            )

            plan = build_recovery_plan(job_file)
            result = continue_workflow(job_file, event_sink=events.append)

        self.assertEqual(plan.category, "awaiting_input")
        self.assertEqual(plan.advance_steps, ("await_agent_translation",))
        self.assertIn("xvt apply ", plan.prerequisite_commands[0])
        self.assertNotIn("retranslate-cues", " ".join(plan.prerequisite_commands))
        self.assertNotIn("retranslate-cues", plan.primary_command or "")
        self.assertEqual(result.status, "awaiting_input")
        types = [event.type for event in events]
        self.assertEqual(types.count(EventType.WORKFLOW_WAITING.value), 1)
        self.assertNotIn(EventType.WORKFLOW_COMPLETED.value, types)

    def test_continue_reports_the_pending_cue_retranslation_instead_of_raising(self) -> None:
        """术语死锁的出口跑完之后，continue 必须与 status 一样报等待，而不是要求重跑。"""

        events: list[dict] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._glossary_blocked_job(root)
            manifest_path = retranslate_cues(
                job_file,
                cue_ids=(1,),
                target_language="zh-CN",
                provider="agent",
            ).paths[0]

            plan = build_recovery_plan(job_file)
            result = continue_workflow(job_file, event_sink=events.append)

        self.assertEqual(plan.category, "awaiting_input")
        self.assertEqual(plan.advance_steps, ("await_cue_retranslation",))
        self.assertEqual(result.status, "awaiting_input")
        self.assertIn(manifest_path, result.artifacts)
        types = [event.type for event in events]
        self.assertEqual(types.count(EventType.WORKFLOW_WAITING.value), 1)
        self.assertNotIn(EventType.WORKFLOW_COMPLETED.value, types)


class BilingualAssDeliverableTest(unittest.TestCase):
    """bilingual.ass 是 README / ARCHITECTURE 列出的交付物，失效必须重新渲染。

    旧行为：``_translation_satisfied`` 只查 cue / SRT / QA 四项，删掉 bilingual.ass
    之后 plan_advance_steps 返回 ()，continue / status / batch-continue 三路都报
    completed，而 CLI 没有任何命令能单独重建 ASS——交付物就此永久缺失。
    """

    def _translated_video_job(self, root: Path) -> Path:
        """有分辨率的真实媒体 Job：译文渲染会写出 bilingual.ass。"""

        media = root / "input.mp4"
        media.write_bytes(b"video")

        def run_ffmpeg(args, **_kwargs):
            Path(args[-1]).write_bytes(b"audio")
            return CommandResult(tuple(args), 0, "", "")

        media_info = MediaInfo(
            path=str(media),
            duration_ms=5000,
            size_bytes=5,
            format_name="mp4",
            width=1920,
            height=1080,
            frame_rate=30.0,
            video_codec="h264",
            audio_codec="aac",
            has_video=True,
            has_audio=True,
        )
        transcript = Transcript(
            language="en",
            engine="mlx",
            words=(Word("Hello.", 1000, 1800, 0.9),),
        )
        with (
            patch("xvt.pipeline.transcription.probe_media", return_value=media_info),
            patch("xvt.pipeline.transcription.run_command", side_effect=run_ffmpeg),
            patch("xvt.pipeline.transcription.transcribe_audio", return_value=transcript),
        ):
            job = run_local_transcription(media, root)
        job_file = get_job_file(job)
        set_workflow_config(job, WorkflowConfig(target_language="zh-CN", provider="agent"))
        save_job(job)
        prepare_agent_translation(
            job_file,
            source_language="en",
            target_language="zh-CN",
        )
        response = root / "translation-response.json"
        response.write_text(
            json.dumps({"items": [{"id": 1, "text": "你好。"}]}),
            encoding="utf-8",
        )
        apply_agent_translation(job_file, response, target_language="zh-CN")
        return job_file

    def test_deleted_bilingual_ass_replans_render_and_continue_rebuilds_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_video_job(root)
            job = load_job(job_file)
            ass_path = valid_artifact_path(job, "bilingual_ass")
            self.assertIsNotNone(ass_path)
            assert ass_path is not None
            self.assertEqual(plan_advance_steps(job, require_workflow_config(job)), ())

            ass_path.unlink()
            job = load_job(job_file)
            self.assertIn("render", plan_advance_steps(job, require_workflow_config(job)))
            self.assertEqual(build_recovery_plan(job_file).category, "retryable")

            result = continue_workflow(job_file)

            self.assertEqual(result.status, "completed")
            rebuilt = load_job(job_file)
            self.assertIsNotNone(valid_artifact_path(rebuilt, "bilingual_ass"))
            self.assertTrue(ass_path.exists())
            self.assertIn(ass_path, result.artifacts)
            self.assertEqual(plan_advance_steps(rebuilt, require_workflow_config(rebuilt)), ())

    def test_tampered_bilingual_ass_also_replans_render(self) -> None:
        """checksum 失效（被改写）与被删除同等对待，不能只认「文件不在」。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_video_job(root)
            ass_path = valid_artifact_path(load_job(job_file), "bilingual_ass")
            assert ass_path is not None
            ass_path.write_text("[Script Info]\n; tampered\n", encoding="utf-8")

            job = load_job(job_file)
            self.assertIn("render", plan_advance_steps(job, require_workflow_config(job)))
            continue_workflow(job_file)

            rebuilt = load_job(job_file)
            self.assertIsNotNone(valid_artifact_path(rebuilt, "bilingual_ass"))
            self.assertNotIn("tampered", ass_path.read_text(encoding="utf-8"))

    def test_job_that_never_rendered_ass_is_still_complete(self) -> None:
        """纯字幕输入没有分辨率，从不登记 bilingual_ass；对它要求 ASS 会永远收敛不了。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello.\n",
                encoding="utf-8",
            )
            job = run_subtitle_transcription(source, root, language="en")
            job_file = get_job_file(job)
            set_workflow_config(job, WorkflowConfig(target_language="zh-CN", provider="agent"))
            save_job(job)
            prepare_agent_translation(
                job_file,
                source_language="en",
                target_language="zh-CN",
            )
            response = root / "translation-response.json"
            response.write_text(
                json.dumps({"items": [{"id": 1, "text": "你好。"}]}),
                encoding="utf-8",
            )
            apply_agent_translation(job_file, response, target_language="zh-CN")

            job = load_job(job_file)
            self.assertFalse(
                any(artifact.kind == "bilingual_ass" for artifact in job.artifacts),
            )
            self.assertEqual(plan_advance_steps(job, require_workflow_config(job)), ())


if __name__ == "__main__":
    unittest.main()

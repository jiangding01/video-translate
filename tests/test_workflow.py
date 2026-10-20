import ast
import json
import os
import shlex
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import xvt.batch
import xvt.pipeline.common
import xvt.pipeline.semantic
import xvt.pipeline.semantic_state
import xvt.pipeline.transcription
import xvt.pipeline.translation
import xvt.workflow
from xvt.errors import ExitCode, InternalError, UserInputError
from xvt.events import EventType
from xvt.job_store import create_job, get_job_directory, get_job_file, load_job, save_job
from xvt.media import MediaInfo
from xvt.models import Artifact, Cue, Job, StageName, StageStatus, Transcript, Word
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
from xvt.pipeline.common import load_cues, required_glossary_terms
from xvt.process import CommandResult
from xvt.qa import check_subtitles
from xvt.recovery import build_recovery_plan, file_checksum, valid_artifact_path, verify_job
from xvt.subtitles import save_subtitles
from xvt.translate import GlossaryTerm, SemanticIssue, TranslationPolicy
from xvt.translation_provider import TranslationProviderError
from xvt.workflow import (
    _RELINK_MEDIA_QA_MESSAGES,
    WorkflowOptions,
    _burn_satisfied,
    _record_burn_fingerprint,
    _stored_burn_fingerprint,
    _translation_review_waiting_reason,
    burned_output_relink_recovery_available,
    continue_workflow,
    missing_burn_source_media,
    plan_advance_steps,
    planned_run_steps,
    relink_burned_output,
    relink_source_media,
    run_workflow,
    workflow_goals,
)
from xvt.workflow_config import (
    WorkflowConfig,
    get_workflow_config,
    require_workflow_config,
    set_workflow_config,
)

# 下面几个 helper 是「打桩也必须产生真实副作用」的共享实现。
# 历史上这批测试把 run_translation / apply_subtitle_translation / burn_translated_job
# 打成「返回假路径、完全不碰 Job」的替身，然后断言 result.status == "completed"——
# 于是「completed 对应一个真的完成了的 Job」这件事从来没有被验证过（REVIEW_FINDINGS
# 的 F6 / O5）。桩只用来避开真实翻译 API 与 FFmpeg，落盘与登记必须照真实运行来。
_SOURCE_SRT_TEXT = "1\n00:00:00,000 --> 00:00:02,000\nHello world.\n"
_SOURCE_CUE = Cue(1, 0, 2000, "Hello world.", source_language="en")
_TRANSLATED_CUE = replace(_SOURCE_CUE, target_text="你好世界。", target_language="zh-CN")


def _register_artifact(job: Job, kind: str, path: Path) -> None:
    """登记（或替换）一个 checksum 有效的 artifact。"""

    job.artifacts = [item for item in job.artifacts if item.kind != kind]
    job.artifacts.append(Artifact(kind, str(path), file_checksum(path)))


def _job_artifacts_directory(job: Job) -> Path:
    directory = get_job_directory(job) / "artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _write_source_leg_artifacts(job_file: Path) -> None:
    """落盘转写腿的真实产物（source_cues + source_srt）。

    ``continue_workflow`` 在推进翻译/烧录之前要求这两者 checksum 有效，
    否则会回去跑 resume；真实状态下它们由转写腿写出。
    """

    job = load_job(job_file)
    directory = _job_artifacts_directory(job)
    cues_path = directory / "source-cues.json"
    cues_path.write_text(json.dumps([asdict(_SOURCE_CUE)], ensure_ascii=False), encoding="utf-8")
    srt_path = directory / "source.srt"
    srt_path.write_text(_SOURCE_SRT_TEXT, encoding="utf-8")
    _register_artifact(job, "source_cues", cues_path)
    _register_artifact(job, "source_srt", srt_path)
    job.stages[StageName.SUBTITLE_QA].status = StageStatus.SUCCEEDED
    save_job(job)


def _complete_translation_artifacts(job_file: Path) -> tuple[Path, ...]:
    """翻译桩的真实副作用：写出译文 cue、两份 SRT 与通过的译文 QA 报告并登记。

    返回值与真实 ``run_translation`` / ``apply_subtitle_translation`` 一致
    （用户可见的译文字幕路径）。
    """

    job = load_job(job_file)
    directory = _job_artifacts_directory(job)
    cues_path = directory / "translated-cues.json"
    cues_path.write_text(
        json.dumps([asdict(_TRANSLATED_CUE)], ensure_ascii=False), encoding="utf-8"
    )
    translated_srt = save_subtitles(
        [_TRANSLATED_CUE], directory / "translated.srt", mode="translated"
    )
    bilingual_srt = save_subtitles([_TRANSLATED_CUE], directory / "bilingual.srt", mode="bilingual")
    qa_path = directory / "qa-translated.json"
    report = check_subtitles([_TRANSLATED_CUE], require_translation=True)
    assert report.status == "pass", report.status
    qa_path.write_text(json.dumps(asdict(report), ensure_ascii=False), encoding="utf-8")
    for kind, path in (
        ("translated_cues", cues_path),
        ("translated_srt", translated_srt),
        ("bilingual_srt", bilingual_srt),
        ("translated_subtitle_qa", qa_path),
    ):
        _register_artifact(job, kind, path)
    for stage in (StageName.TRANSLATE, StageName.SUBTITLE_QA, StageName.RENDER):
        job.stages[stage].status = StageStatus.SUCCEEDED
    save_job(job)
    return (translated_srt, bilingual_srt)


def _complete_burn_artifacts(job_file: Path, burned_path: Path) -> Path:
    """烧录桩的真实副作用：写出成品、登记 artifact、写出通过的媒体 QA 报告。

    真实 ``burn_translated_job`` 会在成功后把 BURN / MEDIA_QA 两个 stage 推到
    succeeded 并登记 ``burned_video`` / ``media_qa``；桩必须造出同样的状态，
    否则 ``_burn_satisfied`` 永远为假而测试却断言 completed。
    """

    burned_path.parent.mkdir(parents=True, exist_ok=True)
    burned_path.write_bytes(b"burned-output")
    job = load_job(job_file)
    directory = _job_artifacts_directory(job)
    media_qa_path = directory / "media-qa.json"
    media_qa_path.write_text('{"status": "pass", "issues": []}\n', encoding="utf-8")
    _register_artifact(job, "burned_video", burned_path)
    _register_artifact(job, "media_qa", media_qa_path)
    for stage in (StageName.BURN, StageName.MEDIA_QA):
        job.stages[stage].status = StageStatus.SUCCEEDED
    save_job(job)
    return burned_path


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
            media = Path(directory) / "video.mp4"
            media.write_bytes(b"media")
            job = create_job(str(media), directory)
            events = []
            burned_path = Path(directory) / "burned.mp4"

            def fake_transcribe(_input, _output, *, workflow_config, **_kwargs):
                # 真实 run_transcription 会把 workflow 目标固化进 Job；桩必须同样落盘，
                # 否则 _record_burn_fingerprint 读不到持久化目标会静默跳过 proof 登记。
                job.config["workflow"] = workflow_config
                save_job(job)
                return job

            def fake_translate(job_file, **_kwargs):
                # 翻译桩只避开真实 API：译文 cue / 两份 SRT / 译文 QA 报告照真实运行落盘并登记。
                return TranslationRunResult(
                    "openai-compatible", _complete_translation_artifacts(job_file)
                )

            def fake_burn(job_file, **_kwargs):
                # 烧录桩只避开 FFmpeg：成品、媒体 QA 报告与 BURN/MEDIA_QA 两个 stage
                # 都照真实运行推进，之后 _record_burn_fingerprint 走真实实现登记 v2 proof。
                return _complete_burn_artifacts(job_file, burned_path)

            with (
                patch("xvt.workflow.run_transcription", side_effect=fake_transcribe) as transcribe,
                patch(
                    "xvt.workflow.run_translation",
                    side_effect=fake_translate,
                ) as translate,
                patch(
                    "xvt.workflow.burn_translated_job",
                    side_effect=fake_burn,
                ) as burn,
            ):
                result = run_workflow(
                    WorkflowOptions(
                        input=str(media),
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

            def fake_transcribe(_input, _output, *, workflow_config, **_kwargs):
                # 真实 run_transcription 会把 workflow 目标固化进 Job；桩同口径落盘。
                job.config["workflow"] = workflow_config
                save_job(job)
                return job

            def fake_apply(job_file, _subtitles, **_kwargs):
                # 导入译文的真实副作用：译文 cue、两份 SRT 与译文 QA 报告全部落盘并登记。
                return _complete_translation_artifacts(job_file)

            with (
                patch("xvt.workflow.run_transcription", side_effect=fake_transcribe),
                patch(
                    "xvt.workflow.apply_subtitle_translation",
                    side_effect=fake_apply,
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
            job_file = save_job(job)
            _write_source_leg_artifacts(job_file)
            with patch(
                "xvt.workflow.run_translation",
                side_effect=lambda job_file, **_kwargs: TranslationRunResult(
                    "openai-compatible", _complete_translation_artifacts(job_file)
                ),
            ) as translate:
                result = continue_workflow(job_file)

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
            job_file = save_job(job)
            _write_source_leg_artifacts(job_file)
            with patch(
                "xvt.workflow.run_translation",
                side_effect=lambda job_file, **_kwargs: TranslationRunResult(
                    "openai-compatible", _complete_translation_artifacts(job_file)
                ),
            ) as translate:
                result = continue_workflow(job_file)

            self.assertEqual(result.status, "completed")
            self.assertEqual(translate.call_args.kwargs["policy"], TranslationPolicy())

    def test_continue_translates_with_the_policy_the_qa_gate_requires(self) -> None:
        """O2：快照缺 policy、顶层键有术语时，翻译侧与译文 QA 侧必须拿到同一份策略。

        这种 Job 真实可达：``WorkflowConfig.translation_policy`` 对策略快照落地之前
        创建的 Job 解析为 ``None``，而在这类 Job 上跑一次
        ``xvt translate --glossary G``（``prepare_agent_translation`` /
        ``run_translation`` 都会 ``job.config["translation_policy"] = ...``）只写顶层键，
        不回写快照。此前 ``continue`` 的翻译侧回退空 ``TranslationPolicy()``、译文 QA 侧
        （``required_glossary_terms`` → ``translation_qa_context``）回退顶层键，于是译文
        永远不带术语却永远被判 ``glossary_violation``——与 F2 同款的术语死锁。
        """

        with tempfile.TemporaryDirectory() as directory:
            job = create_job(
                "/tmp/video.mp4",
                directory,
                {
                    # 老 Job 的形状：workflow 快照里根本没有 translation_policy 这个键。
                    "workflow": {"target_language": "zh-CN", "burn": False},
                    # xvt translate --glossary G 留下的顶层键。
                    "translation_policy": {
                        "style": "natural",
                        "terms": [{"source": "OpenAI", "target": "OpenAI"}],
                    },
                },
            )
            job_file = save_job(job)
            _write_source_leg_artifacts(job_file)
            self.assertIsNone(
                require_workflow_config(load_job(job_file)).translation_policy,
                "前提：快照确实没有策略，否则这条测试没有在测死锁",
            )
            # QA 侧的要求（check_subtitles 的 required_terms 就从这里来）。
            self.assertEqual(
                required_glossary_terms(load_job(job_file)),
                (("OpenAI", "OpenAI"),),
            )

            with patch(
                "xvt.workflow.run_translation",
                side_effect=lambda job_file, **_kwargs: TranslationRunResult(
                    "openai-compatible", _complete_translation_artifacts(job_file)
                ),
            ) as translate:
                result = continue_workflow(job_file)

            self.assertEqual(result.status, "completed")
            # 翻译侧必须收到 QA 侧会要求的同一份策略，风格也一并沿用顶层键。
            self.assertEqual(
                translate.call_args.kwargs["policy"],
                TranslationPolicy(style="natural", terms=(GlossaryTerm("OpenAI", "OpenAI"),)),
            )

    def test_continue_burns_after_agent_translation_was_applied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "video.mp4"
            media.write_bytes(b"media")
            job = create_job(
                str(media),
                directory,
                {
                    "workflow": {
                        "target_language": "zh-CN",
                        "burn": True,
                        "hardware_encoding": True,
                    }
                },
            )
            job_file = save_job(job)
            _write_source_leg_artifacts(job_file)
            _complete_translation_artifacts(job_file)
            # 译文产物齐备，但 TRANSLATE 仍停在 Agent 回填前的 awaiting_input 残留态：
            # continue 必须据产物判定译文已满足并直接推进烧录。
            applied = load_job(job_file)
            applied.stages[StageName.TRANSLATE].status = StageStatus.AWAITING_INPUT
            save_job(applied)
            burned_path = Path(directory) / "burned.mp4"
            with patch(
                "xvt.workflow.burn_translated_job",
                side_effect=lambda job_file, **_kwargs: _complete_burn_artifacts(
                    job_file, burned_path
                ),
            ) as burn:
                result = continue_workflow(job_file)

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

    def test_burn_proof_registration_emits_artifact_created_event(self) -> None:
        """O6：v2 proof 的登记必须进 JSONL 事件流（AGENTS 不可破坏约束 7）。

        这份 proof 是 ``relink-burned-output`` 的唯一授权凭据，此前
        ``_record_burn_fingerprint`` 直接 ``job.artifacts.append`` + ``save_job``，
        绕开 ``add_artifact``，于是 Job 里多出一个决定授权资格的 artifact，
        而事件流里完全看不到它是什么时候、指向哪个文件登记的。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "video.mp4"
            media.write_bytes(b"media")
            job = create_job(
                str(media),
                directory,
                {"workflow": {"target_language": "zh-CN", "burn": True}},
            )
            job_file = save_job(job)
            _write_source_leg_artifacts(job_file)
            _complete_translation_artifacts(job_file)
            burned_path = root / "burned.mp4"
            events = []

            with patch(
                "xvt.workflow.burn_translated_job",
                side_effect=lambda job_file, **_kwargs: _complete_burn_artifacts(
                    job_file, burned_path
                ),
            ):
                result = continue_workflow(job_file, event_sink=events.append)

            self.assertEqual(result.status, "completed")
            registered = next(
                artifact
                for artifact in load_job(job_file).artifacts
                if artifact.kind == "burn_fingerprint"
            )
            proof_events = [
                event
                for event in events
                if event.type == EventType.ARTIFACT_CREATED
                and event.data.get("kind") == "burn_fingerprint"
            ]
            self.assertEqual(len(proof_events), 1)
            self.assertEqual(proof_events[0].data["path"], registered.path)
            self.assertEqual(proof_events[0].data["job_id"], job.id)
            self.assertEqual(proof_events[0].data["job_file"], str(job_file))
            # 事件必须排在 workflow_completed 之前：收尾宣告时 proof 已经可见。
            self.assertLess(events.index(proof_events[0]), len(events) - 1)
            self.assertEqual(events[-1].type, EventType.WORKFLOW_COMPLETED)

    def test_continue_retries_burn_when_media_qa_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "video.mp4"
            media.write_bytes(b"media")
            job = create_job(
                str(media),
                directory,
                {"workflow": {"target_language": "zh-CN", "burn": True}},
            )
            job_file = save_job(job)
            _write_source_leg_artifacts(job_file)
            _complete_translation_artifacts(job_file)
            # 上一轮烧录的真实残留：成品与媒体 QA 报告都在，但 QA 判 failed 且 stage 为 failed。
            stale_burned = root / "stale-burned.mp4"
            stale_burned.write_bytes(b"stale-burned-output")
            stale = load_job(job_file)
            stale_media_qa = _job_artifacts_directory(stale) / "media-qa.json"
            stale_media_qa.write_text('{"status": "failed", "issues": []}\n', encoding="utf-8")
            _register_artifact(stale, "burned_video", stale_burned)
            _register_artifact(stale, "media_qa", stale_media_qa)
            stale.stages[StageName.BURN].status = StageStatus.SUCCEEDED
            stale.stages[StageName.MEDIA_QA].status = StageStatus.FAILED
            save_job(stale)
            # 登记 checksum 有效的 v2 proof：指纹是匹配的，唯一的未满足项是媒体 QA。
            _record_burn_fingerprint(job_file)

            burned_path = root / "burned.mp4"
            with patch(
                "xvt.workflow.burn_translated_job",
                side_effect=lambda job_file, **_kwargs: _complete_burn_artifacts(
                    job_file, burned_path
                ),
            ) as burn:
                result = continue_workflow(job_file)

            self.assertEqual(result.status, "completed")
            burn.assert_called_once()

    def _burnable_job(self, root: Path) -> Path:
        """构造译文目标已满足、只剩烧录目标的端到端 Job（产物与 stage 均为真实状态）。"""

        media = root / "video.mp4"
        media.write_bytes(b"media")
        job = create_job(
            str(media),
            root,
            {"workflow": {"target_language": "zh-CN", "burn": True}},
        )
        job_file = save_job(job)
        _write_source_leg_artifacts(job_file)
        _complete_translation_artifacts(job_file)
        return job_file

    def test_completion_guard_rejects_planner_inconsistency(self) -> None:
        """执行侧跑完烧录但产物没真正满足时必须 fail closed，不得发 workflow_completed。

        这里注入的不一致正是 F6 那批桩制造的状态：烧录「成功」返回却没有推进媒体 QA。
        守卫之前，``_advance_workflow`` 会照常发完成事件并返回 completed，而
        ``status`` / ``batch-continue`` 的 planner 仍然列着 ``burn``。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._burnable_job(root)
            config = require_workflow_config(load_job(job_file))
            burned_path = root / "burned.mp4"
            events = []

            def incomplete_burn(job_file, **_kwargs):
                burned_path.write_bytes(b"burned-output")
                current = load_job(job_file)
                _register_artifact(current, "burned_video", burned_path)
                save_job(current)
                return burned_path

            with (
                patch("xvt.workflow.burn_translated_job", side_effect=incomplete_burn),
                self.assertRaises(InternalError) as caught,
            ):
                continue_workflow(job_file, event_sink=events.append)

            self.assertIn("burn", str(caught.exception))
            self.assertEqual(caught.exception.category, "workflow")
            self.assertEqual(caught.exception.exit_code, ExitCode.WORKFLOW_ERROR)
            types = [event.type for event in events]
            self.assertNotIn("workflow_completed", types)
            self.assertIn("workflow_failed", types)
            self.assertEqual(plan_advance_steps(load_job(job_file), config), ("burn",))

    def test_completion_guard_allows_consistent_completion(self) -> None:
        """planner 与执行侧一致时守卫不得误伤：照常发 workflow_completed。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._burnable_job(root)
            config = require_workflow_config(load_job(job_file))
            burned_path = root / "burned.mp4"
            events = []

            with patch(
                "xvt.workflow.burn_translated_job",
                side_effect=lambda job_file, **_kwargs: _complete_burn_artifacts(
                    job_file, burned_path
                ),
            ):
                result = continue_workflow(job_file, event_sink=events.append)

            self.assertEqual(result.status, "completed")
            self.assertIn("workflow_completed", [event.type for event in events])
            self.assertEqual(plan_advance_steps(load_job(job_file), config), ())

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
        from xvt.pipeline.common import write_json
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
        # 必须与生产写法（`pipeline.common.write_json`，缩进 JSON）逐字节一致：
        # 紧凑 JSON 解析后字段相同但字节不同，continue 重建报告后 checksum 会变，
        # 绑定报告 checksum 的已存确认就会被误判为失效——测试不能自带这种偏差。
        write_json(qa_path, asdict(check_subtitles([translated_cue], require_translation=True)))
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
                    burned_path = root / "workflow-burned.mp4"
                    with patch(
                        "xvt.workflow.burn_translated_job",
                        side_effect=lambda job_file, **_kwargs: _complete_burn_artifacts(
                            job_file, burned_path
                        ),
                    ) as workflow_burn:
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
                    # 本地源媒体在烧录时刻的身份。指纹不匹配时用它重算一次指纹，证明
                    # 差异只来自媒体身份（而不是字体/编码选项漂移），这是内容 checksum
                    # 回退的前提。远程媒体不登记：它在指纹里的身份本就是内容 checksum。
                    "source_media_identity": {
                        "path": str(media.resolve()),
                        "size": media.stat().st_size,
                        "mtime_ns": media.stat().st_mtime_ns,
                    },
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

    def test_burn_not_satisfied_when_media_qa_blocks_a_valid_burned_output(self) -> None:
        """成品与指纹都有效时，媒体 QA 的两道判定仍必须各自独立地否决「已完成」。

        这两道判定（MEDIA_QA stage 必须 succeeded、媒体 QA 报告 status 必须 pass）
        此前零覆盖：唯一相邻的 relink 测试都先删掉了烧录成品，``_burn_satisfied``
        在第一道 ``burned_video`` 校验就早退了，媒体 QA 分支根本走不到。
        放过的 bug 是「legacy Job 原子重烧后媒体 QA 失败，continue / status 判已完成」，
        直接违反 AGENTS 不可破坏约束第 4 条。本测试不打任何桩。
        """

        for case in ("media_qa_stage", "media_qa_report_status"):
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as directory:
                    job_file, _burned, _candidate = self._relinkable_job(Path(directory))
                    # 基线：成品存在、媒体 QA 通过、v2 proof 匹配 → 目标已满足。
                    satisfied = load_job(job_file)
                    config = WorkflowConfig.from_dict(satisfied.config["workflow"])
                    self.assertTrue(_burn_satisfied(satisfied, config))
                    self.assertEqual(plan_advance_steps(satisfied, config), ())

                    blocked = load_job(job_file)
                    media_qa_path = Path(
                        next(
                            artifact.path
                            for artifact in blocked.artifacts
                            if artifact.kind == "media_qa"
                        )
                    )
                    if case == "media_qa_stage":
                        # 报告仍是 pass，只有 stage 停在 failed（原子重烧后的真实残留）。
                        blocked.stages[StageName.MEDIA_QA].status = StageStatus.FAILED
                    else:
                        # stage 仍是 succeeded，只有报告判定不是 pass；checksum 一并更新，
                        # 否则会被「报告失效」这条更早的判定接走，测不到 status 比较。
                        media_qa_path.write_text(
                            '{"status": "failed", "issues": []}\n', encoding="utf-8"
                        )
                        blocked.artifacts = [
                            Artifact("media_qa", str(media_qa_path), file_checksum(media_qa_path))
                            if artifact.kind == "media_qa"
                            else artifact
                            for artifact in blocked.artifacts
                        ]
                    save_job(blocked)

                    blocked = load_job(job_file)
                    self.assertIsNotNone(valid_artifact_path(blocked, "burned_video"))
                    self.assertIsNotNone(valid_artifact_path(blocked, "media_qa"))
                    self.assertFalse(_burn_satisfied(blocked, config))
                    self.assertEqual(plan_advance_steps(blocked, config), ("burn",))

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

    def test_source_media_touch_keeps_the_burn_complete(self) -> None:
        """只动源媒体 mtime（内容、size、路径都没变）不得把完成态判失效。

        此前 ``_burn_fingerprint`` 的本地分支把媒体身份取成 ``{path, size, mtime_ns}``，
        于是一次 ``touch``、不带 ``-p`` 的 ``cp``、不带 ``-a`` 的 ``rsync`` 和备份还原
        都会让 ``status`` 从 complete 翻成 retryable，代价是整段重编码。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, _burned, _candidate = self._relinkable_job(root)
            media = root / "video.mp4"
            before = media.stat()
            os.utime(media, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))

            self.assertNotEqual(media.stat().st_mtime_ns, before.st_mtime_ns)
            self.assertEqual(media.stat().st_size, before.st_size)
            job = load_job(job_file)
            config = require_workflow_config(job)
            self.assertTrue(_burn_satisfied(job, config))
            self.assertEqual(plan_advance_steps(job, config), ())
            self.assertEqual(build_recovery_plan(job_file).category, "complete")

    def test_touch_fallback_requires_the_recorded_media_identity(self) -> None:
        """没有登记烧录时媒体身份的存量 v2 proof 拿不到回退：照旧判失效（fail closed）。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, _burned, _candidate = self._relinkable_job(root)
            job = load_job(job_file)
            proof_path = Path(
                next(item.path for item in job.artifacts if item.kind == "burn_fingerprint")
            )
            document = json.loads(proof_path.read_text(encoding="utf-8"))
            self.assertIn("source_media_identity", document)
            document.pop("source_media_identity")
            proof_path.write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            job.artifacts = [
                Artifact(item.kind, item.path, file_checksum(proof_path))
                if item.kind == "burn_fingerprint"
                else item
                for item in job.artifacts
            ]
            save_job(job)
            media = root / "video.mp4"
            stat = media.stat()
            os.utime(media, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

            job = load_job(job_file)
            config = require_workflow_config(job)
            self.assertFalse(_burn_satisfied(job, config))
            self.assertEqual(plan_advance_steps(job, config), ("burn",))

    def test_touch_fallback_refuses_changed_source_media_content(self) -> None:
        """源媒体内容真的变了（size 相同也不行）：回退必须照旧判失效并要求重烧。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, _burned, _candidate = self._relinkable_job(root)
            media = root / "video.mp4"
            size = media.stat().st_size
            media.write_bytes(b"other")
            self.assertEqual(media.stat().st_size, size)

            job = load_job(job_file)
            config = require_workflow_config(job)
            self.assertFalse(_burn_satisfied(job, config))
            self.assertEqual(plan_advance_steps(job, config), ("burn",))
            self.assertEqual(build_recovery_plan(job_file).category, "retryable")

    def test_relink_names_missing_source_media_instead_of_a_stale_fingerprint(self) -> None:
        """源媒体不在原路径时 relink 必须点名那个路径，而不是归因成「指纹过期」。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, burned, candidate = self._relinkable_job(root)
            media = root / "video.mp4"
            burned.unlink()
            media.unlink()
            before = job_file.read_bytes()

            with self.assertRaises(UserInputError) as caught:
                relink_burned_output(job_file, candidate)

            message = str(caught.exception)
            self.assertIn("source media is missing", message)
            self.assertIn(str(media), message)
            self.assertNotIn("stale", message)
            self.assertEqual(job_file.read_bytes(), before)

    def test_recovery_needs_manual_fix_when_source_media_is_missing(self) -> None:
        """源媒体不在登记路径时 render / burn 都必然失败，不得再推荐 continue。

        ``_relinkable_job`` 签发的是 checksum 有效的 v2 proof，因此这个 Job 够格做
        ``relink-source-media``：首选命令给它（占位符待用户替换成新路径）。原先这里断的是
        ``primary_command is None``——那是把「不推荐 continue」这个意图过度收紧成了
        「一条命令都不给」；意图本身改用下面的 ``assertNotIn`` 显式表达，而
        ``is None`` 搬到了下一条「没有 v2 proof」的反证测试上，它在那里才真正成立。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, _burned, _candidate = self._relinkable_job(root)
            media = root / "video.mp4"
            media.unlink()

            plan = build_recovery_plan(job_file)

            self.assertEqual(plan.category, "manual_fix")
            self.assertEqual(
                plan.primary_command,
                f"xvt relink-source-media {shlex.quote(str(job_file))} NEW_MEDIA_PATH",
            )
            self.assertNotIn("xvt continue", str(plan.primary_command))
            self.assertEqual(plan.alternative_commands, ())
            self.assertIn(str(media), plan.reason)
            self.assertEqual(plan.advance_steps, ("burn",))

    def test_recovery_withholds_relink_source_media_without_a_v2_proof(self) -> None:
        """没有 checksum 有效的 v2 proof 时绝不推荐 ``relink-source-media``。

        那条命令本身要求 v2 proof，拿不到就以退出码 2 失败；在这种 Job 上把它放进推荐位
        就是又一次「推荐一条在该状态下必然失败的命令」。legacy fingerprint 的旧 Job 维持
        原状：不给首选命令，只点名缺失的那个路径。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, _burned, _candidate = self._relinkable_job(root)
            job = load_job(job_file)
            job.artifacts = [item for item in job.artifacts if item.kind != "burn_fingerprint"]
            job.config["workflow"]["burn_fingerprint"] = "legacy"
            save_job(job)
            media = root / "video.mp4"
            media.unlink()

            plan = build_recovery_plan(job_file)

            self.assertEqual(plan.category, "manual_fix")
            self.assertIsNone(plan.primary_command)
            self.assertEqual(plan.alternative_commands, ())
            self.assertNotIn("relink-source-media", plan.reason)
            self.assertIn(str(media), plan.reason)
            self.assertEqual(plan.advance_steps, ("burn",))

    def test_missing_burn_source_media_skips_remote_jobs_when_local_only(self) -> None:
        """``local_only`` 的刻意差异：远程 Job 的下载产物缺失时 continue 能重新获取。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloaded = root / "downloaded.mp4"
            job = create_job("https://example.com/watch?v=abc", root)
            job.artifacts.append(Artifact("source_video", str(downloaded), "deadbeef"))
            config = WorkflowConfig.from_dict({"target_language": "zh-CN", "burn": True})

            self.assertEqual(missing_burn_source_media(job, config), downloaded)
            self.assertIsNone(missing_burn_source_media(job, config, local_only=True))

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

    # ``relink-source-media``：源媒体永久搬家后把持久化路径指到新位置。
    # 状态机本来就处理得了「媒体搬家」——内容 checksum 回退（见
    # docs/RECOVERY.md#指纹不匹配时的源媒体内容-checksum-回退）在路径变了、内容没变时
    # 仍判指纹成立。缺的只是一个受支持的方式去改那个路径：此前只能手改 job.json。
    # 下面这组钉住「命令真的把 Job 从 manual_fix 带回 complete，且一帧都没有重编码」，
    # 以及它的 fail-closed 反面。复用同一份 _relinkable_job 夹具，不另起一个类
    # （继承它会把父类全部用例再跑一遍）。

    def _moved_media(self, root: Path) -> Path:
        media = root / "video.mp4"
        moved = root / "newhome" / "video.mp4"
        moved.parent.mkdir(parents=True)
        media.rename(moved)
        return moved

    def test_relink_source_media_repoints_burn_media_without_reburn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, burned, _candidate = self._relinkable_job(root)
            media = root / "video.mp4"
            moved = self._moved_media(root)

            stale = load_job(job_file)
            stale_config = require_workflow_config(stale)
            self.assertFalse(_burn_satisfied(stale, stale_config))
            self.assertEqual(plan_advance_steps(stale, stale_config), ("burn",))
            self.assertEqual(build_recovery_plan(job_file).category, "manual_fix")
            artifacts_before = [(item.kind, item.path, item.checksum) for item in stale.artifacts]
            burned_stat = burned.stat()

            result = relink_source_media(job_file, moved)

            job = load_job(job_file)
            config = require_workflow_config(job)
            self.assertEqual(result, moved.resolve())
            self.assertEqual(config.burn_media, str(moved.resolve()))
            self.assertTrue(_burn_satisfied(job, config))
            self.assertEqual(plan_advance_steps(job, config), ())
            self.assertEqual(build_recovery_plan(job_file).category, "complete")
            # 零重烧：成品与 proof 都没有被重写（mtime 与 checksum 都不变）。
            self.assertEqual(
                [(item.kind, item.path, item.checksum) for item in job.artifacts],
                artifacts_before,
            )
            self.assertEqual(burned.stat().st_mtime_ns, burned_stat.st_mtime_ns)
            self.assertFalse(media.exists())

            # 幂等：已经指向这里时不再写盘。
            before = job_file.read_bytes()
            self.assertEqual(relink_source_media(job_file, moved), moved.resolve())
            self.assertEqual(job_file.read_bytes(), before)

    def test_relink_source_media_rejects_unproven_candidates_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = {
                "content": "checksum does not match",
                "missing": "does not exist",
                "directory": "is not a file",
                "legacy_proof": "v2 burn proof",
                "remote": "remote media job",
            }
            for case in expected:
                with self.subTest(case=case):
                    case_root = root / case
                    case_root.mkdir()
                    job_file, _burned, _candidate = self._relinkable_job(case_root)
                    moved = self._moved_media(case_root)
                    job = load_job(job_file)
                    if case == "content":
                        moved.write_bytes(b"other-media")
                    elif case == "missing":
                        moved.unlink()
                    elif case == "directory":
                        moved.unlink()
                        moved.mkdir()
                    elif case == "legacy_proof":
                        job.artifacts = [
                            item for item in job.artifacts if item.kind != "burn_fingerprint"
                        ]
                        job.config["workflow"]["burn_fingerprint"] = "legacy"
                        save_job(job)
                    else:
                        job.input = "https://example.com/watch?v=abc"
                        save_job(job)
                    before = job_file.read_bytes()

                    with self.assertRaises(UserInputError) as caught:
                        relink_source_media(job_file, moved)

                    self.assertIn(expected[case], str(caught.exception))
                    self.assertEqual(job_file.read_bytes(), before)
                    self.assertIsNone(require_workflow_config(load_job(job_file)).burn_media)

    def test_relink_source_media_rejects_a_copied_job_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, _burned, _candidate = self._relinkable_job(root)
            moved = self._moved_media(root)
            copied_job = root / "copied-job.json"
            copied_job.write_bytes(job_file.read_bytes())

            with self.assertRaisesRegex(UserInputError, "persisted identity"):
                relink_source_media(copied_job, moved)

            self.assertEqual(copied_job.read_bytes(), job_file.read_bytes())

    def test_relink_source_media_rejects_jobs_without_a_burn_goal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "video.mp4"
            media.write_bytes(b"media")
            job = create_job(str(media), root)
            job_file = save_job(job)
            before = job_file.read_bytes()

            with self.assertRaisesRegex(UserInputError, "workflow goal"):
                relink_source_media(job_file, media)

            self.assertEqual(job_file.read_bytes(), before)

            job = load_job(job_file)
            job.config["workflow"] = {"target_language": "zh-CN", "burn": False}
            save_job(job)
            before = job_file.read_bytes()

            with self.assertRaisesRegex(UserInputError, "burned-video goal"):
                relink_source_media(job_file, media)

            self.assertEqual(job_file.read_bytes(), before)

    def test_relink_source_media_accepts_proofs_without_the_recorded_identity(self) -> None:
        """存量 v2 proof（没有 ``source_media_identity``）仍可重指路径，但要重烧一次。

        授权依据是 proof 的 ``source_media_checksum``，那是 v2 的必需字段，匹配即证明
        是同一份媒体，写入是正确的。零重烧是 ``_burn_fingerprint_satisfied`` 回退的性质，
        不是本命令的承诺：缺身份字段时回退拿不到授权，Job 照旧落到 ``burn``——那也是
        用户能真正推进的出口（否则他只剩手改 job.json）。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file, _burned, _candidate = self._relinkable_job(root)
            job = load_job(job_file)
            proof_path = Path(
                next(item.path for item in job.artifacts if item.kind == "burn_fingerprint")
            )
            document = json.loads(proof_path.read_text(encoding="utf-8"))
            document.pop("source_media_identity")
            proof_path.write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            job.artifacts = [
                Artifact(item.kind, item.path, file_checksum(proof_path))
                if item.kind == "burn_fingerprint"
                else item
                for item in job.artifacts
            ]
            save_job(job)
            moved = self._moved_media(root)

            self.assertEqual(relink_source_media(job_file, moved), moved.resolve())

            job = load_job(job_file)
            config = require_workflow_config(job)
            self.assertEqual(config.burn_media, str(moved.resolve()))
            self.assertFalse(_burn_satisfied(job, config))
            self.assertEqual(plan_advance_steps(job, config), ("burn",))
            # 出口真的能推进：媒体就在新路径上，重烧不再撞「video file does not exist」。
            self.assertIsNone(missing_burn_source_media(job, config))


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
            # 批次 manifest 必须有效，这次等待才真的在途（manifest 失效时 planner 按 F3
            # 给出可推进的 translate；本测试要守的是「等待步骤不追加 burn」）。
            with patch(
                "xvt.workflow.valid_artifact_path",
                side_effect=lambda _job, kind: (
                    Path("/tmp/translation-batches.json") if kind == "translation_batches" else None
                ),
            ):
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


class FullRoundAgentTranslationWaitTest(unittest.TestCase):
    """整轮 Agent 翻译等待叠加在「译文目标已满足」的 Job 上时必须对三侧可见。

    与 ``await_cue_retranslation`` 同构的一类缺陷：``_translation_satisfied`` 为真会
    把一次真实的在途等待短路成空计划，于是 ``status`` 报 complete、``continue`` 报
    完成、``xvt apply`` 又说「job is not awaiting Agent input」，导航闭环断掉。

    夹具与 ``CueRetranslationWaitingTest`` 共用（显式引用而不是继承，避免把那边的
    用例在本类里重跑一遍）。
    """

    _translated_job = CueRetranslationWaitingTest._translated_job
    _request_retranslation = CueRetranslationWaitingTest._request_retranslation

    def _start_full_round(self, job_file: Path) -> Path:
        """等价于用户执行 ``xvt translate JOB --target-language zh-CN --provider agent``。"""

        return prepare_agent_translation(
            job_file,
            source_language="en",
            target_language="zh-CN",
            await_input=True,
        )

    def test_full_round_wait_is_visible_on_a_satisfied_job(self) -> None:
        """译文目标已满足的 Job 上新开一轮整轮等待：planner / status / continue 三侧一致。"""

        events: list[dict] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root)
            job = load_job(job_file)
            config = require_workflow_config(job)
            self.assertEqual(plan_advance_steps(job, config), ())

            manifest_path = self._start_full_round(job_file)

            job = load_job(job_file)
            # 前提：旧译文刻意保留（等待期间对用户仍有价值），所以译文目标仍然「已满足」。
            self.assertIsNotNone(valid_artifact_path(job, "translated_cues"))
            self.assertIsNotNone(valid_artifact_path(job, "translated_subtitle_qa"))
            self.assertIs(job.stages[StageName.TRANSLATE].status, StageStatus.AWAITING_INPUT)
            self.assertEqual(plan_advance_steps(job, config), ("await_agent_translation",))

            plan = build_recovery_plan(job_file)
            result = continue_workflow(job_file, event_sink=events.append)

        self.assertEqual(plan.category, "awaiting_input")
        self.assertEqual(plan.advance_steps, ("await_agent_translation",))
        self.assertIn("xvt apply ", plan.prerequisite_commands[0])
        self.assertEqual(result.status, "awaiting_input")
        self.assertIn(manifest_path, result.artifacts)
        types = [event.type for event in events]
        self.assertEqual(types.count(EventType.WORKFLOW_WAITING.value), 1)
        self.assertNotIn(EventType.WORKFLOW_COMPLETED.value, types)

    def test_full_round_supersedes_a_pending_cue_retranslation(self) -> None:
        """E2E 复现路径：按 status 的提示用整轮翻译取代重译，不得两头落空。"""

        events: list[dict] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root)
            self._request_retranslation(job_file)
            self.assertTrue(cue_retranslation_waiting(load_job(job_file)))

            manifest_path = self._start_full_round(job_file)

            job = load_job(job_file)
            # prepare_agent_translation 把重译 manifest 移出登记，整轮请求成为唯一在途等待。
            self.assertIsNone(valid_artifact_path(job, "cue_retranslation_batches"))
            self.assertFalse(cue_retranslation_waiting(job))
            self.assertEqual(
                plan_advance_steps(job, require_workflow_config(job)),
                ("await_agent_translation",),
            )

            plan = build_recovery_plan(job_file)
            result = continue_workflow(job_file, event_sink=events.append)

        self.assertEqual(plan.category, "awaiting_input")
        self.assertEqual(plan.advance_steps, ("await_agent_translation",))
        self.assertNotIn("retranslate-cues", " ".join(plan.prerequisite_commands))
        self.assertEqual(result.status, "awaiting_input")
        self.assertIn(manifest_path, result.artifacts)
        self.assertNotIn(EventType.WORKFLOW_COMPLETED.value, [event.type for event in events])

    def test_a_newer_cue_retranslation_request_still_wins(self) -> None:
        """护栏：重译请求登记更晚时仍由 await_cue_retranslation 接住，不被整轮等待顶掉。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root)
            self._start_full_round(job_file)
            self._request_retranslation(job_file)

            job = load_job(job_file)
            self.assertIsNotNone(valid_artifact_path(job, "translation_batches"))
            self.assertEqual(
                plan_advance_steps(job, require_workflow_config(job)),
                ("await_cue_retranslation",),
            )


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
            patch("xvt.pipeline.transcription.local.probe_media", return_value=media_info),
            patch("xvt.pipeline.transcription.local.run_command", side_effect=run_ffmpeg),
            patch("xvt.pipeline.transcription.local.transcribe_audio", return_value=transcript),
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


class StaleAgentManifestPlanTest(unittest.TestCase):
    """F3：批次 manifest 失效时，端到端 Job 必须拿到可推进的出口而不是死等。

    manifest 无效 + 没有译文 cue + ``TRANSLATE`` 停在 ``awaiting_input`` 时，planner 的兜底
    分支旧实现照样返回 ``await_agent_translation``：``status`` 说等回填，``apply`` 被 manifest
    校验拒绝，``continue`` 只发 waiting，三条命令互指，用户没有出口。``recovery`` 的原子分支
    对同一状态早已给出 retryable + ``translate``，planner 必须同口径。
    """

    def _job_file(self, root: Path) -> Path:
        source = root / "source.srt"
        source.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHello.\n",
            encoding="utf-8",
        )
        job = run_subtitle_transcription(source, root, language="en")
        set_workflow_config(job, WorkflowConfig(target_language="zh-CN"))
        job_file = save_job(job)
        manifest_path = prepare_agent_translation(
            job_file,
            source_language="en",
            target_language="zh-CN",
        )
        # 登记仍在、内容已变：checksum 失效，任何响应都回填不进这次等待。
        manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
        job = load_job(job_file)
        self.assertIs(job.stages[StageName.TRANSLATE].status, StageStatus.AWAITING_INPUT)
        self.assertIsNone(valid_artifact_path(job, "translation_batches"))
        return job_file

    def test_planner_and_status_ask_for_a_rebuild_instead_of_a_dead_wait(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job_file(Path(directory))
            job = load_job(job_file)

            steps = plan_advance_steps(job, require_workflow_config(job))
            plan = build_recovery_plan(job_file)

        self.assertEqual(steps, ("translate",))
        self.assertEqual(plan.advance_steps, ("translate",))
        self.assertEqual(plan.category, "retryable")
        self.assertIn("continue", plan.primary_command or "")

    def test_continue_rebuilds_the_manifest_so_the_wait_has_an_exit(self) -> None:
        events: list = []
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job_file(Path(directory))
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("XVT_TRANSLATION_API_KEY", None)
                result = continue_workflow(job_file, event_sink=events.append)
            job = load_job(job_file)
            # 必须在 tempdir 仍存在时判定：有效性只认内容 checksum，要真的读文件。
            manifest_valid = valid_artifact_path(job, "translation_batches") is not None
            steps = plan_advance_steps(job, require_workflow_config(job))

        self.assertEqual(result.status, "awaiting_input")
        self.assertTrue(manifest_valid)
        self.assertEqual(steps, ("await_agent_translation",))


class RelinkMediaQaMessageCoverageTest(unittest.TestCase):
    """`media_qa_failure` 的每个标签都必须有对应的 relink 报错文案。

    两者是「判定」与「文案」的分离：判定返回字符串标签，relink 侧按标签查表。
    新增标签却忘了加文案时，用户拿到的是 ``KeyError`` 而不是可读报错，而标签类型
    只是 ``str | None``，静态检查拦不住。这里用 ast 取 ``media_qa_failure`` 体内
    return 的字面量，与文案表的键集合比对，把这条脱节钉住。
    """

    def test_every_failure_label_has_a_relink_message(self) -> None:
        source = Path(xvt.workflow.__file__).read_text(encoding="utf-8")
        function = next(
            node
            for node in ast.parse(source).body
            if isinstance(node, ast.FunctionDef) and node.name == "media_qa_failure"
        )
        labels = {
            node.value.value
            for node in ast.walk(function)
            if isinstance(node, ast.Return)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        }

        self.assertTrue(labels, "没有从 media_qa_failure 解析出任何标签，守卫会空转")
        self.assertEqual(labels, set(_RELINK_MEDIA_QA_MESSAGES))


class WorkflowReusesPipelineHelpersTest(unittest.TestCase):
    """`workflow.py` 只编排，不复制 pipeline 的判定实现（AGENTS「代码结构」）。

    F5：这三个名字曾在 `workflow.py` 里各有一份逐字副本（`_source_selection_satisfied`
    / `_artifact_checksum` / `_error_category`），只差一个 helper 名。给
    `selected_kind` 加新分支时只改一边，`continue` 与 `resume` 对「源是否满足」的判定
    就会分叉，而两边都绿。这里用对象身份把「workflow 用的就是 pipeline 那一份」钉死：
    任何人重新在 workflow.py 里定义同名副本，`assertIs` 立刻失败。
    """

    def test_source_selection_predicate_is_the_pipeline_implementation(self) -> None:
        self.assertIs(
            xvt.workflow.source_selection_matches,
            xvt.pipeline.transcription.source_selection_matches,
        )

    def test_artifact_checksum_helper_is_the_pipeline_implementation(self) -> None:
        self.assertIs(
            xvt.workflow.artifact_checksum_for,
            xvt.pipeline.common.artifact_checksum_for,
        )

    def test_error_category_helper_is_the_pipeline_implementation(self) -> None:
        self.assertIs(xvt.workflow.error_category, xvt.pipeline.common.error_category)

    def test_batch_error_category_helper_is_the_pipeline_implementation(self) -> None:
        """`batch.py` 曾有第四份同样的副本，一并钉住。

        它和 workflow 的那三份是同一个气味：批处理自己算一遍 error category，
        哪天 `common.error_category` 改了分类规则，批处理条目的 `error.category`
        就会和 Job 事件里的对不上，而两边都绿。
        """

        self.assertIs(xvt.batch.error_category, xvt.pipeline.common.error_category)

    def test_translation_policy_fallback_is_the_pipeline_implementation(self) -> None:
        """O2：policy 回退曾有四份、两派语义，翻译侧与 QA 侧能各走各的。

        `workflow._translation_policy` / `translation.translation_progress_rebuildable`
        缺快照时回退**空策略**，`common.resolved_translation_policy` /
        `semantic_state.resolved_policy` 回退**顶层键**。快照缺失而顶层键有术语的 Job
        上，翻译不带术语、QA 按术语判 `glossary_violation`，术语死锁没有出口。
        四份已收敛成 `common.resolved_translation_policy` 一份，这里用对象身份钉住；
        任何人再在 workflow / translation / semantic 里定义一份同名副本都会立刻变红。
        """

        self.assertIs(
            xvt.workflow.resolved_translation_policy,
            xvt.pipeline.common.resolved_translation_policy,
        )
        self.assertIs(
            xvt.pipeline.translation.resolved_translation_policy,
            xvt.pipeline.common.resolved_translation_policy,
        )
        self.assertIs(
            xvt.pipeline.semantic.resolved_translation_policy,
            xvt.pipeline.common.resolved_translation_policy,
        )
        self.assertIs(
            xvt.pipeline.semantic_state.resolved_translation_policy,
            xvt.pipeline.common.resolved_translation_policy,
        )
        # `semantic_state.resolved_policy` 是被吃掉的那第三份，不许复活。
        self.assertFalse(hasattr(xvt.pipeline.semantic_state, "resolved_policy"))
        self.assertFalse(hasattr(xvt.workflow, "_translation_policy"))


class AwaitStepsCoverageTest(unittest.TestCase):
    """`AWAIT_STEPS` 必须与 `plan_advance_steps` 实际返回的等待步骤名一致。

    O7a：`AWAIT_STEPS` 是手写集合，而步骤名散在 `plan_advance_steps` 的各条 return 里。
    漏加一个新等待步骤，`recovery` 的让路判定（`workflow_steps[0] in AWAIT_STEPS`）与
    统一 `xvt apply` 的路由会同时静默失效——两处都只是「不命中」，不报错。
    这里用 ast 取 planner 各条 `return` 的字符串字面量（含元组里的）与 `steps.append`
    的字面量，凡以 `await_` 开头的都必须在 `AWAIT_STEPS` 里；反向也核，避免清单里
    留下再也不会出现的死条目。
    """

    @staticmethod
    def _planner_await_step_names() -> set[str]:
        source = Path(xvt.workflow.__file__).read_text(encoding="utf-8")
        function = next(
            node
            for node in ast.parse(source).body
            if isinstance(node, ast.FunctionDef) and node.name == "plan_advance_steps"
        )
        literals: set[str] = set()
        for node in ast.walk(function):
            # 步骤名只有两条出路：直接 return（可能包在元组/tuple() 里），或先 append
            # 进 steps 再统一 return tuple(steps)。两条都收，免得改写形式就绕过守卫。
            if isinstance(node, ast.Return) and node.value is not None:
                sources: list[ast.AST] = [node.value]
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
            ):
                sources = list(node.args)
            else:
                continue
            for root in sources:
                for child in ast.walk(root):
                    if isinstance(child, ast.Constant) and isinstance(child.value, str):
                        literals.add(child.value)
        return {name for name in literals if name.startswith("await_")}

    def test_planner_await_steps_are_all_declared(self) -> None:
        planner_steps = self._planner_await_step_names()

        self.assertTrue(
            planner_steps,
            "没有从 plan_advance_steps 解析出任何 await_ 步骤名，守卫会空转",
        )
        undeclared = planner_steps - xvt.workflow.AWAIT_STEPS
        self.assertEqual(
            undeclared,
            set(),
            "plan_advance_steps 返回了未登记进 workflow.AWAIT_STEPS 的等待步骤："
            f"{sorted(undeclared)}；recovery 的让路判定与 xvt apply 的路由都会静默失效",
        )

    def test_declared_await_steps_are_all_reachable(self) -> None:
        stale = xvt.workflow.AWAIT_STEPS - self._planner_await_step_names()
        self.assertEqual(
            stale,
            set(),
            f"AWAIT_STEPS 里有 plan_advance_steps 已经不再返回的死条目：{sorted(stale)}",
        )

    @staticmethod
    def _await_literals(node: ast.AST, *, skip: ast.AST | None = None) -> set[str]:
        """收集子树里出现的 ``await_*`` 步骤名；``skip`` 指向的子树整体跳过。"""

        literals: set[str] = set()
        stack: list[ast.AST] = [node]
        while stack:
            current = stack.pop()
            if current is skip:
                continue
            sources: list[ast.AST] = []
            if isinstance(current, ast.Return) and current.value is not None:
                sources = [current.value]
            elif (
                isinstance(current, ast.Call)
                and isinstance(current.func, ast.Attribute)
                and current.func.attr == "append"
            ):
                sources = list(current.args)
            for root in sources:
                for child in ast.walk(root):
                    if isinstance(child, ast.Constant) and isinstance(child.value, str):
                        if child.value.startswith("await_"):
                            literals.add(child.value)
            stack.extend(ast.iter_child_nodes(current))
        return literals

    def test_await_steps_state_their_position_against_the_satisfied_gate(self) -> None:
        """每个等待步骤都必须就「会不会被『译文目标已满足』盖掉」表态。

        这是同一类缺陷的第四次之后加的收敛网：``_translation_satisfied`` 为真时，埋在
        `not translation_satisfied` 分支里的等待 return 根本不会被求值，planner 返回空
        计划，status / continue / batch-continue 一起报完成。要么写一条满足度判定之前的
        早退，要么登记进 ``workflow._SATISFIED_GATED_AWAIT_STEPS`` 并说明理由。
        """

        source = Path(xvt.workflow.__file__).read_text(encoding="utf-8")
        function = next(
            node
            for node in ast.parse(source).body
            if isinstance(node, ast.FunctionDef) and node.name == "plan_advance_steps"
        )
        gate = next(
            (
                node
                for node in ast.walk(function)
                if isinstance(node, ast.If)
                and "not translation_satisfied" in ast.unparse(node.test)
            ),
            None,
        )
        self.assertIsNotNone(
            gate,
            "没在 plan_advance_steps 里找到 `not translation_satisfied` 这道满足度闸门，"
            "守卫会空转；改写闸门形式时请同步本守卫",
        )
        assert gate is not None
        early = self._await_literals(function, skip=gate)
        gated = self._await_literals(gate) - early

        self.assertEqual(
            gated,
            set(xvt.workflow._SATISFIED_GATED_AWAIT_STEPS),
            "只在「译文目标未满足」分支里给出的等待步骤必须与 "
            "workflow._SATISFIED_GATED_AWAIT_STEPS 完全一致："
            f"未表态的 {sorted(gated - xvt.workflow._SATISFIED_GATED_AWAIT_STEPS)}，"
            f"已不再被闸门挡住的 {sorted(xvt.workflow._SATISFIED_GATED_AWAIT_STEPS - gated)}",
        )
        self.assertTrue(
            early,
            "满足度判定之前一条早退都没有，整轮/局部等待会重新被『已满足』短路",
        )


if __name__ == "__main__":
    unittest.main()

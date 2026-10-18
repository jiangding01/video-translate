import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xvt.errors import ExitCode, UserInputError
from xvt.job_store import get_job_file, load_job, save_job
from xvt.models import Cue, StageName, StageStatus
from xvt.pipeline import (
    agent_translate_and_polish_core_satisfied,
    apply_agent_semantic_review,
    apply_agent_translation,
    apply_agent_translation_polish,
    apply_cue_retranslation,
    apply_subtitle_translation,
    prepare_agent_semantic_review,
    prepare_agent_translation,
    retranslate_cues,
    run_atomic_translation_polish,
    run_subtitle_transcription,
    run_translation,
    run_translation_polish,
    translation_polish_satisfied,
)
from xvt.recovery import file_checksum
from xvt.translation_provider import TranslationProviderError
from xvt.workflow import plan_advance_steps
from xvt.workflow_config import WorkflowConfig, require_workflow_config, set_workflow_config


class TranslationPolishPipelineTest(unittest.TestCase):
    def _write_polish_response(
        self,
        job_file: Path,
        path: Path,
        items: list[dict],
        *,
        fingerprint: str | None = None,
    ) -> None:
        job = load_job(job_file)
        manifest_path = next(
            Path(artifact.path)
            for artifact in job.artifacts
            if artifact.kind == "translation_polish_batches"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        path.write_text(
            json.dumps(
                {
                    "fingerprint": fingerprint or manifest["fingerprint"],
                    "items": items,
                }
            ),
            encoding="utf-8",
        )

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
                        {"id": cue_id, "text": f"草稿{cue_id}"}
                        for cue_id in range(1, cue_count + 1)
                    ]
                }
            ),
            encoding="utf-8",
        )
        apply_agent_translation(job_file, response, target_language="zh-CN")
        job = load_job(job_file)
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
        return job_file

    def _complete_semantic_review(self, job_file: Path, root: Path) -> None:
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
        apply_agent_semantic_review(job_file, response)

    def test_agent_polish_round_trip_preserves_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root)

            result = run_translation_polish(job_file)

            self.assertEqual(result.mode, "agent")
            manifest = json.loads(result.paths[0].read_text(encoding="utf-8"))
            self.assertEqual(manifest["prompt_version"], 3)
            self.assertEqual(manifest["batches"][0]["items"][0]["draft_text"], "草稿1")
            response = root / "polish-response.json"
            self._write_polish_response(
                job_file,
                response,
                [{"id": 1, "text": "润色一，"}, {"id": 2, "text": "润色二。"}],
            )
            apply_agent_translation_polish(job_file, response)

            job = load_job(job_file)
            kinds = {artifact.kind for artifact in job.artifacts}
            self.assertTrue(
                {
                    "translation_draft_cues",
                    "translation_polish_batches",
                    "translation_polish_progress",
                    "translation_polished_cues",
                }
                <= kinds
            )
            self.assertNotIn("translated_cues", kinds)
            polished_path = next(
                Path(artifact.path)
                for artifact in job.artifacts
                if artifact.kind == "translation_polished_cues"
            )
            polished = [Cue(**item) for item in json.loads(polished_path.read_text())]
            self.assertEqual([cue.target_text for cue in polished], ["润色一", "润色二"])
            self.assertEqual(
                [(cue.id, cue.start_ms, cue.end_ms) for cue in polished],
                [(1, 0, 1000), (2, 1000, 2000)],
            )
            self.assertEqual(job.config["workflow"]["translation_quality"], "polished")

    def test_agent_polish_remains_applicable_when_auto_provider_becomes_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root)
            job = load_job(job_file)
            job.config["workflow"]["provider"] = "auto"
            save_job(job)
            missing_provider = {
                "XVT_TRANSLATION_BASE_URL": "",
                "XVT_TRANSLATION_MODEL": "",
                "XVT_TRANSLATION_API_KEY": "",
            }
            with patch.dict(os.environ, missing_provider, clear=False):
                result = run_translation_polish(job_file)
            self.assertEqual(result.mode, "agent")
            response = root / "polish-response.json"
            self._write_polish_response(
                job_file,
                response,
                [{"id": 1, "text": "润色一"}, {"id": 2, "text": "润色二"}],
            )

            available_provider = {
                "XVT_TRANSLATION_BASE_URL": "https://provider.example.com/v1",
                "XVT_TRANSLATION_MODEL": "test-model",
                "XVT_TRANSLATION_API_KEY": "secret",
            }
            with patch.dict(os.environ, available_provider, clear=False):
                job = load_job(job_file)
                self.assertEqual(
                    plan_advance_steps(job, require_workflow_config(job)),
                    ("await_agent_polish",),
                )
                apply_agent_translation_polish(job_file, response)

    def test_missing_polish_response_file_is_a_user_input_error(self) -> None:
        """等待态正确、只是 response 路径打错：属于用户输入错误（2），不是依赖故障（3）。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root)
            run_translation_polish(job_file)

            with self.assertRaisesRegex(UserInputError, "cannot read polish response") as caught:
                apply_agent_translation_polish(job_file, root / "gone" / "polish-response.json")

            self.assertEqual(caught.exception.exit_code, ExitCode.USER_ERROR)

    def test_agent_polish_rejects_old_prompt_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root)
            with patch("xvt.pipeline.polish.TRANSLATION_POLISH_PROMPT_VERSION", 1):
                result = run_translation_polish(job_file)
            manifest = json.loads(result.paths[0].read_text(encoding="utf-8"))
            response = root / "polish-response.json"
            response.write_text(
                json.dumps(
                    {
                        "fingerprint": manifest["fingerprint"],
                        "items": [{"id": 1, "text": "润色一"}, {"id": 2, "text": "润色二"}],
                    }
                ),
                encoding="utf-8",
            )

            job = load_job(job_file)
            self.assertEqual(
                plan_advance_steps(job, require_workflow_config(job)),
                ("polish",),
            )

            with self.assertRaisesRegex(ValueError, "manifest no longer matches"):
                apply_agent_translation_polish(job_file, response)

    def test_new_agent_job_translates_and_polishes_in_one_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello.\n\n"
                "2\n00:00:01,000 --> 00:00:02,000\nWorld.\n",
                encoding="utf-8",
            )
            job = run_subtitle_transcription(source, root, language="en")
            job_file = get_job_file(job)

            manifest_path = prepare_agent_translation(
                job_file,
                source_language="en",
                target_language="zh-CN",
                translation_quality="polished",
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["task"], "translate_and_polish")
            self.assertEqual(manifest["task_version"], 1)
            self.assertEqual(manifest["prompt_version"], 5)
            response = root / "response.json"
            response.write_text(
                json.dumps(
                    {
                        "fingerprint": manifest["fingerprint"],
                        "items": [{"id": 1, "text": "你好。"}, {"id": 2, "text": "世界。"}],
                    }
                ),
                encoding="utf-8",
            )

            paths = apply_agent_translation(
                job_file,
                response,
                target_language="zh-CN",
            )

            job = load_job(job_file)
            kinds = {artifact.kind for artifact in job.artifacts}
            self.assertNotIn("translation_draft_cues", kinds)
            self.assertNotIn("translation_polish_batches", kinds)
            self.assertTrue(
                {
                    "translation_polish_progress",
                    "translation_polished_cues",
                }
                <= kinds
            )
            self.assertNotIn("translated_cues", kinds)
            self.assertTrue(agent_translate_and_polish_core_satisfied(job))
            polished_path = next(
                Path(artifact.path)
                for artifact in job.artifacts
                if artifact.kind == "translation_polished_cues"
            )
            polished = json.loads(polished_path.read_text(encoding="utf-8"))
            self.assertEqual([item["target_text"] for item in polished], ["你好", "世界"])
            self.assertEqual(
                [(item["id"], item["start_ms"], item["end_ms"]) for item in polished],
                [(1, 0, 1000), (2, 1000, 2000)],
            )
            self.assertEqual(polished[0]["source_text"], "Hello.")
            self.assertEqual(polished[0]["source_language"], "en")
            self.assertEqual(polished[0]["target_language"], "zh-CN")
            self.assertEqual({path.suffix for path in paths}, {".json"})

    def test_agent_polished_progress_recovers_missing_cues_by_reapplying_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello.\n",
                encoding="utf-8",
            )
            job = run_subtitle_transcription(source, root, language="en")
            job_file = get_job_file(job)
            manifest_path = prepare_agent_translation(
                job_file,
                source_language="en",
                target_language="zh-CN",
                translation_quality="polished",
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
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
            job = load_job(job_file)
            job.artifacts = [
                artifact
                for artifact in job.artifacts
                if artifact.kind
                not in {
                    "translation_polished_cues",
                    "translated_cues",
                    "translated_subtitle_qa",
                    "translated_srt",
                    "bilingual_srt",
                    "bilingual_ass",
                }
            ]
            save_job(job)

            apply_agent_translation(job_file, response, target_language="zh-CN")

            self.assertTrue(agent_translate_and_polish_core_satisfied(load_job(job_file)))

    def test_new_agent_job_rejects_stale_fingerprint_without_writing_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello.\n",
                encoding="utf-8",
            )
            job = run_subtitle_transcription(source, root, language="en")
            job_file = get_job_file(job)
            prepare_agent_translation(
                job_file,
                source_language="en",
                target_language="zh-CN",
                translation_quality="polished",
            )
            response = root / "response.json"
            response.write_text(
                json.dumps(
                    {
                        "fingerprint": "stale",
                        "items": [{"id": 1, "text": "你好。"}],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "fingerprint"):
                apply_agent_translation(job_file, response, target_language="zh-CN")

            self.assertNotIn(
                "translation_polish_progress",
                {artifact.kind for artifact in load_job(job_file).artifacts},
            )

    def test_new_agent_job_rejects_unknown_task_contract_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello.\n",
                encoding="utf-8",
            )

            for field, value, message in (
                ("task", "unknown", "unsupported translation task"),
                ("task_version", 99, "manifest is invalid or stale"),
                ("prompt_version", 3, "manifest is invalid or stale"),
            ):
                with self.subTest(field=field):
                    output = root / field
                    job = run_subtitle_transcription(source, output, language="en")
                    job_file = get_job_file(job)
                    manifest_path = prepare_agent_translation(
                        job_file,
                        source_language="en",
                        target_language="zh-CN",
                        translation_quality="polished",
                    )
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest[field] = value
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    job = load_job(job_file)
                    artifact = next(
                        item for item in job.artifacts if item.kind == "translation_batches"
                    )
                    artifact.checksum = file_checksum(manifest_path)
                    save_job(job)
                    response = root / f"{field}-response.json"
                    response.write_text(
                        json.dumps(
                            {
                                "fingerprint": manifest["fingerprint"],
                                "items": [{"id": 1, "text": "你好。"}],
                            }
                        ),
                        encoding="utf-8",
                    )
                    before = job_file.read_bytes()

                    with self.assertRaisesRegex(ValueError, message):
                        apply_agent_translation(
                            job_file,
                            response,
                            target_language="zh-CN",
                        )

                    self.assertEqual(job_file.read_bytes(), before)

    def test_new_agent_job_rejects_incomplete_or_duplicate_ids_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello.\n\n"
                "2\n00:00:01,000 --> 00:00:02,000\nWorld.\n",
                encoding="utf-8",
            )
            job = run_subtitle_transcription(source, root, language="en")
            job_file = get_job_file(job)
            manifest_path = prepare_agent_translation(
                job_file,
                source_language="en",
                target_language="zh-CN",
                translation_quality="polished",
            )
            fingerprint = json.loads(manifest_path.read_text(encoding="utf-8"))["fingerprint"]

            for name, items in (
                ("missing", [{"id": 1, "text": "你好。"}]),
                (
                    "duplicate",
                    [
                        {"id": 1, "text": "你好。"},
                        {"id": 1, "text": "您好。"},
                        {"id": 2, "text": "世界。"},
                    ],
                ),
                (
                    "extra",
                    [
                        {"id": 1, "text": "你好。"},
                        {"id": 2, "text": "世界。"},
                        {"id": 99, "text": "多余。"},
                    ],
                ),
            ):
                with self.subTest(name=name):
                    response = root / f"{name}-response.json"
                    response.write_text(
                        json.dumps({"fingerprint": fingerprint, "items": items}),
                        encoding="utf-8",
                    )
                    before = job_file.read_bytes()

                    with self.assertRaises(ValueError):
                        apply_agent_translation(
                            job_file,
                            response,
                            target_language="zh-CN",
                        )

                    self.assertEqual(job_file.read_bytes(), before)

    def test_atomic_api_polished_rerun_reuses_draft_and_final_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello.\n",
                encoding="utf-8",
            )
            job = run_subtitle_transcription(source, root, language="en")
            job_file = get_job_file(job)
            provider_args = {
                "target_language": "zh-CN",
                "provider": "openai-compatible",
                "base_url": "https://example.com/v1",
                "model": "model",
                "api_key_env": "TEST_POLISH_API_KEY",
                "batch_size": 40,
                "max_retries": 0,
                "timeout_seconds": 10,
            }
            events = []
            with (
                patch.dict("os.environ", {"TEST_POLISH_API_KEY": "secret"}),
                patch(
                    "xvt.pipeline.translation.translate_batch",
                    return_value={1: "草稿"},
                ) as translate_provider,
                patch(
                    "xvt.pipeline.polish.polish_batch",
                    return_value={1: "润色"},
                ) as polish_provider,
            ):
                draft = run_translation(
                    job_file,
                    **provider_args,
                    translation_quality="polished",
                    event_sink=events.append,
                )
                polished = run_atomic_translation_polish(
                    job_file,
                    **provider_args,
                    event_sink=events.append,
                )
                reused_draft = run_translation(
                    job_file,
                    **provider_args,
                    translation_quality="polished",
                )
                reused_polish = run_atomic_translation_polish(job_file, **provider_args)

            self.assertEqual(draft.mode, "openai-compatible")
            self.assertEqual(polished.mode, "openai-compatible")
            self.assertEqual(reused_draft.mode, "draft_reused")
            self.assertEqual(reused_polish.mode, "reused")
            translate_provider.assert_called_once()
            polish_provider.assert_called_once()
            batch_phases = {
                event.data.get("phase")
                for event in events
                if event.type == "translation_batch_started"
            }
            self.assertEqual(batch_phases, {"draft", "polish"})

    def test_api_polish_failure_marks_stage_with_polish_phase(self) -> None:
        """润色批次失败时的 stage 收口必须带 ``phase=polish``。

        这是润色相对初译 / 局部重译唯一的失败收口差异（共享批次循环的
        ``stage_extras`` 参数就只为它存在），丢了它人类进度与事件消费方会把润色失败
        误读成初译失败。
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root)
            events = []

            with (
                patch.dict("os.environ", {"TEST_POLISH_API_KEY": "secret"}),
                patch(
                    "xvt.pipeline.polish.polish_batch",
                    side_effect=RuntimeError("polish crashed"),
                ),
                self.assertRaisesRegex(RuntimeError, "polish crashed"),
            ):
                run_translation_polish(
                    job_file,
                    provider="openai-compatible",
                    base_url="https://example.com/v1",
                    model="model",
                    api_key_env="TEST_POLISH_API_KEY",
                    max_retries=0,
                    event_sink=events.append,
                )

            failures = [event for event in events if event.type == "stage_failed"]
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0].data["stage"], "translate")
            self.assertEqual(failures[0].data["phase"], "polish")
            self.assertEqual(failures[0].data["error"]["category"], "workflow")
            self.assertEqual(failures[0].data["error"]["message"], "polish crashed")
            stage = load_job(job_file).stages[StageName.TRANSLATE]
            self.assertIs(stage.status, StageStatus.FAILED)

    def test_api_polish_resumes_from_valid_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root)
            calls = []

            def fail_second(batch, _config, *, correction=None):
                calls.append(batch.id)
                if batch.id == 2:
                    raise TranslationProviderError("offline", retryable=True)
                return {item.id: f"润色{item.id}，" for item in batch.items}

            with (
                patch.dict("os.environ", {"TEST_POLISH_API_KEY": "secret"}),
                patch("xvt.pipeline.polish.polish_batch", side_effect=fail_second),
                self.assertRaises(TranslationProviderError),
            ):
                run_translation_polish(
                    job_file,
                    provider="openai-compatible",
                    base_url="https://example.com/v1",
                    model="model",
                    api_key_env="TEST_POLISH_API_KEY",
                    max_retries=0,
                )
            self.assertEqual(calls, [1, 2])

            with (
                patch.dict("os.environ", {"TEST_POLISH_API_KEY": "secret"}),
                patch(
                    "xvt.pipeline.polish.polish_batch",
                    side_effect=lambda batch, _config, **_kwargs: {
                        item.id: f"润色{item.id}。" for item in batch.items
                    },
                ) as provider,
            ):
                result = run_translation_polish(job_file)

            self.assertEqual(result.mode, "openai-compatible")
            self.assertEqual(provider.call_count, 1)
            self.assertEqual(provider.call_args.args[0].id, 2)
            job = load_job(job_file)
            polished_path = next(
                Path(artifact.path)
                for artifact in job.artifacts
                if artifact.kind == "translation_polished_cues"
            )
            polished = [Cue(**item) for item in json.loads(polished_path.read_text())]
            self.assertEqual([cue.target_text for cue in polished], ["润色1", "润色2"])

    def test_agent_polish_rejects_extra_id_without_replacing_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root, cue_count=1)
            run_translation_polish(job_file)
            response = root / "polish-response.json"
            self._write_polish_response(
                job_file,
                response,
                [{"id": 1, "text": "润色"}, {"id": 99, "text": "多余"}],
            )

            with self.assertRaisesRegex(ValueError, "cover every requested cue id"):
                apply_agent_translation_polish(job_file, response)

            job = load_job(job_file)
            draft_path = next(
                Path(artifact.path)
                for artifact in job.artifacts
                if artifact.kind == "translation_draft_cues"
            )
            draft = json.loads(draft_path.read_text())
            self.assertEqual(draft[0]["target_text"], "草稿1")
            self.assertNotIn(
                "translation_polished_cues", {artifact.kind for artifact in job.artifacts}
            )

    def test_agent_polish_rejects_response_for_an_old_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root, cue_count=1)
            run_translation_polish(job_file)
            response = root / "polish-response.json"
            self._write_polish_response(
                job_file,
                response,
                [{"id": 1, "text": "旧润色"}],
                fingerprint="stale",
            )

            with self.assertRaisesRegex(ValueError, "fingerprint does not match"):
                apply_agent_translation_polish(job_file, response)

    def test_cue_retranslation_replaces_draft_and_invalidates_old_polish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root, cue_count=1)
            run_translation_polish(job_file)
            polish_response = root / "polish-response.json"
            self._write_polish_response(
                job_file,
                polish_response,
                [{"id": 1, "text": "旧润色"}],
            )
            apply_agent_translation_polish(job_file, polish_response)
            self._complete_semantic_review(job_file, root)
            manifest_path = retranslate_cues(
                job_file,
                cue_ids=(1,),
                target_language="zh-CN",
                provider="agent",
            ).paths[0]
            fingerprint = json.loads(manifest_path.read_text(encoding="utf-8"))["fingerprint"]
            correction = root / "correction.json"
            correction.write_text(
                json.dumps(
                    {
                        "fingerprint": fingerprint,
                        "items": [{"id": 1, "text": "新修订"}],
                    }
                ),
                encoding="utf-8",
            )

            paths = apply_cue_retranslation(
                job_file,
                correction,
                cue_ids=(1,),
                target_language="zh-CN",
            )

            self.assertEqual(paths[0].name, "translation-draft-cues.json")
            job = load_job(job_file)
            kinds = {artifact.kind for artifact in job.artifacts}
            self.assertIn("translation_draft_cues", kinds)
            self.assertNotIn("translation_polished_cues", kinds)
            self.assertNotIn("translated_cues", kinds)
            draft = json.loads(paths[0].read_text())
            self.assertEqual(draft[0]["target_text"], "新修订")

    def test_invalid_override_does_not_create_draft_or_change_goal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root, cue_count=1)

            with self.assertRaisesRegex(ValueError, "batch_size"):
                run_translation_polish(job_file, batch_size=0)

            job = load_job(job_file)
            self.assertEqual(job.config["workflow"]["translation_quality"], "standard")
            self.assertNotIn(
                "translation_draft_cues", {artifact.kind for artifact in job.artifacts}
            )
            self.assertIn("translated_cues", {artifact.kind for artifact in job.artifacts})

    def test_polish_satisfaction_rejects_semantically_incomplete_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root, cue_count=1)
            run_translation_polish(job_file)
            response = root / "polish-response.json"
            self._write_polish_response(job_file, response, [{"id": 1, "text": "润色"}])
            apply_agent_translation_polish(job_file, response)
            job = load_job(job_file)
            self.assertTrue(translation_polish_satisfied(job))
            progress_artifact = next(
                artifact
                for artifact in job.artifacts
                if artifact.kind == "translation_polish_progress"
            )
            progress_path = Path(progress_artifact.path)
            progress = json.loads(progress_path.read_text())
            progress["completed_batches"] = []
            progress_path.write_text(json.dumps(progress), encoding="utf-8")
            progress_artifact.checksum = file_checksum(progress_path)
            save_job(job)

            self.assertFalse(translation_polish_satisfied(load_job(job_file)))

    def test_polish_satisfaction_rejects_non_object_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root, cue_count=1)
            run_translation_polish(job_file)
            response = root / "polish-response.json"
            self._write_polish_response(job_file, response, [{"id": 1, "text": "润色"}])
            apply_agent_translation_polish(job_file, response)
            job = load_job(job_file)
            batches_artifact = next(
                artifact
                for artifact in job.artifacts
                if artifact.kind == "translation_polish_batches"
            )
            batches_path = Path(batches_artifact.path)
            batches_path.write_text("[]\n", encoding="utf-8")
            batches_artifact.checksum = file_checksum(batches_path)
            save_job(job)

            self.assertFalse(translation_polish_satisfied(load_job(job_file)))

    def test_missing_draft_does_not_resnapshot_polished_translation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root, cue_count=1)
            run_translation_polish(job_file)
            response = root / "polish-response.json"
            self._write_polish_response(job_file, response, [{"id": 1, "text": "润色"}])
            apply_agent_translation_polish(job_file, response)
            job = load_job(job_file)
            draft = next(
                artifact for artifact in job.artifacts if artifact.kind == "translation_draft_cues"
            )
            Path(draft.path).unlink()

            with self.assertRaisesRegex(ValueError, "no valid translation draft checkpoint"):
                run_translation_polish(job_file)

            job = load_job(job_file)
            self.assertNotIn(
                Path(draft.path),
                [
                    Path(artifact.path)
                    for artifact in job.artifacts
                    if artifact.kind == "translation_draft_cues" and Path(artifact.path).is_file()
                ],
            )

    def test_agent_one_shot_polished_job_rejects_polish_rerun_with_reason(self) -> None:
        """Agent `translate_and_polish` Job 结构上就没有 draft，不是 checkpoint 失效。

        这类 Job 的润色在初译那一步就做完了，永远不会写
        `translation-draft-cues.json`，所以「补跑 Q5a 润色」这条路对它根本不适用。
        报错必须说清这一点，而不是复用「checkpoint 失效、需要恢复草稿」的文案。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello.\n",
                encoding="utf-8",
            )
            job = run_subtitle_transcription(source, root, language="en")
            job_file = get_job_file(job)
            set_workflow_config(
                job,
                WorkflowConfig(
                    target_language="zh-CN",
                    provider="agent",
                    batch_size=1,
                    translation_quality="polished",
                ),
            )
            save_job(job)
            manifest_path = prepare_agent_translation(
                job_file,
                source_language="en",
                target_language="zh-CN",
                translation_quality="polished",
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
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
            self.assertTrue(agent_translate_and_polish_core_satisfied(load_job(job_file)))

            with self.assertRaises(ValueError) as caught:
                run_translation_polish(job_file)

            message = str(caught.exception)
            self.assertIn("translate_and_polish", message)
            self.assertIn("translation-draft-cues.json", message)
            self.assertNotIn("no valid translation draft checkpoint", message)
            # Job 不被这次拒绝改写：仍是一个译文目标已满足的 polished Job。
            self.assertTrue(agent_translate_and_polish_core_satisfied(load_job(job_file)))

    def test_provider_override_reruns_completed_polish_from_original_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root, cue_count=1)
            run_translation_polish(job_file)
            response = root / "polish-response.json"
            self._write_polish_response(job_file, response, [{"id": 1, "text": "旧润色"}])
            apply_agent_translation_polish(job_file, response)

            with (
                patch.dict("os.environ", {"TEST_POLISH_API_KEY": "secret"}),
                patch(
                    "xvt.pipeline.polish.polish_batch",
                    side_effect=lambda batch, _config, **_kwargs: {
                        item.id: "新润色" for item in batch.items
                    },
                ) as provider,
            ):
                run_translation_polish(
                    job_file,
                    provider="openai-compatible",
                    base_url="https://example.com/v1",
                    model="new-model",
                    api_key_env="TEST_POLISH_API_KEY",
                )

            provider.assert_called_once()
            job = load_job(job_file)
            polished_path = next(
                Path(artifact.path)
                for artifact in job.artifacts
                if artifact.kind == "translation_polished_cues"
            )
            polished = json.loads(polished_path.read_text())
            self.assertEqual(polished[0]["target_text"], "新润色")
            draft_path = next(
                Path(artifact.path)
                for artifact in job.artifacts
                if artifact.kind == "translation_draft_cues"
            )
            draft = json.loads(draft_path.read_text())
            self.assertEqual(draft[0]["target_text"], "草稿1")

    def test_source_language_change_resets_api_polish_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root, cue_count=1)
            job = load_job(job_file)
            source_artifact = next(
                artifact for artifact in job.artifacts if artifact.kind == "source_cues"
            )
            source_path = Path(source_artifact.path)
            source = json.loads(source_path.read_text())
            source[0]["source_language"] = None
            source_path.write_text(json.dumps(source), encoding="utf-8")
            source_artifact.checksum = file_checksum(source_path)
            job.config["source_language"] = "en"
            save_job(job)

            with (
                patch.dict("os.environ", {"TEST_POLISH_API_KEY": "secret"}),
                patch(
                    "xvt.pipeline.polish.polish_batch",
                    side_effect=lambda batch, _config, **_kwargs: {
                        item.id: "英文润色" for item in batch.items
                    },
                ),
            ):
                run_translation_polish(
                    job_file,
                    provider="openai-compatible",
                    base_url="https://example.com/v1",
                    model="model",
                    api_key_env="TEST_POLISH_API_KEY",
                )

            job = load_job(job_file)
            job.config["source_language"] = "fr"
            save_job(job)
            with (
                patch.dict("os.environ", {"TEST_POLISH_API_KEY": "secret"}),
                patch(
                    "xvt.pipeline.polish.polish_batch",
                    side_effect=lambda batch, _config, **_kwargs: {
                        item.id: "法文润色" for item in batch.items
                    },
                ) as provider,
            ):
                run_translation_polish(job_file)

            provider.assert_called_once()

    def test_completed_polish_is_reused_without_mutating_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root, cue_count=1)
            run_translation_polish(job_file)
            response = root / "polish-response.json"
            self._write_polish_response(job_file, response, [{"id": 1, "text": "润色"}])
            apply_agent_translation_polish(job_file, response)
            before = job_file.read_bytes()

            result = run_translation_polish(job_file)

            self.assertEqual(result.mode, "reused")
            self.assertEqual(job_file.read_bytes(), before)

    def test_import_rejects_target_language_change_before_replacing_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root, cue_count=1)
            run_translation_polish(job_file)
            translated = root / "ja.srt"
            translated.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n翻訳\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "polished workflow goal"):
                apply_subtitle_translation(
                    job_file,
                    translated,
                    target_language="ja",
                )

            job = load_job(job_file)
            draft_path = next(
                Path(artifact.path)
                for artifact in job.artifacts
                if artifact.kind == "translation_draft_cues"
            )
            draft = json.loads(draft_path.read_text())
            self.assertEqual(draft[0]["target_text"], "草稿1")
            self.assertNotIn(
                "input_translation_subtitles", {artifact.kind for artifact in job.artifacts}
            )
            self.assertEqual(job.stages[StageName.TRANSLATE].status, StageStatus.AWAITING_INPUT)

    def test_agent_translation_rejects_polished_target_language_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root, cue_count=1)
            run_translation_polish(job_file)
            response = root / "translation-response.json"
            response.write_text(
                json.dumps({"items": [{"id": 1, "text": "翻訳"}]}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "polished workflow goal"):
                apply_agent_translation(job_file, response, target_language="ja")

            job = load_job(job_file)
            self.assertEqual(job.stages[StageName.TRANSLATE].status, StageStatus.AWAITING_INPUT)

    def test_standard_atomic_entrypoints_reject_target_language_change_before_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello.\n",
                encoding="utf-8",
            )

            for entrypoint in ("prepare", "translate", "apply"):
                with self.subTest(entrypoint=entrypoint):
                    output = root / entrypoint
                    job = run_subtitle_transcription(source, output, language="en")
                    job_file = get_job_file(job)
                    prepare_agent_translation(
                        job_file,
                        source_language="en",
                        target_language="zh-CN",
                    )
                    before = job_file.read_bytes()

                    with self.assertRaisesRegex(
                        ValueError,
                        "target language must match the persisted atomic job goal",
                    ):
                        if entrypoint == "prepare":
                            prepare_agent_translation(
                                job_file,
                                source_language="en",
                                target_language="ja",
                            )
                        elif entrypoint == "translate":
                            run_translation(
                                job_file,
                                target_language="ja",
                                provider="agent",
                            )
                        else:
                            response = root / "standard-response.json"
                            response.write_text(
                                json.dumps({"items": [{"id": 1, "text": "翻訳"}]}),
                                encoding="utf-8",
                            )
                            apply_agent_translation(
                                job_file,
                                response,
                                target_language="ja",
                            )

                    self.assertEqual(job_file.read_bytes(), before)

    def test_standard_workflow_import_rejects_target_language_change_before_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root, cue_count=1)
            translated = root / "ja.srt"
            translated.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n翻訳\n",
                encoding="utf-8",
            )
            before = job_file.read_bytes()

            with self.assertRaisesRegex(
                ValueError,
                "target language must match the persisted workflow goal",
            ):
                apply_subtitle_translation(
                    job_file,
                    translated,
                    target_language="ja",
                )

            self.assertEqual(job_file.read_bytes(), before)

    def test_translate_rejects_polished_target_language_change_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._translated_job(root, cue_count=1)
            run_translation_polish(job_file)
            response = root / "polish-response.json"
            self._write_polish_response(job_file, response, [{"id": 1, "text": "润色"}])
            apply_agent_translation_polish(job_file, response)
            before = load_job(job_file)
            before_kinds = {artifact.kind for artifact in before.artifacts}

            with self.assertRaisesRegex(ValueError, "polished workflow goal"):
                run_translation(job_file, target_language="ja", provider="agent")

            after = load_job(job_file)
            self.assertEqual(after.stages[StageName.TRANSLATE].status, StageStatus.SUCCEEDED)
            self.assertEqual({artifact.kind for artifact in after.artifacts}, before_kinds)


if __name__ == "__main__":
    unittest.main()

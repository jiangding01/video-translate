"""局部重译（pipeline.retranslate）的流程测试。"""

import json
import os
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from xvt.errors import ExitCode, UserInputError
from xvt.job_store import create_job, load_job, save_job
from xvt.models import Artifact, Cue, StageName, StageStatus
from xvt.pipeline import (
    apply_cue_retranslation,
    cue_retranslation_request,
    cue_retranslation_waiting,
    prepare_agent_translation,
    retranslate_cues,
)
from xvt.recovery import file_checksum, valid_artifact_path
from xvt.translation_provider import ProviderResponseError, TranslationProviderError


class CueRetranslationTest(unittest.TestCase):
    def test_cue_retranslation_updates_only_requested_cues_and_invalidates_burn_outputs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            source_path = artifacts / "source-cues.json"
            source_path.write_text(
                json.dumps(
                    [
                        asdict(Cue(1, 0, 1000, "First", source_language="en")),
                        asdict(Cue(2, 1000, 2000, "Second", source_language="en")),
                    ]
                ),
                encoding="utf-8",
            )
            translated_path = artifacts / "translated-cues.json"
            translated_path.write_text(
                json.dumps(
                    [
                        asdict(Cue(1, 0, 1000, "First", "旧译文一", target_language="zh-CN")),
                        asdict(Cue(2, 1000, 2000, "Second", "旧译文二", target_language="zh-CN")),
                    ]
                ),
                encoding="utf-8",
            )
            media_path = artifacts / "media.json"
            media_path.write_text(json.dumps({"duration_ms": 2000}), encoding="utf-8")
            stale_ass = artifacts / "bilingual.ass"
            stale_ass.write_text("stale", encoding="utf-8")
            stale_preview = artifacts / "preview.mp4"
            stale_preview.write_bytes(b"stale preview")
            job.artifacts.extend(
                [
                    Artifact("source_cues", str(source_path)),
                    Artifact("translated_cues", str(translated_path)),
                    Artifact("media_info", str(media_path)),
                    Artifact("bilingual_ass", str(stale_ass)),
                    Artifact("preview_video", str(stale_preview)),
                ]
            )
            job_file = save_job(job)
            manifest_path = retranslate_cues(
                job_file,
                cue_ids=(1,),
                target_language="zh-CN",
                provider="agent",
            ).paths[0]
            fingerprint = json.loads(manifest_path.read_text(encoding="utf-8"))["fingerprint"]
            response_path = root / "response.json"
            response_path.write_text(
                json.dumps(
                    {
                        "fingerprint": fingerprint,
                        "items": [{"id": 1, "text": "新译文一，"}],
                    }
                ),
                encoding="utf-8",
            )

            apply_cue_retranslation(
                job_file,
                response_path,
                cue_ids=(1,),
                target_language="zh-CN",
            )

            persisted = json.loads(job_file.read_text(encoding="utf-8"))
            repaired = json.loads(translated_path.read_text(encoding="utf-8"))

        self.assertEqual([cue["target_text"] for cue in repaired], ["新译文一", "旧译文二"])
        self.assertEqual(persisted["stages"]["subtitle_qa"]["status"], "succeeded")
        self.assertNotIn("bilingual_ass", {item["kind"] for item in persisted["artifacts"]})
        self.assertNotIn("preview_video", {item["kind"] for item in persisted["artifacts"]})

    def test_missing_response_file_is_a_user_input_error(self) -> None:
        """等待态正确、只是 response 路径打错：属于用户输入错误（2），不是依赖故障（3）。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            source_path = artifacts / "source-cues.json"
            source_path.write_text(
                json.dumps([asdict(Cue(1, 0, 1000, "First", source_language="en"))]),
                encoding="utf-8",
            )
            translated_path = artifacts / "translated-cues.json"
            translated_path.write_text(
                json.dumps([asdict(Cue(1, 0, 1000, "First", "旧译文", target_language="zh-CN"))]),
                encoding="utf-8",
            )
            media_path = artifacts / "media.json"
            media_path.write_text(json.dumps({"duration_ms": 1000}), encoding="utf-8")
            job.artifacts.extend(
                [
                    Artifact("source_cues", str(source_path)),
                    Artifact("translated_cues", str(translated_path)),
                    Artifact("media_info", str(media_path)),
                ]
            )
            job_file = save_job(job)
            retranslate_cues(job_file, cue_ids=(1,), target_language="zh-CN", provider="agent")

            with self.assertRaisesRegex(
                UserInputError, "cannot read cue retranslation response"
            ) as caught:
                apply_cue_retranslation(
                    job_file,
                    root / "gone" / "response.json",
                    cue_ids=(1,),
                    target_language="zh-CN",
                )

            self.assertEqual(caught.exception.exit_code, ExitCode.USER_ERROR)

    def test_cue_retranslation_rejects_a_different_target_language(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            source_path = artifacts / "source-cues.json"
            source_path.write_text(
                json.dumps([asdict(Cue(1, 0, 1000, "First", source_language="en"))]),
                encoding="utf-8",
            )
            translated_path = artifacts / "translated-cues.json"
            translated_path.write_text(
                json.dumps([asdict(Cue(1, 0, 1000, "First", "一", target_language="zh-CN"))]),
                encoding="utf-8",
            )
            media_path = artifacts / "media.json"
            media_path.write_text(json.dumps({"duration_ms": 1000}), encoding="utf-8")
            job.artifacts.extend(
                [
                    Artifact("source_cues", str(source_path)),
                    Artifact("translated_cues", str(translated_path)),
                    Artifact("media_info", str(media_path)),
                ]
            )
            job_file = save_job(job)
            response_path = root / "response.json"
            response_path.write_text(
                json.dumps({"items": [{"id": 1, "text": "one"}]}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "target language must match"):
                apply_cue_retranslation(
                    job_file,
                    response_path,
                    cue_ids=(1,),
                    target_language="en",
                )

    def test_cue_retranslation_agent_writes_selected_cue_with_original_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            source_path = artifacts / "source-cues.json"
            source_path.write_text(
                json.dumps(
                    [
                        asdict(Cue(1, 0, 1000, "First", source_language="en")),
                        asdict(Cue(2, 1000, 2000, "Second", source_language="en")),
                        asdict(Cue(3, 2000, 3000, "Third", source_language="en")),
                    ]
                ),
                encoding="utf-8",
            )
            translated_path = artifacts / "translated-cues.json"
            translated_path.write_text(
                json.dumps(
                    [
                        asdict(Cue(1, 0, 1000, "First", "一", target_language="zh-CN")),
                        asdict(Cue(2, 1000, 2000, "Second", "二", target_language="zh-CN")),
                        asdict(Cue(3, 2000, 3000, "Third", "三", target_language="zh-CN")),
                    ]
                ),
                encoding="utf-8",
            )
            job.artifacts.extend(
                [
                    Artifact("source_cues", str(source_path)),
                    Artifact("translated_cues", str(translated_path)),
                ]
            )
            job_file = save_job(job)

            result = retranslate_cues(
                job_file,
                cue_ids=(2,),
                target_language="zh-CN",
                provider="agent",
            )

            batch_data = json.loads(result.paths[0].read_text(encoding="utf-8"))
            persisted = json.loads(job_file.read_text(encoding="utf-8"))

            stale_response = root / "stale-response.json"
            stale_response.write_text(
                json.dumps(
                    {
                        "fingerprint": batch_data["fingerprint"],
                        "items": [{"id": 2, "text": "旧上下文生成的译文"}],
                    }
                ),
                encoding="utf-8",
            )
            current = json.loads(translated_path.read_text(encoding="utf-8"))
            current[1]["target_text"] = "等待期间人工改过"
            translated_path.write_text(json.dumps(current), encoding="utf-8")

            # 文案要涵盖「输入没变、但策略/版本解析变了」这条真实路径，并给出处置。
            with self.assertRaisesRegex(
                ValueError,
                "manifest does not match what this xvt version would generate",
            ):
                apply_cue_retranslation(
                    job_file,
                    stale_response,
                    cue_ids=(2,),
                    target_language="zh-CN",
                )

            with patch("xvt.pipeline.retranslate.TRANSLATION_PROMPT_VERSION", 4):
                old_manifest_path = retranslate_cues(
                    job_file,
                    cue_ids=(2,),
                    target_language="zh-CN",
                    provider="agent",
                ).paths[0]
            old_manifest = json.loads(old_manifest_path.read_text(encoding="utf-8"))
            old_prompt_response = root / "old-prompt-response.json"
            old_prompt_response.write_text(
                json.dumps(
                    {
                        "fingerprint": old_manifest["fingerprint"],
                        "items": [{"id": 2, "text": "旧提示词生成的译文"}],
                    }
                ),
                encoding="utf-8",
            )
            # 文案要涵盖「输入没变、但策略/版本解析变了」这条真实路径，并给出处置。
            with self.assertRaises(ValueError) as captured:
                apply_cue_retranslation(
                    job_file,
                    old_prompt_response,
                    cue_ids=(2,),
                    target_language="zh-CN",
                )
            message = str(captured.exception)
            self.assertIn("does not match what this xvt version would generate", message)
            self.assertIn("rerun xvt retranslate-cues", message)

        batch = batch_data["batches"][0]
        self.assertEqual(result.mode, "agent")
        self.assertEqual(batch_data["prompt_version"], 5)
        self.assertEqual(batch_data["task"], "retranslate_cues")
        self.assertIsInstance(batch_data["fingerprint"], str)
        self.assertEqual([item["id"] for item in batch["items"]], [2])
        self.assertEqual([item["id"] for item in batch["context_before"]], [1])
        self.assertEqual([item["id"] for item in batch["context_after"]], [3])
        self.assertEqual(persisted["stages"]["translate"]["status"], "awaiting_input")

    def test_cue_retranslation_uses_independent_retry_budgets_and_events(self) -> None:
        provider_env = {
            "XVT_TRANSLATION_BASE_URL": "https://provider.example.com/v1",
            "XVT_TRANSLATION_MODEL": "test-model",
            "XVT_TRANSLATION_API_KEY": "secret",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root, {"source_language": "en"})
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            source_path = artifacts / "source-cues.json"
            source_path.write_text(
                json.dumps([asdict(Cue(1, 0, 1000, "Hello", source_language="en"))]),
                encoding="utf-8",
            )
            translated_path = artifacts / "translated-cues.json"
            translated_path.write_text(
                json.dumps(
                    [
                        asdict(
                            Cue(
                                1,
                                0,
                                1000,
                                "Hello",
                                "旧译文",
                                source_language="en",
                                target_language="zh-CN",
                            )
                        )
                    ]
                ),
                encoding="utf-8",
            )
            media_path = artifacts / "media.json"
            media_path.write_text(json.dumps({"duration_ms": 1000}), encoding="utf-8")
            job.artifacts.extend(
                [
                    Artifact("source_cues", str(source_path)),
                    Artifact("translated_cues", str(translated_path)),
                    Artifact("media_info", str(media_path)),
                ]
            )
            job_file = save_job(job)
            corrections = []

            def flaky_provider(batch, config, *, correction=None, **_kwargs):
                corrections.append(correction)
                if len(corrections) == 1:
                    raise TranslationProviderError("disconnected", retryable=True)
                if len(corrections) == 2:
                    raise ProviderResponseError("invalid response")
                return {1: "新译文"}

            events = []
            with (
                patch.dict(os.environ, provider_env, clear=False),
                patch("xvt.pipeline.translation.translate_batch", side_effect=flaky_provider),
                patch("xvt.pipeline.translation.time.sleep") as sleep,
                patch("xvt.pipeline.translation._backoff_delay", lambda base: float(base)),
            ):
                retranslate_cues(
                    job_file,
                    cue_ids=(1,),
                    target_language="zh-CN",
                    provider="openai-compatible",
                    max_retries=1,
                    event_sink=events.append,
                )

            repaired = json.loads(translated_path.read_text(encoding="utf-8"))

        self.assertEqual(corrections[0:2], [None, None])
        self.assertIn("previous response was invalid", corrections[2] or "")
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 0.5])
        self.assertEqual(repaired[0]["target_text"], "新译文")
        batch_events = [
            event
            for event in events
            if event.type
            in {
                "translation_batch_started",
                "translation_batch_retrying",
                "translation_batch_completed",
            }
        ]
        self.assertEqual(
            [event.type for event in batch_events],
            [
                "translation_batch_retrying",
                "translation_batch_retrying",
            ],
        )
        self.assertTrue(all(event.data["batch_total"] == 1 for event in batch_events))


class CueRetranslationInternalResponseReadTest(unittest.TestCase):
    """API 模式读的是自己刚写下并登记的产物，不能借用户输入 helper 的错误归类。"""

    def test_api_mode_does_not_route_its_own_response_through_the_user_input_helper(
        self,
    ) -> None:
        provider_env = {
            "XVT_TRANSLATION_BASE_URL": "https://provider.test/v1",
            "XVT_TRANSLATION_MODEL": "test-model",
            "XVT_TRANSLATION_API_KEY": "secret",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root, {"source_language": "en"})
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            source_path = artifacts / "source-cues.json"
            source_path.write_text(
                json.dumps([asdict(Cue(1, 0, 1000, "Hello", source_language="en"))]),
                encoding="utf-8",
            )
            translated_path = artifacts / "translated-cues.json"
            translated_path.write_text(
                json.dumps([asdict(Cue(1, 0, 1000, "Hello", "旧译文", target_language="zh-CN"))]),
                encoding="utf-8",
            )
            media_path = artifacts / "media.json"
            media_path.write_text(json.dumps({"duration_ms": 1000}), encoding="utf-8")
            job.artifacts.extend(
                [
                    Artifact("source_cues", str(source_path)),
                    Artifact("translated_cues", str(translated_path)),
                    Artifact("media_info", str(media_path)),
                ]
            )
            job_file = save_job(job)

            def forbidden(*_args, **_kwargs):
                raise AssertionError("internal artifact must not go through the user-input helper")

            with (
                patch.dict(os.environ, provider_env, clear=False),
                patch(
                    "xvt.pipeline.translation.translate_batch",
                    side_effect=lambda *_a, **_k: {1: "新译文"},
                ),
                patch("xvt.pipeline.retranslate.load_user_response_json", forbidden),
            ):
                retranslate_cues(
                    job_file,
                    cue_ids=(1,),
                    target_language="zh-CN",
                    provider="openai-compatible",
                )

            repaired = json.loads(translated_path.read_text(encoding="utf-8"))

        self.assertEqual(repaired[0]["target_text"], "新译文")


class CueRetranslationPolicyResolutionTest(unittest.TestCase):
    """守卫：重译 manifest 的策略解析与译文 QA 的术语校验必须走同一口径。

    workflow 快照优先、顶层 ``translation_policy`` 是原子 Job 的回退位置。两处都锁住，
    否则任一侧单独改动会让端到端 Job 的重译提示词与 QA 判定读到不同的策略。
    """

    def _prepare_job(self, root: Path, config: dict) -> Path:
        job = create_job("/tmp/input.mp4", root)
        job.config.update(config)
        artifacts = root / "jobs" / job.id / "artifacts"
        artifacts.mkdir()
        source_path = artifacts / "source-cues.json"
        source_path.write_text(
            json.dumps(
                [
                    asdict(Cue(1, 0, 1000, "First widget", source_language="en")),
                    asdict(Cue(2, 1000, 2000, "Second widget", source_language="en")),
                ]
            ),
            encoding="utf-8",
        )
        translated_path = artifacts / "translated-cues.json"
        translated_path.write_text(
            json.dumps(
                [
                    asdict(Cue(1, 0, 1000, "First widget", "一", target_language="zh-CN")),
                    asdict(Cue(2, 1000, 2000, "Second widget", "二", target_language="zh-CN")),
                ]
            ),
            encoding="utf-8",
        )
        job.artifacts.extend(
            [
                Artifact("source_cues", str(source_path)),
                Artifact("translated_cues", str(translated_path)),
            ]
        )
        return save_job(job)

    def test_manifest_policy_follows_the_workflow_snapshot(self) -> None:
        policy = {"style": "concise", "terms": [{"source": "widget", "target": "小组件"}]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._prepare_job(
                root,
                {
                    "workflow": {
                        "target_language": "zh-CN",
                        "translation_policy": policy,
                    }
                },
            )
            manifest_path = retranslate_cues(
                job_file,
                cue_ids=(1,),
                target_language="zh-CN",
                provider="agent",
            ).paths[0]
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["policy"], policy)
        self.assertEqual(manifest["batches"][0]["policy"], policy)

    def test_manifest_policy_still_falls_back_to_the_top_level_config(self) -> None:
        policy = {"style": "natural", "terms": [{"source": "widget", "target": "部件"}]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._prepare_job(root, {"translation_policy": policy})
            manifest_path = retranslate_cues(
                job_file,
                cue_ids=(1,),
                target_language="zh-CN",
                provider="agent",
            ).paths[0]
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["policy"], policy)
        self.assertEqual(manifest["batches"][0]["policy"], policy)


class CueRetranslationWaitingPredicateTest(unittest.TestCase):
    """恢复计划与 planner 共用的两个只读谓词。"""

    def _waiting_job_file(self, root: Path, *, cue_ids: tuple[int, ...] = (2,)) -> Path:
        job = create_job("/tmp/input.mp4", root)
        artifacts = root / "jobs" / job.id / "artifacts"
        artifacts.mkdir()
        source_path = artifacts / "source-cues.json"
        source_path.write_text(
            json.dumps(
                [
                    asdict(Cue(cue_id, (cue_id - 1) * 1000, cue_id * 1000, f"S{cue_id}"))
                    for cue_id in (1, 2, 3)
                ]
            ),
            encoding="utf-8",
        )
        translated_path = artifacts / "translated-cues.json"
        translated_path.write_text(
            json.dumps(
                [
                    asdict(
                        Cue(
                            cue_id,
                            (cue_id - 1) * 1000,
                            cue_id * 1000,
                            f"S{cue_id}",
                            f"译{cue_id}",
                            target_language="zh-CN",
                        )
                    )
                    for cue_id in (1, 2, 3)
                ]
            ),
            encoding="utf-8",
        )
        media_path = artifacts / "media.json"
        media_path.write_text(json.dumps({"duration_ms": 3000}), encoding="utf-8")
        job.artifacts.extend(
            [
                Artifact("source_cues", str(source_path), file_checksum(source_path)),
                Artifact("translated_cues", str(translated_path), file_checksum(translated_path)),
                Artifact("media_info", str(media_path), file_checksum(media_path)),
            ]
        )
        job_file = save_job(job)
        retranslate_cues(job_file, cue_ids=cue_ids, target_language="zh-CN", provider="agent")
        return job_file

    def test_request_returns_only_the_targeted_cue_ids(self) -> None:
        """上下文 cue 在 context_before / context_after，不能混进回填命令的 --cue。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._waiting_job_file(root)
            job = load_job(job_file)
            manifest_path = next(
                Path(artifact.path)
                for artifact in job.artifacts
                if artifact.kind == "cue_retranslation_batches"
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            # 前提：manifest 里确实带着上下文 cue，否则这条断言测不到东西。
            self.assertEqual([item["id"] for item in manifest["batches"][0]["context_before"]], [1])
            self.assertEqual([item["id"] for item in manifest["batches"][0]["context_after"]], [3])
            self.assertEqual(cue_retranslation_request(job), ((2,), "zh-CN"))

    def test_waiting_predicate_requires_the_manifest_to_match_current_cues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._waiting_job_file(root)
            job = load_job(job_file)
            self.assertTrue(cue_retranslation_waiting(job))

            translated = next(
                artifact for artifact in job.artifacts if artifact.kind == "translated_cues"
            )
            path = Path(translated.path)
            cues = json.loads(path.read_text(encoding="utf-8"))
            cues[0]["target_text"] = "等待期间被改过"
            path.write_text(json.dumps(cues), encoding="utf-8")
            # 译文重新登记为有效产物：失配的只有 manifest 记录的译文 checksum。
            translated.checksum = file_checksum(path)
            save_job(job)
            job = load_job(job_file)

            self.assertIsNotNone(valid_artifact_path(job, "cue_retranslation_batches"))
            self.assertIsNotNone(valid_artifact_path(job, "translated_cues"))
            self.assertFalse(cue_retranslation_waiting(job))

    def test_waiting_predicate_requires_valid_translated_cues(self) -> None:
        """译文 cue 被失效移出登记（润色/语义等待的常态）时谓词必须为假。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._waiting_job_file(root)
            job = load_job(job_file)
            job.artifacts = [
                artifact for artifact in job.artifacts if artifact.kind != "translated_cues"
            ]
            save_job(job)
            job = load_job(job_file)

            self.assertIs(job.stages[StageName.TRANSLATE].status, StageStatus.AWAITING_INPUT)
            self.assertIsNotNone(valid_artifact_path(job, "cue_retranslation_batches"))
            self.assertFalse(cue_retranslation_waiting(job))
            self.assertIsNotNone(cue_retranslation_request(job))

    def test_request_returns_none_for_an_unparsable_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._waiting_job_file(root)
            job = load_job(job_file)
            manifest = next(
                artifact
                for artifact in job.artifacts
                if artifact.kind == "cue_retranslation_batches"
            )
            path = Path(manifest.path)
            path.write_text("not json", encoding="utf-8")
            manifest.checksum = file_checksum(path)
            save_job(job)
            job = load_job(job_file)

            self.assertIsNone(cue_retranslation_request(job))
            self.assertFalse(cue_retranslation_waiting(job))

    def test_request_preserves_the_manifest_batch_order(self) -> None:
        """`--cue 3 --cue 1` 的 manifest 就是 3、1 顺序；排序会让回填的全等比对必然失败。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._waiting_job_file(root, cue_ids=(3, 1))
            job = load_job(job_file)
            request = cue_retranslation_request(job)

            assert request is not None
            self.assertEqual(request, ((3, 1), "zh-CN"))

            manifest_path = valid_artifact_path(job, "cue_retranslation_batches")
            assert manifest_path is not None
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            response_path = root / "response.json"
            response_path.write_text(
                json.dumps(
                    {
                        "fingerprint": manifest["fingerprint"],
                        "items": [{"id": cue_id, "text": f"新{cue_id}"} for cue_id in request[0]],
                    }
                ),
                encoding="utf-8",
            )

            # 恢复计划给出的 --cue 顺序必须能被原样复制执行。
            apply_cue_retranslation(
                job_file,
                response_path,
                cue_ids=request[0],
                target_language=request[1],
            )

            self.assertIs(
                load_job(job_file).stages[StageName.TRANSLATE].status,
                StageStatus.SUCCEEDED,
            )

    def test_bad_user_retranslation_response_keeps_the_waiting_state(self) -> None:
        """显式 apply-cue-retranslation 喂坏响应不得改写 Job 状态：等待态保住，修好后可回填。

        此前坏响应会把 TRANSLATE 打成 failed，cue_retranslation_waiting 判否，
        恢复计划与统一 xvt apply 一起丢失重译等待。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._waiting_job_file(root)
            job = load_job(job_file)
            manifest_path = valid_artifact_path(job, "cue_retranslation_batches")
            assert manifest_path is not None
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            bad_json = root / "bad.json"
            bad_json.write_text("{not json", encoding="utf-8")
            stale = root / "stale.json"
            stale.write_text(
                json.dumps({"fingerprint": "nope", "items": [{"id": 2, "text": "新2"}]}),
                encoding="utf-8",
            )

            for response_path, expected in (
                (bad_json, UserInputError),
                (stale, ValueError),
            ):
                with self.subTest(response=response_path.name):
                    with self.assertRaises(expected):
                        apply_cue_retranslation(
                            job_file,
                            response_path,
                            cue_ids=(2,),
                            target_language="zh-CN",
                        )
                    job = load_job(job_file)
                    self.assertIs(
                        job.stages[StageName.TRANSLATE].status,
                        StageStatus.AWAITING_INPUT,
                    )
                    self.assertTrue(cue_retranslation_waiting(job))

            fixed = root / "fixed.json"
            fixed.write_text(
                json.dumps(
                    {
                        "fingerprint": manifest["fingerprint"],
                        "items": [{"id": 2, "text": "修好后的译文"}],
                    }
                ),
                encoding="utf-8",
            )
            apply_cue_retranslation(
                job_file,
                fixed,
                cue_ids=(2,),
                target_language="zh-CN",
            )

            self.assertIs(
                load_job(job_file).stages[StageName.TRANSLATE].status,
                StageStatus.SUCCEEDED,
            )

    def test_a_fresh_translation_round_supersedes_a_stale_retranslation_manifest(self) -> None:
        """放弃重译后重新跑整轮翻译：残留 manifest 不得再遮蔽合法的初译等待。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._waiting_job_file(root)
            job = load_job(job_file)
            self.assertTrue(cue_retranslation_waiting(job))

            prepare_agent_translation(
                job_file,
                source_language="en",
                target_language="zh-CN",
            )
            job = load_job(job_file)

            self.assertIsNone(valid_artifact_path(job, "cue_retranslation_batches"))
            self.assertNotIn(
                "cue_retranslation_batches",
                {artifact.kind for artifact in job.artifacts},
            )
            self.assertIsNotNone(valid_artifact_path(job, "translation_batches"))
            self.assertIs(job.stages[StageName.TRANSLATE].status, StageStatus.AWAITING_INPUT)
            self.assertFalse(cue_retranslation_waiting(job))

    _PROVIDER_ENV = {
        "XVT_TRANSLATION_BASE_URL": "https://provider.example.com/v1",
        "XVT_TRANSLATION_MODEL": "test-model",
        "XVT_TRANSLATION_API_KEY": "secret",
    }

    def _failed_api_retranslation(self, job_file: Path) -> None:
        """API 模式重译（``xvt retranslate-cues`` 直连提供方）失败：manifest 保留，落到 failed。

        FAILED 必须由这条路径制造。用户模式（显式 ``apply-cue-retranslation``）的校验
        失败已经不再改写 Job 状态，用它来铺 failed 只会得到 awaiting_input，谓词的
        FAILED 分支就成了永远走不到的死码。谓词接受 FAILED 现在只服务两类场景：
        API 模式失败（这里），以及旧版本留下的、已被打成 failed 的历史 Job。
        """

        with (
            patch.dict(os.environ, self._PROVIDER_ENV, clear=False),
            patch(
                "xvt.pipeline.translation.translate_batch",
                side_effect=ProviderResponseError("invalid response"),
            ),
        ):
            with self.assertRaises(ProviderResponseError):
                retranslate_cues(
                    job_file,
                    cue_ids=(2,),
                    target_language="zh-CN",
                    provider="openai-compatible",
                    max_retries=0,
                )

    def test_api_retranslation_validation_failure_still_lands_on_failed(self) -> None:
        """API 模式下回填校验失败仍收口成 failed：响应是内部产物，不是用户输入。

        与 ``test_bad_user_retranslation_response_keeps_the_waiting_state`` 成对：同一段
        校验代码，用户模式不改写状态，API 模式按内部故障收口。manifest 仍然有效，
        谓词继续把这次重译认作可回填的等待。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._waiting_job_file(root)

            with (
                patch.dict(os.environ, self._PROVIDER_ENV, clear=False),
                # 提供方交回请求范围外的 cue id：覆盖范围校验在 _apply_cue_retranslation
                # 里失败，此时 stage 已由 retranslate_cues 置 running。
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

            job = load_job(job_file)
            self.assertIs(job.stages[StageName.TRANSLATE].status, StageStatus.FAILED)
            self.assertIsNotNone(valid_artifact_path(job, "cue_retranslation_batches"))
            self.assertTrue(cue_retranslation_waiting(job))

    def test_waiting_survives_a_failed_api_retranslation(self) -> None:
        """API 模式重译失败不作废这次等待：manifest 仍然有效，改对参数再回填一次就能推进。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._waiting_job_file(root)
            self._failed_api_retranslation(job_file)

            job = load_job(job_file)
            self.assertIs(job.stages[StageName.TRANSLATE].status, StageStatus.FAILED)
            self.assertIsNotNone(valid_artifact_path(job, "cue_retranslation_batches"))
            self.assertTrue(cue_retranslation_waiting(job))
            self.assertEqual(cue_retranslation_request(job), ((2,), "zh-CN"))

    def test_a_newer_full_round_request_wins_over_a_failed_retranslation(self) -> None:
        """failed + 两份 manifest 同时在场时按登记先后定夺：整轮翻译取代局部重译。

        ``prepare_agent_translation`` 自己会把重译 manifest 移出登记，所以这里直接按
        ``add_artifact`` 的语义（去重后追加）补一条 ``translation_batches``，测的是谓词
        在两份 manifest 都还挂着时的防御。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._waiting_job_file(root)
            self._failed_api_retranslation(job_file)

            job = load_job(job_file)
            manifest_path = valid_artifact_path(job, "cue_retranslation_batches")
            assert manifest_path is not None
            batches_path = manifest_path.parent / "translation-batches.json"
            batches_path.write_text(json.dumps({"batches": []}), encoding="utf-8")
            job.artifacts.append(
                Artifact("translation_batches", str(batches_path), file_checksum(batches_path))
            )
            save_job(job)

            job = load_job(job_file)
            self.assertFalse(cue_retranslation_waiting(job))

            # 反向次序（整轮批次是上一轮的旧产物）不构成取代，重译等待仍然成立。
            batches = job.artifacts.pop()
            job.artifacts.insert(0, batches)
            save_job(job)
            self.assertTrue(cue_retranslation_waiting(load_job(job_file)))


if __name__ == "__main__":
    unittest.main()

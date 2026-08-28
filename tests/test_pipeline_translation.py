"""翻译主链（pipeline.translation）的流程测试。"""

import json
import os
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from xvt.errors import ExitCode, UserInputError
from xvt.job_store import create_job, get_job_file, load_job, save_job
from xvt.media import MediaInfo
from xvt.models import Artifact, Cue, Transcript, Word
from xvt.pipeline import (
    apply_agent_translation,
    apply_subtitle_translation,
    prepare_agent_translation,
    run_local_transcription,
    run_subtitle_transcription,
    run_translation,
)
from xvt.pipeline.translation import _backoff_delay, load_translation_response
from xvt.process import CommandResult
from xvt.recovery import file_checksum
from xvt.translate import GlossaryTerm, TranslationPolicy
from xvt.translation_provider import ProviderResponseError, TranslationProviderError
from xvt.workflow_config import WorkflowConfig, set_workflow_config


class TranslationPipelineTest(unittest.TestCase):
    def test_agent_translation_round_trip_exports_all_subtitle_formats(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
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

            glossary = root / "glossary.json"
            glossary.write_text(
                json.dumps({"terms": [{"source": "Hello", "target": "你好"}]}),
                encoding="utf-8",
            )
            batch_path = prepare_agent_translation(
                root / "jobs" / job.id / "job.json",
                source_language="en",
                target_language="zh-CN",
                glossary=glossary,
                translation_style="natural",
            )
            batch_data = json.loads(batch_path.read_text())
            item = batch_data["batches"][0]["items"][0]
            self.assertEqual(item["id"], 1)
            self.assertEqual(item["duration_ms"], 950)
            self.assertEqual(item["max_target_characters"], 8)
            self.assertEqual(batch_data["prompt_version"], 5)
            self.assertEqual(batch_data["semantic_grouping_version"], 1)
            self.assertEqual(batch_data["batches"][0]["semantic_groups"], [[1]])
            self.assertEqual(
                batch_data["policy"],
                {
                    "style": "natural",
                    "terms": [{"source": "Hello", "target": "你好"}],
                },
            )

            response_path = root / "response.json"
            response_path.write_text(
                json.dumps({"items": [{"id": 1, "text": "你好，世界。"}]}),
                encoding="utf-8",
            )
            translated, bilingual, bilingual_ass = apply_agent_translation(
                root / "jobs" / job.id / "job.json",
                response_path,
                target_language="zh-CN",
            )

            self.assertTrue(translated.exists())
            # 模型译文经确定性标点收口后才写入产物：入口漏传 normalize_generated 时此处会失败。
            self.assertIn("你好 世界", translated.read_text(encoding="utf-8"))
            self.assertNotIn("你好，世界。", translated.read_text(encoding="utf-8"))
            self.assertIn("你好 世界", bilingual.read_text(encoding="utf-8"))
            self.assertTrue(bilingual_ass.exists())
            persisted = json.loads((root / "jobs" / job.id / "job.json").read_text())
            self.assertEqual(persisted["stages"]["translate"]["status"], "succeeded")
            self.assertEqual(persisted["stages"]["subtitle_qa"]["status"], "succeeded")
            self.assertEqual(persisted["config"]["translation_policy"], batch_data["policy"])
            self.assertIn(
                "bilingual_ass", {artifact["kind"] for artifact in persisted["artifacts"]}
            )

    def test_standard_agent_translation_accepts_waiting_v4_manifest(self) -> None:
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
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["prompt_version"] = 4
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            persisted = load_job(job_file)
            artifact = next(
                item for item in persisted.artifacts if item.kind == "translation_batches"
            )
            artifact.checksum = file_checksum(manifest_path)
            save_job(persisted)

            response = root / "response.json"
            response.write_text(
                json.dumps({"items": [{"id": 1, "text": "你好"}]}),
                encoding="utf-8",
            )

            translated, *_outputs = apply_agent_translation(
                job_file,
                response,
                target_language="zh-CN",
            )

            self.assertTrue(translated.exists())

    def test_agent_translation_rejects_incomplete_cue_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            job_directory = root / "jobs" / job.id
            artifacts = job_directory / "artifacts"
            artifacts.mkdir()
            cues_path = artifacts / "source-cues.json"
            cues_path.write_text(
                json.dumps([asdict(Cue(1, 0, 1000, "Hello"))]),
                encoding="utf-8",
            )
            media_path = artifacts / "media.json"
            media_path.write_text(json.dumps({"duration_ms": 2000}), encoding="utf-8")
            job.artifacts.extend(
                [
                    Artifact("source_cues", str(cues_path), file_checksum(cues_path)),
                    Artifact("media_info", str(media_path), file_checksum(media_path)),
                ]
            )
            save_job(job)
            response_path = root / "response.json"
            response_path.write_text(json.dumps({"items": []}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "cover the requested cue ids exactly once"):
                apply_agent_translation(
                    job_directory / "job.json",
                    response_path,
                    target_language="zh-CN",
                )

    def test_missing_translation_response_file_is_a_user_input_error(self) -> None:
        """等待态正确、只是 response 路径打错：属于用户输入错误（2），不是依赖故障（3）。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            job_directory = root / "jobs" / job.id
            artifacts = job_directory / "artifacts"
            artifacts.mkdir()
            cues_path = artifacts / "source-cues.json"
            cues_path.write_text(json.dumps([asdict(Cue(1, 0, 1000, "Hello"))]), encoding="utf-8")
            media_path = artifacts / "media.json"
            media_path.write_text(json.dumps({"duration_ms": 2000}), encoding="utf-8")
            job.artifacts.extend(
                [
                    Artifact("source_cues", str(cues_path), file_checksum(cues_path)),
                    Artifact("media_info", str(media_path), file_checksum(media_path)),
                ]
            )
            save_job(job)

            with self.assertRaisesRegex(
                UserInputError, "cannot read translation response"
            ) as caught:
                apply_agent_translation(
                    job_directory / "job.json",
                    root / "gone" / "response.json",
                    target_language="zh-CN",
                )

            self.assertEqual(caught.exception.exit_code, ExitCode.USER_ERROR)

    def test_malformed_translation_response_json_is_a_user_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            response_path = root / "response.json"
            response_path.write_text("{not json", encoding="utf-8")

            with self.assertRaisesRegex(UserInputError, "cannot read translation response"):
                load_translation_response(response_path, {1})

    def test_agent_translation_blocks_glossary_violations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job(
                "/tmp/input.mp4",
                root,
                {
                    "translation_policy": {
                        "style": "faithful",
                        "terms": [{"source": "OpenAI", "target": "OpenAI"}],
                    }
                },
            )
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            cues_path = artifacts / "source-cues.json"
            cues_path.write_text(
                json.dumps([asdict(Cue(1, 0, 1000, "OpenAI is useful."))]),
                encoding="utf-8",
            )
            media_path = artifacts / "media.json"
            media_path.write_text(json.dumps({"duration_ms": 2000}), encoding="utf-8")
            job.artifacts.extend(
                [
                    Artifact("source_cues", str(cues_path), file_checksum(cues_path)),
                    Artifact("media_info", str(media_path), file_checksum(media_path)),
                ]
            )
            job_file = save_job(job)
            response_path = root / "response.json"
            response_path.write_text(
                json.dumps({"items": [{"id": 1, "text": "开放人工智能很有用。"}]}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "subtitle QA failed"):
                apply_agent_translation(job_file, response_path, target_language="zh-CN")

            persisted = json.loads(job_file.read_text())
            report = json.loads((artifacts / "qa-translated.json").read_text())

        self.assertEqual(persisted["stages"]["subtitle_qa"]["status"], "failed")
        self.assertEqual(report["issues"][0]["code"], "glossary_violation")
        self.assertNotIn("bilingual_ass", {item["kind"] for item in persisted["artifacts"]})

    def test_existing_translation_uses_source_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            source_path = artifacts / "source-cues.json"
            source_path.write_text(
                json.dumps([asdict(Cue(1, 1000, 2000, "Hello"))]),
                encoding="utf-8",
            )
            media_path = artifacts / "media.json"
            media_path.write_text(json.dumps({"duration_ms": 3000}), encoding="utf-8")
            job.artifacts.extend(
                [
                    Artifact("source_cues", str(source_path), file_checksum(source_path)),
                    Artifact("media_info", str(media_path), file_checksum(media_path)),
                ]
            )
            save_job(job)
            translation_path = root / "translation.srt"
            translation_path.write_text(
                "1\n00:00:00,000 --> 00:00:00,500\n你好，世界。\n",
                encoding="utf-8",
            )

            translated, bilingual = apply_subtitle_translation(
                artifacts.parent / "job.json",
                translation_path,
                target_language="zh-CN",
            )

            self.assertIn("00:00:01,000 --> 00:00:02,000", translated.read_text())
            self.assertIn("你好，世界。", bilingual.read_text())

    def test_existing_translation_rejects_cue_count_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            source_path = artifacts / "source-cues.json"
            source_path.write_text(
                json.dumps([asdict(Cue(1, 0, 1000, "One")), asdict(Cue(2, 1000, 2000, "Two"))]),
                encoding="utf-8",
            )
            job.artifacts.append(
                Artifact("source_cues", str(source_path), file_checksum(source_path))
            )
            save_job(job)
            translation_path = root / "translation.srt"
            translation_path.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n一个\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "do not cover source cues"):
                apply_subtitle_translation(
                    artifacts.parent / "job.json",
                    translation_path,
                    target_language="zh-CN",
                )

    def test_malformed_provider_response_enters_correction_retry(self) -> None:
        # 回归测试：结构性无效的 provider 响应（ProviderResponseError）必须进入
        # 纠错重试分支，第二次请求携带纠正提示词后成功。
        provider_env = {
            "XVT_TRANSLATION_BASE_URL": "https://provider.example.com/v1",
            "XVT_TRANSLATION_MODEL": "test-model",
            "XVT_TRANSLATION_API_KEY": "secret",
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,500\nHello world.\n",
                encoding="utf-8",
            )
            job = run_subtitle_transcription(source, directory, language="en")

            calls = []

            def flaky_provider(batch, config, *, correction=None, **_kwargs):
                calls.append(correction)
                if len(calls) == 1:
                    raise ProviderResponseError(
                        "translation provider response must contain an items array"
                    )
                return {item.id: "你好，世界。" for item in batch.items}

            events = []
            with (
                patch.dict(os.environ, provider_env, clear=False),
                patch("xvt.pipeline.translation.translate_batch", side_effect=flaky_provider),
                patch("xvt.pipeline.translation.time.sleep"),
            ):
                result = run_translation(
                    get_job_file(job),
                    target_language="zh-CN",
                    provider="openai-compatible",
                    max_retries=2,
                    event_sink=events.append,
                )

            # API 入口的最终产物必须已经过简中标点收口：provider 返回「你好，世界。」，
            # 落盘的 cue 与用户可见字幕都应是「你好 世界」。入口漏传 normalize_generated
            # 时这里会失败。
            persisted = load_job(get_job_file(job))
            translated_cues = json.loads(
                next(
                    Path(artifact.path)
                    for artifact in persisted.artifacts
                    if artifact.kind == "translated_cues"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual([cue["target_text"] for cue in translated_cues], ["你好 世界"])
            translated_srt = next(
                Path(artifact.path)
                for artifact in persisted.artifacts
                if artifact.kind == "translated_srt"
            ).read_text(encoding="utf-8")
            self.assertIn("你好 世界", translated_srt)
            self.assertNotIn("你好，世界。", translated_srt)

        self.assertEqual(result.mode, "openai-compatible")
        self.assertEqual(len(calls), 2)
        self.assertIsNone(calls[0])
        self.assertIn("previous response was invalid", calls[1] or "")

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
            {event.type for event in batch_events},
            {
                "translation_batch_started",
                "translation_batch_retrying",
                "translation_batch_completed",
            },
        )
        for event in batch_events:
            self.assertEqual(event.data["batch_total"], 1)

    def test_transport_and_response_retries_use_independent_budgets(self) -> None:
        provider_env = {
            "XVT_TRANSLATION_BASE_URL": "https://provider.example.com/v1",
            "XVT_TRANSLATION_MODEL": "test-model",
            "XVT_TRANSLATION_API_KEY": "secret",
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,500\nHello world.\n",
                encoding="utf-8",
            )
            job = run_subtitle_transcription(source, directory, language="en")
            corrections = []

            def flaky_provider(batch, config, *, correction=None, **_kwargs):
                corrections.append(correction)
                if len(corrections) == 1:
                    raise TranslationProviderError("disconnected", retryable=True)
                if len(corrections) == 2:
                    raise ProviderResponseError("invalid response")
                if len(corrections) == 3:
                    raise TranslationProviderError("disconnected", retryable=True)
                return {item.id: "你好，世界。" for item in batch.items}

            events = []
            with (
                patch.dict(os.environ, provider_env, clear=False),
                patch("xvt.pipeline.translation.translate_batch", side_effect=flaky_provider),
                patch("xvt.pipeline.translation.time.sleep") as sleep,
                patch("xvt.pipeline.translation._backoff_delay", lambda base: float(base)),
            ):
                run_translation(
                    get_job_file(job),
                    target_language="zh-CN",
                    provider="openai-compatible",
                    max_retries=2,
                    event_sink=events.append,
                )

        self.assertIsNone(corrections[0])
        self.assertIsNone(corrections[1])
        self.assertIn("previous response was invalid", corrections[2] or "")
        # 第 3 次请求是传输失败（请求没被服务端消费），纠正提示词必须原样带到第 4 次；
        # 清空它会退回原始提示词，让纠错预算白烧。
        self.assertIn("previous response was invalid", corrections[3] or "")
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 0.5, 2])
        retry_events = [event for event in events if event.type == "translation_batch_retrying"]
        self.assertEqual([event.data["attempt"] for event in retry_events], [2, 3, 4])
        self.assertEqual(
            [event.data["reason"] for event in retry_events],
            [
                "provider temporarily unavailable",
                "invalid provider response",
                "provider temporarily unavailable",
            ],
        )

    def test_transport_backoff_caps_before_large_exponent(self) -> None:
        provider_env = {
            "XVT_TRANSLATION_BASE_URL": "https://provider.example.com/v1",
            "XVT_TRANSLATION_MODEL": "test-model",
            "XVT_TRANSLATION_API_KEY": "secret",
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,500\nHello world.\n",
                encoding="utf-8",
            )
            job = run_subtitle_transcription(source, directory, language="en")
            calls = 0

            def unavailable_then_success(batch, config, *, correction=None, **_kwargs):
                nonlocal calls
                calls += 1
                if calls <= 6:
                    raise TranslationProviderError("disconnected", retryable=True)
                return {item.id: "你好，世界。" for item in batch.items}

            with (
                patch.dict(os.environ, provider_env, clear=False),
                patch(
                    "xvt.pipeline.translation.translate_batch",
                    side_effect=unavailable_then_success,
                ),
                patch("xvt.pipeline.translation.time.sleep") as sleep,
                patch("xvt.pipeline.translation._backoff_delay", lambda base: float(base)),
            ):
                run_translation(
                    get_job_file(job),
                    target_language="zh-CN",
                    provider="openai-compatible",
                    max_retries=6,
                )

        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2, 4, 8, 16, 30])

    def test_malformed_provider_response_exhausts_retries_as_dependency_error(self) -> None:
        provider_env = {
            "XVT_TRANSLATION_BASE_URL": "https://provider.example.com/v1",
            "XVT_TRANSLATION_MODEL": "test-model",
            "XVT_TRANSLATION_API_KEY": "secret",
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,500\nHello world.\n",
                encoding="utf-8",
            )
            job = run_subtitle_transcription(source, directory, language="en")

            with (
                patch.dict(os.environ, provider_env, clear=False),
                patch(
                    "xvt.pipeline.translation.translate_batch",
                    side_effect=ProviderResponseError("always malformed"),
                ) as provider_call,
                patch("xvt.pipeline.translation.time.sleep") as sleep,
                self.assertRaises(ProviderResponseError) as raised,
            ):
                run_translation(
                    get_job_file(job),
                    target_language="zh-CN",
                    provider="openai-compatible",
                    max_retries=4,
                )

        self.assertEqual(raised.exception.category, "dependency_or_process")
        self.assertEqual(int(raised.exception.exit_code), 3)
        # 纠错预算 _RESPONSE_RETRY_BUDGET=2：首次请求 + 2 次纠错重试 = 3 次调用后才放弃。
        # 传输预算 max_retries=4 不参与该判定。
        self.assertEqual(provider_call.call_count, 3)
        # 坏响应退避恒为 0.5 秒，不加抖动。
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.5, 0.5])

    def test_second_correction_retry_still_inside_response_budget(self) -> None:
        # 预算 _RESPONSE_RETRY_BUDGET=2 的另一条路径：第 1 次纠错仍是坏响应，
        # 第 2 次纠错成功。预算若退回 1，这条路径会在第 3 次请求前直接放弃。
        provider_env = {
            "XVT_TRANSLATION_BASE_URL": "https://provider.example.com/v1",
            "XVT_TRANSLATION_MODEL": "test-model",
            "XVT_TRANSLATION_API_KEY": "secret",
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,500\nHello world.\n",
                encoding="utf-8",
            )
            job = run_subtitle_transcription(source, directory, language="en")
            corrections: list[str | None] = []

            def flaky_provider(batch, config, *, correction=None, **_kwargs):
                corrections.append(correction)
                if len(corrections) <= 2:
                    raise ProviderResponseError("invalid response")
                return {item.id: "你好，世界。" for item in batch.items}

            events = []
            with (
                patch.dict(os.environ, provider_env, clear=False),
                patch("xvt.pipeline.translation.translate_batch", side_effect=flaky_provider),
                patch("xvt.pipeline.translation.time.sleep") as sleep,
            ):
                run_translation(
                    get_job_file(job),
                    target_language="zh-CN",
                    provider="openai-compatible",
                    max_retries=0,
                    event_sink=events.append,
                )

        self.assertEqual(len(corrections), 3)
        self.assertIsNone(corrections[0])
        self.assertIn("previous response was invalid", corrections[1] or "")
        self.assertIn("previous response was invalid", corrections[2] or "")
        # 纠错预算独立于 --max-retries：这里 max_retries=0 仍然给出两次纠错机会。
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.5, 0.5])
        retry_events = [event for event in events if event.type == "translation_batch_retrying"]
        self.assertEqual([event.data["attempt"] for event in retry_events], [2, 3])
        self.assertEqual(
            {event.data["reason"] for event in retry_events}, {"invalid provider response"}
        )

    def test_backoff_delay_stays_inside_equal_jitter_interval(self) -> None:
        # 真随机（不 patch）的区间断言：equal jitter 取 [base/2, base]，恒真无 flaky。
        for base in (1, 2, 4, 8, 16, 30):
            with self.subTest(base=base):
                for _ in range(50):
                    delay = _backoff_delay(base)
                    self.assertGreaterEqual(delay, base / 2)
                    self.assertLessEqual(delay, base)
                    self.assertLessEqual(delay, 30)

    def test_backoff_delay_is_rounded_to_two_decimals(self) -> None:
        # 非同源：不复算 random.uniform，也不 patch 全局 random（translation.random 就是
        # 全局 random 模块对象，patch 它等于污染全局）—— 只断言返回值本身满足「已按两位
        # 小数取整」这一属性。去掉 round 后 uniform 的 17 位有效数字几乎必然违反它
        # （round(d, 2) != d），改成 round(..., 3) 也会被抓住（0.753 != 0.75）；而已取整的
        # 值恒满足两条断言，所以误报率为 0，与上面的区间用例同一标准。
        # 取整是 translation_batch_retrying 事件里的 delay_seconds 与实际 sleep 完全一致的
        # 前提（见 _backoff_delay 的 docstring）。
        for base in (1, 2, 4, 8, 16, 30):
            with self.subTest(base=base):
                for _ in range(50):
                    delay = _backoff_delay(base)
                    self.assertEqual(delay, round(delay, 2))
                    self.assertLessEqual(len(repr(delay).partition(".")[2]), 2)

    def test_transport_backoff_bases_follow_capped_exponential(self) -> None:
        # jitter 确定化：把 _backoff_delay 钉成恒等函数后，可以直接断言喂给它的 base
        # 序列——指数增长并在 _MAX_BACKOFF_SECONDS 处封顶。
        provider_env = {
            "XVT_TRANSLATION_BASE_URL": "https://provider.example.com/v1",
            "XVT_TRANSLATION_MODEL": "test-model",
            "XVT_TRANSLATION_API_KEY": "secret",
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,500\nHello world.\n",
                encoding="utf-8",
            )
            job = run_subtitle_transcription(source, directory, language="en")
            calls = 0

            def unavailable_then_success(batch, config, *, correction=None, **_kwargs):
                nonlocal calls
                calls += 1
                if calls <= 7:
                    raise TranslationProviderError("disconnected", retryable=True)
                return {item.id: "你好，世界。" for item in batch.items}

            bases: list[float] = []

            def record_base(base):
                bases.append(base)
                return float(base)

            with (
                patch.dict(os.environ, provider_env, clear=False),
                patch(
                    "xvt.pipeline.translation.translate_batch",
                    side_effect=unavailable_then_success,
                ),
                patch("xvt.pipeline.translation.time.sleep"),
                patch("xvt.pipeline.translation._backoff_delay", record_base),
            ):
                run_translation(
                    get_job_file(job),
                    target_language="zh-CN",
                    provider="openai-compatible",
                    max_retries=7,
                )

        self.assertEqual(bases, [1, 2, 4, 8, 16, 30, 30])

    def test_workflow_api_translation_blocks_glossary_violations(self) -> None:
        # 回归测试：workflow + API 模式（await_input=False）下 translation_policy 必须
        # 在 prepare 阶段落盘，否则 run_translation 重载 Job 后术语表 QA 会静默失效。
        provider_env = {
            "XVT_TRANSLATION_BASE_URL": "https://provider.example.com/v1",
            "XVT_TRANSLATION_MODEL": "test-model",
            "XVT_TRANSLATION_API_KEY": "secret",
        }
        policy = TranslationPolicy(terms=(GlossaryTerm("OpenAI", "OpenAI"),))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:02,000\nOpenAI is useful.\n",
                encoding="utf-8",
            )
            job = run_subtitle_transcription(source, root, language="en")
            set_workflow_config(
                job,
                WorkflowConfig(target_language="zh-CN", translation_policy=policy),
            )
            job_file = save_job(job)

            def provider(batch, _config, *, correction=None, **_kwargs):
                return {item.id: "开放人工智能很有用" for item in batch.items}

            with (
                patch.dict(os.environ, provider_env, clear=False),
                patch("xvt.pipeline.translation.translate_batch", side_effect=provider),
                self.assertRaisesRegex(RuntimeError, "subtitle QA failed"),
            ):
                run_translation(
                    job_file,
                    target_language="zh-CN",
                    provider="openai-compatible",
                    policy=policy,
                )

            persisted = json.loads(job_file.read_text(encoding="utf-8"))
            report = json.loads(
                (root / "jobs" / job.id / "artifacts" / "qa-translated.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(persisted["config"]["translation_policy"], policy.to_dict())
        self.assertEqual(persisted["stages"]["subtitle_qa"]["status"], "failed")
        self.assertIn("glossary_violation", {issue["code"] for issue in report["issues"]})

    def test_workflow_glossary_qa_survives_missing_top_level_policy(self) -> None:
        # 回归测试：即便顶层 config 没有 translation_policy（旧 Job 或被清理），
        # workflow 快照里的术语表也必须参与译文 QA。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:02,000\nOpenAI is useful.\n",
                encoding="utf-8",
            )
            job = run_subtitle_transcription(source, root, language="en")
            set_workflow_config(
                job,
                WorkflowConfig(
                    target_language="zh-CN",
                    translation_policy=TranslationPolicy(terms=(GlossaryTerm("OpenAI", "OpenAI"),)),
                ),
            )
            job_file = save_job(job)
            manifest_path = prepare_agent_translation(
                job_file,
                source_language="en",
                target_language="zh-CN",
            )
            self.assertTrue(manifest_path.exists())
            stale = load_job(job_file)
            stale.config.pop("translation_policy")
            save_job(stale)

            response_path = root / "response.json"
            response_path.write_text(
                json.dumps({"items": [{"id": 1, "text": "开放人工智能很有用"}]}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "subtitle QA failed"):
                apply_agent_translation(job_file, response_path, target_language="zh-CN")

            report = json.loads(
                (root / "jobs" / job.id / "artifacts" / "qa-translated.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertIn("glossary_violation", {issue["code"] for issue in report["issues"]})

    def test_prepare_agent_translation_persists_policy_for_api_mode(self) -> None:
        # 回归测试：API 模式（await_input=False）没有 set_stage 兜底保存，
        # prepare 返回后重新 load_job 必须能读到策略与语言配置。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello.\n",
                encoding="utf-8",
            )
            job = run_subtitle_transcription(source, root, language="en")
            set_workflow_config(job, WorkflowConfig(target_language="zh-CN"))
            job_file = save_job(job)
            policy = TranslationPolicy(style="natural", terms=(GlossaryTerm("Hello", "你好"),))

            prepare_agent_translation(
                job_file,
                source_language="en",
                target_language="zh-CN",
                policy=policy,
                await_input=False,
            )

            persisted = load_job(job_file)

        self.assertEqual(persisted.config["translation_policy"], policy.to_dict())
        self.assertEqual(persisted.config["target_language"], "zh-CN")
        self.assertEqual(persisted.config["source_language"], "en")
        # workflow Job 的质量由 workflow 配置派生，不得写入顶层 config。
        self.assertNotIn("translation_quality", persisted.config)

    def test_load_translation_response_rejects_boolean_cue_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for value in (True, False):
                response_path = root / f"response-{value}.json"
                response_path.write_text(
                    json.dumps({"items": [{"id": value, "text": "你好"}]}),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "invalid or duplicate cue id"):
                    load_translation_response(response_path, {1})

    def test_agent_translation_rejects_stale_source_cues(self) -> None:
        # 回归测试：prepare 之后源 cue 变更，旧响应必须被拒绝而不是错位回填。
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
            )
            response_path = root / "response.json"
            response_path.write_text(
                json.dumps({"items": [{"id": 1, "text": "你好"}]}),
                encoding="utf-8",
            )

            persisted = load_job(job_file)
            source_cues = next(item for item in persisted.artifacts if item.kind == "source_cues")
            cues_path = Path(source_cues.path)
            cues_path.write_text(
                json.dumps([asdict(Cue(1, 0, 1000, "Completely different source."))]),
                encoding="utf-8",
            )
            source_cues.checksum = file_checksum(cues_path)
            save_job(persisted)

            with self.assertRaisesRegex(ValueError, "prepared for different source cues"):
                apply_agent_translation(job_file, response_path, target_language="zh-CN")

    def test_agent_translation_rejects_tampered_manifest(self) -> None:
        # 回归测试：manifest 已登记但 checksum 失效属于可疑状态，不能与「极旧 Job
        # 从未登记 manifest」走同一放行路径，否则会绕过源 cue 绑定门。
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
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["source_cues_checksum"] = "0" * 64
            # 只改文件、不更新已登记 checksum。
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            response_path = root / "response.json"
            response_path.write_text(
                json.dumps({"items": [{"id": 1, "text": "你好"}]}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "checksum is stale"):
                apply_agent_translation(job_file, response_path, target_language="zh-CN")

    def test_agent_translation_rejects_unreadable_manifest(self) -> None:
        # checksum 有效但内容不是 JSON 对象时同样 fail closed。
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
            )
            manifest_path.write_text("not json at all", encoding="utf-8")
            persisted = load_job(job_file)
            artifact = next(
                item for item in persisted.artifacts if item.kind == "translation_batches"
            )
            artifact.checksum = file_checksum(manifest_path)
            save_job(persisted)

            response_path = root / "response.json"
            response_path.write_text(
                json.dumps({"items": [{"id": 1, "text": "你好"}]}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "not a readable JSON object"):
                apply_agent_translation(job_file, response_path, target_language="zh-CN")

    def test_agent_translation_accepts_manifest_without_source_checksum(self) -> None:
        # 前向兼容：修复前生成的标准 manifest 没有 source_cues_checksum，仍必须可回填。
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
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.pop("source_cues_checksum")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            persisted = load_job(job_file)
            artifact = next(
                item for item in persisted.artifacts if item.kind == "translation_batches"
            )
            artifact.checksum = file_checksum(manifest_path)
            save_job(persisted)

            response_path = root / "response.json"
            response_path.write_text(
                json.dumps({"items": [{"id": 1, "text": "你好"}]}),
                encoding="utf-8",
            )

            translated, *_outputs = apply_agent_translation(
                job_file,
                response_path,
                target_language="zh-CN",
            )

            self.assertTrue(translated.exists())


if __name__ == "__main__":
    unittest.main()

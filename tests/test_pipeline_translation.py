"""翻译主链（pipeline.translation）的流程测试。"""

import json
import os
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

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
from xvt.process import CommandResult
from xvt.recovery import file_checksum
from xvt.translation_provider import ProviderResponseError, TranslationProviderError


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
                json.dumps({"items": [{"id": 1, "text": "你好"}]}),
                encoding="utf-8",
            )
            translated, bilingual, bilingual_ass = apply_agent_translation(
                root / "jobs" / job.id / "job.json",
                response_path,
                target_language="zh-CN",
            )

            self.assertTrue(translated.exists())
            self.assertIn("你好", bilingual.read_text())
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
                    Artifact("source_cues", str(cues_path)),
                    Artifact("media_info", str(media_path)),
                ]
            )
            save_job(job)
            response_path = root / "response.json"
            response_path.write_text(json.dumps({"items": []}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "cover every cue"):
                apply_agent_translation(
                    job_directory / "job.json",
                    response_path,
                    target_language="zh-CN",
                )

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
                    Artifact("source_cues", str(cues_path)),
                    Artifact("media_info", str(media_path)),
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
                [Artifact("source_cues", str(source_path)), Artifact("media_info", str(media_path))]
            )
            save_job(job)
            translation_path = root / "translation.srt"
            translation_path.write_text(
                "1\n00:00:00,000 --> 00:00:00,500\n你好\n",
                encoding="utf-8",
            )

            translated, bilingual = apply_subtitle_translation(
                artifacts.parent / "job.json",
                translation_path,
                target_language="zh-CN",
            )

            self.assertIn("00:00:01,000 --> 00:00:02,000", translated.read_text())
            self.assertIn("你好", bilingual.read_text())

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
            job.artifacts.append(Artifact("source_cues", str(source_path)))
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
            ):
                result = run_translation(
                    get_job_file(job),
                    target_language="zh-CN",
                    provider="openai-compatible",
                    max_retries=2,
                    event_sink=events.append,
                )

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
        self.assertIsNone(corrections[3])
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
        self.assertEqual(provider_call.call_count, 2)


if __name__ == "__main__":
    unittest.main()

import json
import os
import tempfile
import unittest
from dataclasses import asdict
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from xvt.errors import UserInputError
from xvt.job_store import create_job, save_job
from xvt.models import Artifact, Cue
from xvt.pipeline import run_translation
from xvt.recovery import file_checksum
from xvt.translate import GlossaryTerm, TranslationPolicy, build_translation_batches
from xvt.translation_provider import (
    CompatibleProviderConfig,
    ProviderResponseError,
    TranslationProviderError,
    resolve_provider,
    translate_batch,
)


class TranslationProviderTest(unittest.TestCase):
    def test_auto_mode_falls_back_when_configuration_is_incomplete(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            mode, config, reason = resolve_provider(
                "auto",
                base_url=None,
                model=None,
                api_key_env="XVT_TRANSLATION_API_KEY",
            )

        self.assertEqual(mode, "agent")
        self.assertIsNone(config)
        self.assertIsNotNone(reason)
        self.assertIn("base URL", reason or "")

    def test_explicit_provider_missing_configuration_is_user_error(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(UserInputError, "openai-compatible provider is missing"),
        ):
            resolve_provider(
                "openai-compatible",
                base_url=None,
                model=None,
                api_key_env="XVT_TRANSLATION_API_KEY",
            )

    def test_incompatible_response_is_dependency_error(self) -> None:
        cue = Cue(1, 0, 1000, "Hello", source_language="en")
        batch = build_translation_batches([cue], source_language="en", target_language="zh-CN")[0]

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return json.dumps({"unexpected": True}).encode()

        def opener(_request, **_kwargs):
            return Response()

        with self.assertRaisesRegex(ProviderResponseError, "incompatible response"):
            translate_batch(
                batch,
                CompatibleProviderConfig("https://example.com/v1", "model", "secret"),
                opener=opener,
            )

    def test_non_json_body_is_provider_response_error(self) -> None:
        # P7f：真实解析路径——HTTP 200 但响应体不是 JSON。
        cue = Cue(1, 0, 1000, "Hello", source_language="en")
        batch = build_translation_batches([cue], source_language="en", target_language="zh-CN")[0]

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return b"<html>gateway error</html>"

        with self.assertRaisesRegex(ProviderResponseError, "non-JSON"):
            translate_batch(
                batch,
                CompatibleProviderConfig("https://example.com/v1", "model", "secret"),
                opener=lambda *_args, **_kwargs: Response(),
            )

    def test_non_utf8_body_is_provider_response_error(self) -> None:
        # P7g：HTTP 200 但响应体不是合法 UTF-8（真实 bytes 路径）。
        cue = Cue(1, 0, 1000, "Hello", source_language="en")
        batch = build_translation_batches([cue], source_language="en", target_language="zh-CN")[0]

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return b"\xff\xfe broken"

        with self.assertRaisesRegex(ProviderResponseError, "non-UTF-8") as raised:
            translate_batch(
                batch,
                CompatibleProviderConfig("https://example.com/v1", "model", "secret"),
                opener=lambda *_args, **_kwargs: Response(),
            )
        self.assertEqual(raised.exception.category, "dependency_or_process")

    def test_mismatched_cue_ids_are_provider_response_error(self) -> None:
        # P7f：真实解析路径——模型返回了错误的 cue ID（不 mock 专用异常）。
        cue = Cue(1, 0, 1000, "Hello", source_language="en")
        batch = build_translation_batches([cue], source_language="en", target_language="zh-CN")[0]

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                payload = {
                    "choices": [{"message": {"content": '{"items":[{"id":99,"text":"你好"}]}'}}]
                }
                return json.dumps(payload).encode()

        with self.assertRaises(ProviderResponseError) as raised:
            translate_batch(
                batch,
                CompatibleProviderConfig("https://example.com/v1", "model", "secret"),
                opener=lambda *_args, **_kwargs: Response(),
            )
        self.assertEqual(raised.exception.category, "dependency_or_process")

    def test_empty_translation_text_is_provider_response_error(self) -> None:
        cue = Cue(1, 0, 1000, "Hello", source_language="en")
        batch = build_translation_batches([cue], source_language="en", target_language="zh-CN")[0]

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                payload = {
                    "choices": [{"message": {"content": '{"items":[{"id":1,"text":"  "}]}'}}]
                }
                return json.dumps(payload).encode()

        with self.assertRaises(ProviderResponseError):
            translate_batch(
                batch,
                CompatibleProviderConfig("https://example.com/v1", "model", "secret"),
                opener=lambda *_args, **_kwargs: Response(),
            )

    def test_compatible_provider_parses_fenced_json(self) -> None:
        cue = Cue(1, 0, 1000, "Hello", source_language="en")
        batch = build_translation_batches([cue], source_language="en", target_language="zh-CN")[0]

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                payload = {
                    "choices": [
                        {"message": {"content": '```json\n{"items":[{"id":1,"text":"你好"}]}\n```'}}
                    ]
                }
                return json.dumps(payload).encode()

        def opener(_request, **_kwargs):
            return Response()

        translated = translate_batch(
            batch,
            CompatibleProviderConfig("https://example.com/v1", "model", "secret"),
            opener=opener,
        )

        self.assertEqual(translated, {1: "你好"})

    def test_provider_sends_style_and_glossary_policy(self) -> None:
        cue = Cue(1, 0, 1000, "OpenAI is useful", source_language="en")
        batch = build_translation_batches(
            [cue],
            source_language="en",
            target_language="zh-CN",
            policy=TranslationPolicy("natural", (GlossaryTerm("OpenAI", "OpenAI"),)),
        )[0]
        request_payload = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return json.dumps(
                    {
                        "choices": [
                            {"message": {"content": '{"items":[{"id":1,"text":"OpenAI 很有用"}]}'}}
                        ]
                    }
                ).encode()

        def opener(request, **_kwargs):
            request_payload.update(json.loads(request.data))
            return Response()

        translate_batch(
            batch,
            CompatibleProviderConfig("https://example.com/v1", "model", "secret"),
            opener=opener,
        )

        self.assertIn("idiomatic", request_payload["messages"][0]["content"])
        self.assertEqual(
            json.loads(request_payload["messages"][1]["content"])["policy"]["terms"],
            [{"source": "OpenAI", "target": "OpenAI"}],
        )

    def test_provider_classifies_rate_limit_as_retryable(self) -> None:
        cue = Cue(1, 0, 1000, "Hello", source_language="en")
        batch = build_translation_batches([cue], source_language="en", target_language="zh-CN")[0]
        headers = Message()
        headers["Retry-After"] = "3"

        def opener(request, **_kwargs):
            raise HTTPError(request.full_url, 429, "rate limited", headers, None)

        with self.assertRaises(TranslationProviderError) as raised:
            translate_batch(
                batch,
                CompatibleProviderConfig("https://example.com/v1", "model", "secret"),
                opener=opener,
            )

        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.retry_after, 3)

    def test_provider_does_not_retry_authentication_errors(self) -> None:
        cue = Cue(1, 0, 1000, "Hello", source_language="en")
        batch = build_translation_batches([cue], source_language="en", target_language="zh-CN")[0]

        def opener(request, **_kwargs):
            raise HTTPError(request.full_url, 401, "unauthorized", Message(), None)

        with self.assertRaises(TranslationProviderError) as raised:
            translate_batch(
                batch,
                CompatibleProviderConfig("https://example.com/v1", "model", "secret"),
                opener=opener,
            )

        self.assertFalse(raised.exception.retryable)

    def test_pipeline_retries_rate_limit_with_correction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root, {"source_language": "en"})
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            cue = Cue(1, 0, 2000, "Hello", source_language="en")
            cues_path = artifacts / "source-cues.json"
            cues_path.write_text(json.dumps([asdict(cue)]), encoding="utf-8")
            media_path = artifacts / "media.json"
            media_path.write_text(
                json.dumps({"duration_ms": 3000, "width": None, "height": None}),
                encoding="utf-8",
            )
            job.artifacts.extend(
                [
                    Artifact("source_cues", str(cues_path), file_checksum(cues_path)),
                    Artifact("media_info", str(media_path), file_checksum(media_path)),
                ]
            )
            job_file = save_job(job)
            environment = {
                "XVT_TRANSLATION_BASE_URL": "https://example.com/v1",
                "XVT_TRANSLATION_MODEL": "model",
                "XVT_TRANSLATION_API_KEY": "secret",
            }
            corrections = []

            def translate(_batch, _config, *, correction=None):
                corrections.append(correction)
                if len(corrections) == 1:
                    raise TranslationProviderError("rate limited", retryable=True, retry_after=3)
                return {1: "你好"}

            with (
                patch.dict(os.environ, environment, clear=True),
                patch("xvt.pipeline.translation.translate_batch", side_effect=translate),
                patch("xvt.pipeline.translation.time.sleep") as sleep,
            ):
                result = run_translation(
                    job_file,
                    target_language="zh-CN",
                    max_retries=1,
                )

        sleep.assert_called_once_with(3)
        self.assertEqual(corrections[0], None)
        self.assertIsNotNone(corrections[1])
        self.assertTrue(any(path.name == "translated.srt" for path in result.paths))

    def test_translation_resumes_from_completed_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root, {"source_language": "en"})
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            cues = [
                Cue(1, 0, 2000, "Hello", source_language="en"),
                Cue(2, 2000, 4000, "World", source_language="en"),
            ]
            cues_path = artifacts / "source-cues.json"
            cues_path.write_text(json.dumps([asdict(cue) for cue in cues]), encoding="utf-8")
            media_path = artifacts / "media.json"
            media_path.write_text(
                json.dumps({"duration_ms": 5000, "width": None, "height": None}),
                encoding="utf-8",
            )
            job.artifacts.extend(
                [
                    Artifact("source_cues", str(cues_path), file_checksum(cues_path)),
                    Artifact("media_info", str(media_path), file_checksum(media_path)),
                ]
            )
            job_file = save_job(job)
            environment = {
                "XVT_TRANSLATION_BASE_URL": "https://example.com/v1",
                "XVT_TRANSLATION_MODEL": "model",
                "XVT_TRANSLATION_API_KEY": "secret",
            }
            first_calls = []

            def first_attempt(batch, _config):
                first_calls.append(batch.id)
                if batch.id == 2:
                    raise RuntimeError("temporary failure")
                return {1: "你好"}

            with (
                patch.dict(os.environ, environment, clear=True),
                patch("xvt.pipeline.translation.translate_batch", side_effect=first_attempt),
                self.assertRaisesRegex(RuntimeError, "temporary failure"),
            ):
                run_translation(job_file, target_language="zh-CN", batch_size=1, max_retries=0)

            second_calls = []

            def second_attempt(batch, _config):
                second_calls.append(batch.id)
                return {2: "世界"}

            with (
                patch.dict(os.environ, environment, clear=True),
                patch("xvt.pipeline.translation.translate_batch", side_effect=second_attempt),
            ):
                result = run_translation(
                    job_file,
                    target_language="zh-CN",
                    batch_size=1,
                    max_retries=0,
                )

        self.assertEqual(first_calls, [1, 2])
        self.assertEqual(second_calls, [2])
        self.assertEqual(result.mode, "openai-compatible")
        self.assertTrue(any(path.name == "translated.srt" for path in result.paths))

    def test_translation_discards_progress_when_target_language_or_policy_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.mp4", root, {"source_language": "en"})
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            cue = Cue(1, 0, 2000, "Hello", source_language="en")
            cues_path = artifacts / "source-cues.json"
            cues_path.write_text(json.dumps([asdict(cue)]), encoding="utf-8")
            media_path = artifacts / "media.json"
            media_path.write_text(
                json.dumps({"duration_ms": 3000, "width": None, "height": None}),
                encoding="utf-8",
            )
            job.artifacts.extend(
                [
                    Artifact("source_cues", str(cues_path), file_checksum(cues_path)),
                    Artifact("media_info", str(media_path), file_checksum(media_path)),
                ]
            )
            job_file = save_job(job)
            environment = {
                "XVT_TRANSLATION_BASE_URL": "https://example.com/v1",
                "XVT_TRANSLATION_MODEL": "model",
                "XVT_TRANSLATION_API_KEY": "secret",
            }
            calls = []

            def translate(batch, _config):
                calls.append((batch.target_language, batch.policy.style))
                text = "你好" if batch.target_language == "zh-CN" else "こんにちは"
                return {1: text}

            with (
                patch.dict(os.environ, environment, clear=True),
                patch("xvt.pipeline.translation.translate_batch", side_effect=translate),
            ):
                run_translation(job_file, target_language="zh-CN")
                run_translation(job_file, target_language="ja")
                run_translation(
                    job_file,
                    target_language="zh-CN",
                    translation_style="natural",
                )

            progress = json.loads(
                (artifacts / "translation-progress.json").read_text(encoding="utf-8")
            )

        self.assertEqual(
            calls,
            [("zh-CN", "faithful"), ("ja", "faithful"), ("zh-CN", "natural")],
        )
        self.assertIn("fingerprint", progress)
        self.assertEqual(progress["items"], [{"id": 1, "text": "你好"}])

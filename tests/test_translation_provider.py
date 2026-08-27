import json
import os
import tempfile
import unittest
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from email.message import Message
from email.utils import format_datetime
from http.client import IncompleteRead, RemoteDisconnected, ResponseNotReady
from pathlib import Path
from ssl import SSLCertVerificationError, SSLEOFError, SSLZeroReturnError
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from xvt.errors import UserInputError
from xvt.job_store import create_job, save_job
from xvt.models import Artifact, Cue
from xvt.pipeline import run_translation
from xvt.pipeline.translation import translate_batch_with_retries
from xvt.recovery import file_checksum
from xvt.translate import (
    GlossaryTerm,
    TranslationPolicy,
    build_polish_batches,
    build_semantic_repair_batches,
    build_semantic_review_batches,
    build_translation_batches,
    parse_semantic_review_response,
)
from xvt.translation_provider import (
    CompatibleProviderConfig,
    ProviderResponseError,
    TranslationProviderError,
    polish_batch,
    resolve_provider,
    semantic_repair_batch,
    semantic_review_batch,
    translate_batch,
)


class TranslationProviderTest(unittest.TestCase):
    def test_semantic_review_provider_requires_issues_array(self) -> None:
        source = [Cue(1, 0, 1000, "Hello", source_language="en")]
        candidate = [Cue(1, 0, 1000, "Hello", target_text="你好", target_language="zh-CN")]
        batch = build_semantic_review_batches(
            source,
            candidate,
            source_language="en",
            target_language="zh-CN",
        )[0]

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                content = json.dumps({"items": []})
                return json.dumps({"choices": [{"message": {"content": content}}]}).encode()

        with self.assertRaisesRegex(ProviderResponseError, "issues array"):
            semantic_review_batch(
                batch,
                CompatibleProviderConfig("https://example.com/v1", "model", "secret"),
                opener=lambda *_args, **_kwargs: Response(),
            )

    def test_semantic_repair_provider_sends_complete_group_and_issues(self) -> None:
        source = [Cue(1, 0, 1000, "Hello", source_language="en")]
        candidate = [Cue(1, 0, 1000, "Hello", target_text="你好", target_language="zh-CN")]
        review_batch = build_semantic_review_batches(
            source,
            candidate,
            source_language="en",
            target_language="zh-CN",
        )[0]
        issues = parse_semantic_review_response(
            review_batch,
            [
                {
                    "category": "meaning_error",
                    "cue_ids": [1],
                    "severity": "review",
                    "message": "wrong meaning",
                    "suggested_items": [{"id": 1, "text": "您好"}],
                }
            ],
        )
        batch = build_semantic_repair_batches(
            source,
            candidate,
            issues,
            source_language="en",
            target_language="zh-CN",
        )[0]
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                content = json.dumps({"items": [{"id": 1, "text": "您好"}]})
                return json.dumps({"choices": [{"message": {"content": content}}]}).encode()

        def opener(request, **_kwargs):
            captured.update(json.loads(request.data))
            return Response()

        result = semantic_repair_batch(
            batch,
            CompatibleProviderConfig("https://example.com/v1", "model", "secret"),
            opener=opener,
        )

        self.assertEqual(result, {1: "您好"})
        manifest = json.loads(captured["messages"][1]["content"])
        prompt = captured["messages"][0]["content"]
        self.assertEqual([item["id"] for item in manifest["items"]], [1])
        self.assertEqual(manifest["issues"][0]["category"], "meaning_error")
        self.assertIn("rewriting each complete semantic_groups entry jointly", prompt)
        self.assertIn("suggested_items as review hints", prompt)
        self.assertIn("do not invent corrections to suspicious source text", prompt)
        self.assertIn("Do not reintroduce", prompt)
        self.assertIn("不是……而是……", prompt)
        self.assertIn("source-grounded contrast", prompt)
        self.assertIn("untrusted subtitle content", prompt)

    def test_polish_provider_sends_draft_and_rejects_context_ids(self) -> None:
        source = [
            Cue(1, 0, 1000, "Hello", source_language="en"),
            Cue(2, 1000, 2000, "World", source_language="en"),
        ]
        draft = [
            Cue(1, 0, 1000, "Hello", target_text="你好", target_language="zh-CN"),
            Cue(2, 1000, 2000, "World", target_text="世界", target_language="zh-CN"),
        ]
        batch = build_polish_batches(
            source,
            draft,
            source_language="en",
            target_language="zh-CN",
            batch_size=1,
        )[0]
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                content = json.dumps({"items": [{"id": 1, "text": "你好。"}]})
                return json.dumps({"choices": [{"message": {"content": content}}]}).encode()

        def opener(request, **_kwargs):
            captured.update(json.loads(request.data))
            return Response()

        result = polish_batch(
            batch,
            CompatibleProviderConfig("https://example.com/v1", "model", "secret"),
            opener=opener,
        )

        self.assertEqual(result, {1: "你好。"})
        manifest = json.loads(captured["messages"][1]["content"])
        self.assertEqual(manifest["items"][0]["draft_text"], "你好")
        self.assertEqual([item["id"] for item in manifest["context_after"]], [2])
        prompt = captured["messages"][0]["content"]
        self.assertIn("one joined utterance", prompt)
        self.assertIn("dangling fragments", prompt)
        self.assertIn("proper names", prompt)
        self.assertIn("read-only context", prompt)
        self.assertIn("gratuitous em dashes", prompt)
        self.assertIn("formulaic contrasts", prompt)
        self.assertIn("不仅……而且……", prompt)
        self.assertIn("empty promotional language", prompt)
        self.assertIn("source genuinely expresses", prompt)
        self.assertIn("untrusted subtitle content", prompt)

    def test_semantic_review_prompt_distinguishes_source_suspect(self) -> None:
        source = [Cue(1, 0, 1000, "Cloud Code", source_language="en")]
        candidate = [
            Cue(
                1,
                0,
                1000,
                "Cloud Code",
                target_text="Cloud Code",
                target_language="zh-CN",
            )
        ]
        batch = build_semantic_review_batches(
            source,
            candidate,
            source_language="en",
            target_language="zh-CN",
        )[0]
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                content = json.dumps({"issues": []})
                return json.dumps({"choices": [{"message": {"content": content}}]}).encode()

        def opener(request, **_kwargs):
            captured.update(json.loads(request.data))
            return Response()

        self.assertEqual(
            semantic_review_batch(
                batch,
                CompatibleProviderConfig("https://example.com/v1", "model", "secret"),
                opener=opener,
            ),
            (),
        )
        prompt = captured["messages"][0]["content"]
        self.assertIn("joined source utterance", prompt)
        self.assertIn("not stylistic preferences", prompt)
        self.assertIn("recognizable proper name", prompt)
        self.assertIn("suggested_items must be empty", prompt)
        self.assertIn("at least two adjacent cue ids", prompt)
        self.assertIn("never report it for a single cue", prompt)
        self.assertIn("treat ASCII spaces and cue boundaries as valid separators", prompt)
        self.assertIn("Do not report awkward_join", prompt)
        self.assertIn("solely because those marks are absent", prompt)
        self.assertIn("untrusted subtitle content", prompt)

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

    def test_provider_payload_exposes_semantic_groups_and_prompt_constraints(self) -> None:
        cues = [
            Cue(1, 0, 500, "First"),
            Cue(2, 500, 1000, "continues."),
            Cue(3, 1000, 1500, "Next sentence."),
        ]
        batch = build_translation_batches(cues, source_language="en", target_language="zh-CN")[0]
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "items": [
                                                {"id": 1, "text": "第一部分"},
                                                {"id": 2, "text": "延续。"},
                                                {"id": 3, "text": "下一句。"},
                                            ]
                                        }
                                    )
                                }
                            }
                        ]
                    }
                ).encode()

        def opener(request, **_kwargs):
            captured.update(json.loads(request.data.decode()))
            return Response()

        translate_batch(
            batch,
            CompatibleProviderConfig("https://example.com/v1", "model", "secret"),
            opener=opener,
        )

        prompt = captured["messages"][0]["content"]
        manifest = json.loads(captured["messages"][1]["content"])
        self.assertEqual(manifest["semantic_groups"], [[1, 2], [3]])
        self.assertIn("semantic_groups", prompt)
        self.assertIn("one continuous utterance", prompt)
        self.assertIn("never move meaning between groups", prompt)
        self.assertIn("do not guess corrections to suspicious source text", prompt)
        self.assertIn("display recommendation", prompt)
        self.assertIn("gratuitous em dashes", prompt)
        self.assertIn("formulaic contrasts", prompt)
        self.assertIn("不是……而是……", prompt)
        self.assertIn("template-like connective phrases", prompt)
        self.assertIn("source genuinely expresses", prompt)
        self.assertIn("untrusted subtitle content", prompt)

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

    def _retry_after_for_header(self, value: str) -> float | None:
        cue = Cue(1, 0, 1000, "Hello", source_language="en")
        batch = build_translation_batches([cue], source_language="en", target_language="zh-CN")[0]
        headers = Message()
        headers["Retry-After"] = value

        def opener(request, **_kwargs):
            raise HTTPError(request.full_url, 429, "rate limited", headers, None)

        with self.assertRaises(TranslationProviderError) as raised:
            translate_batch(
                batch,
                CompatibleProviderConfig("https://example.com/v1", "model", "secret"),
                opener=opener,
            )
        return raised.exception.retry_after

    def test_retry_after_parses_http_dates_and_rejects_non_integer_deltas(self) -> None:
        # RFC 9110 的 Retry-After 只有两种形态：delta-seconds（纯整数）与 HTTP-date。
        # 此前的裸 float() 会把 "1.5"/"inf"/"nan"/"-1" 全部接受，也完全读不懂 HTTP-date。
        now = datetime.now(UTC)
        self.assertEqual(self._retry_after_for_header("3"), 3.0)
        # 首部值两侧的空白按 HTTP 语义无意义，strip 后仍是合法的 delta-seconds。
        self.assertEqual(self._retry_after_for_header(" 5 "), 5.0)

        future = self._retry_after_for_header(format_datetime(now + timedelta(seconds=5)))
        self.assertIsNotNone(future)
        # 断言区间而不是等值：格式化到秒会截断亚秒部分，且解析与断言之间会流逝时间。
        self.assertGreaterEqual(future, 3.0)
        self.assertLessEqual(future, 5.0)

        # 无时区的 HTTP-date 按 GMT 处理；不补 tzinfo 会在与 aware datetime 相减时 TypeError。
        naive = (now + timedelta(seconds=5)).strftime("%a, %d %b %Y %H:%M:%S")
        naive_delta = self._retry_after_for_header(naive)
        self.assertIsNotNone(naive_delta)
        self.assertGreaterEqual(naive_delta, 3.0)
        self.assertLessEqual(naive_delta, 5.0)

        # 已过期的日期（或服务端时钟偏慢）钳到 0，而不是负延迟。
        past = self._retry_after_for_header(format_datetime(now - timedelta(days=1)))
        self.assertEqual(past, 0.0)

        # 本函数不设上限，30 秒钳位是调用侧（pipeline.translation）的职责。
        far = self._retry_after_for_header(format_datetime(now + timedelta(days=1)))
        self.assertIsNotNone(far)
        self.assertGreater(far, 30.0)

        # 纯数字串也能溢出成 inf：float("1" * 400) 不报错，直接返回 inf。放行它会让
        # retry_after 这个公开字段带出非有限值，只靠调用侧的 isfinite 兜底太脆。
        self.assertIsNone(self._retry_after_for_header("1" * 400))

        for value in ("soon", "inf", "nan", "1.5", "-1", "1_0", "²", "٥", "", "   "):
            with self.subTest(value=value):
                # '²'.isdigit() 为 True 但 float('²') 抛 ValueError，正则 \d 又会放行 '٥'：
                # 两者都必须落在 None 分支，不能让异常逃出解析函数。
                self.assertIsNone(self._retry_after_for_header(value))

    def test_retry_after_path_clamps_to_cap_and_skips_jitter(self) -> None:
        # 服务端指定的重试时刻不参与抖动：只做 30 秒上限钳位，_backoff_delay 零调用。
        cue = Cue(1, 0, 1000, "Hello", source_language="en")
        batch = build_translation_batches([cue], source_language="en", target_language="zh-CN")[0]
        calls: list[str | None] = []

        def request(_batch, _config, correction=None):
            calls.append(correction)
            if len(calls) == 1:
                raise TranslationProviderError("rate limited", retryable=True, retry_after=86400.0)
            return {1: "你好"}

        delays: list[float] = []
        with (
            patch("xvt.pipeline.translation.time.sleep", lambda delay: delays.append(delay)),
            patch("xvt.pipeline.translation._backoff_delay") as backoff,
        ):
            result = translate_batch_with_retries(
                batch,
                CompatibleProviderConfig("https://example.com/v1", "model", "secret"),
                max_retries=2,
                request=request,
            )

        self.assertEqual(result, {1: "你好"})
        self.assertEqual(delays, [30])
        backoff.assert_not_called()

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

    def test_provider_does_not_retry_non_retryable_http_errors(self) -> None:
        cue = Cue(1, 0, 1000, "Hello", source_language="en")
        batch = build_translation_batches([cue], source_language="en", target_language="zh-CN")[0]

        def opener(request, **_kwargs):
            raise HTTPError(request.full_url, 400, "bad request", Message(), None)

        with self.assertRaises(TranslationProviderError) as raised:
            translate_batch(
                batch,
                CompatibleProviderConfig("https://example.com/v1", "model", "secret"),
                opener=opener,
            )

        self.assertFalse(raised.exception.retryable)

    def test_provider_classifies_remote_disconnect_as_retryable(self) -> None:
        cue = Cue(1, 0, 1000, "Hello", source_language="en")
        batch = build_translation_batches([cue], source_language="en", target_language="zh-CN")[0]

        def opener(_request, **_kwargs):
            raise RemoteDisconnected("remote closed connection without response")

        with self.assertRaises(TranslationProviderError) as raised:
            translate_batch(
                batch,
                CompatibleProviderConfig("https://example.com/v1", "model", "secret"),
                opener=opener,
            )

        self.assertTrue(raised.exception.retryable)
        self.assertIn("connection failed", str(raised.exception))

    def test_provider_classifies_response_read_connection_reset_as_retryable(self) -> None:
        cue = Cue(1, 0, 1000, "Hello", source_language="en")
        batch = build_translation_batches([cue], source_language="en", target_language="zh-CN")[0]

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                raise ConnectionResetError("connection reset by peer")

        with self.assertRaises(TranslationProviderError) as raised:
            translate_batch(
                batch,
                CompatibleProviderConfig("https://example.com/v1", "model", "secret"),
                opener=lambda *_args, **_kwargs: Response(),
            )

        self.assertTrue(raised.exception.retryable)

    def test_provider_classifies_incomplete_chunked_read_as_retryable(self) -> None:
        cue = Cue(1, 0, 1000, "Hello", source_language="en")
        batch = build_translation_batches([cue], source_language="en", target_language="zh-CN")[0]

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                raise IncompleteRead(b"")

        with self.assertRaises(TranslationProviderError) as raised:
            translate_batch(
                batch,
                CompatibleProviderConfig("https://example.com/v1", "model", "secret"),
                opener=lambda *_args, **_kwargs: Response(),
            )

        self.assertTrue(raised.exception.retryable)
        self.assertIn("transfer failed", str(raised.exception))

    def test_provider_does_not_retry_connection_state_protocol_errors(self) -> None:
        cue = Cue(1, 0, 1000, "Hello", source_language="en")
        batch = build_translation_batches([cue], source_language="en", target_language="zh-CN")[0]

        def opener(_request, **_kwargs):
            raise ResponseNotReady("client connection state error")

        with self.assertRaises(TranslationProviderError) as raised:
            translate_batch(
                batch,
                CompatibleProviderConfig("https://example.com/v1", "model", "secret"),
                opener=opener,
            )

        self.assertFalse(raised.exception.retryable)

    def test_retry_budget_covers_incomplete_chunked_read(self) -> None:
        cue = Cue(1, 0, 1000, "Hello", source_language="en")
        batch = build_translation_batches([cue], source_language="en", target_language="zh-CN")[0]
        attempts = []

        class Response:
            def __init__(self, body: str | None) -> None:
                self._body = body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                if self._body is None:
                    raise IncompleteRead(b"")
                return self._body.encode("utf-8")

        def opener(_request, **_kwargs):
            attempts.append(len(attempts) + 1)
            if len(attempts) < 3:
                return Response(None)
            payload = {
                "choices": [
                    {"message": {"content": json.dumps({"items": [{"id": 1, "text": "你好"}]})}}
                ]
            }
            return Response(json.dumps(payload))

        retries = []

        with (
            patch("xvt.pipeline.translation.time.sleep", lambda delay: retries.append(delay)),
            patch("xvt.pipeline.translation._backoff_delay", lambda base: float(base)),
        ):
            result = translate_batch_with_retries(
                batch,
                CompatibleProviderConfig("https://example.com/v1", "model", "secret"),
                max_retries=4,
                request=lambda batch_arg, config, correction=None: translate_batch(
                    batch_arg, config, opener=opener
                ),
            )

        self.assertEqual(result, {1: "你好"})
        self.assertEqual(len(attempts), 3)
        self.assertEqual(retries, [1, 2])

    def test_provider_classifies_tls_request_termination_as_retryable(self) -> None:
        # urlopen 在请求阶段把底层 SSLError 包装成 URLError，因此真实路径下 provider 收到的是
        # URLError(reason=SSLError 子类)，而不是裸的 SSLError。
        cue = Cue(1, 0, 1000, "Hello", source_language="en")
        batch = build_translation_batches([cue], source_language="en", target_language="zh-CN")[0]

        def opener(_request, **_kwargs):
            raise URLError(SSLZeroReturnError("tls connection closed"))

        with self.assertRaises(TranslationProviderError) as raised:
            translate_batch(
                batch,
                CompatibleProviderConfig("https://example.com/v1", "model", "secret"),
                opener=opener,
            )

        self.assertTrue(raised.exception.retryable)

    def test_provider_classifies_tls_response_termination_as_retryable(self) -> None:
        # 响应读取不经过 urlopen 的 URLError 包装，SSLError 原样到达提供方边界。
        cue = Cue(1, 0, 1000, "Hello", source_language="en")
        batch = build_translation_batches([cue], source_language="en", target_language="zh-CN")[0]

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                raise SSLEOFError("EOF occurred in violation of protocol")

        with self.assertRaises(TranslationProviderError) as raised:
            translate_batch(
                batch,
                CompatibleProviderConfig("https://example.com/v1", "model", "secret"),
                opener=lambda *_args, **_kwargs: Response(),
            )

        self.assertTrue(raised.exception.retryable)

    def test_provider_does_not_retry_tls_certificate_errors(self) -> None:
        # 证书校验发生在握手阶段，真实路径下必然经 URLError 包装抵达；直接断言裸异常会
        # 绕过该包装层，让不可重试分类看起来成立而实际失效。
        cue = Cue(1, 0, 1000, "Hello", source_language="en")
        batch = build_translation_batches([cue], source_language="en", target_language="zh-CN")[0]

        def opener(_request, **_kwargs):
            raise URLError(SSLCertVerificationError("certificate verify failed"))

        with self.assertRaises(TranslationProviderError) as raised:
            translate_batch(
                batch,
                CompatibleProviderConfig("https://example.com/v1", "model", "secret"),
                opener=opener,
            )

        # SSLCertVerificationError 同时继承 ValueError，必须保持依赖故障分类而非用户输入错误。
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(raised.exception.category, "dependency_or_process")

    def test_provider_retries_non_tls_url_errors(self) -> None:
        # 按 reason 细分 TLS 后，普通网络故障必须保持既有的无条件可重试语义。
        cue = Cue(1, 0, 1000, "Hello", source_language="en")
        batch = build_translation_batches([cue], source_language="en", target_language="zh-CN")[0]

        for reason in (ConnectionRefusedError("connection refused"), "name resolution failed"):
            with self.subTest(reason=type(reason).__name__):

                def opener(_request, _reason=reason, **_kwargs):
                    raise URLError(_reason)

                with self.assertRaises(TranslationProviderError) as raised:
                    translate_batch(
                        batch,
                        CompatibleProviderConfig("https://example.com/v1", "model", "secret"),
                        opener=opener,
                    )

                self.assertTrue(raised.exception.retryable)

    def test_provider_does_not_retry_non_connection_os_errors(self) -> None:
        cue = Cue(1, 0, 1000, "Hello", source_language="en")
        batch = build_translation_batches([cue], source_language="en", target_language="zh-CN")[0]

        def opener(_request, **_kwargs):
            raise PermissionError("local permission denied")

        with self.assertRaises(TranslationProviderError) as raised:
            translate_batch(
                batch,
                CompatibleProviderConfig("https://example.com/v1", "model", "secret"),
                opener=opener,
            )

        self.assertFalse(raised.exception.retryable)

    def test_pipeline_retries_rate_limit_without_correction(self) -> None:
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
                    raise TranslationProviderError(
                        "rate limited", retryable=True, retry_after=86400
                    )
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

        sleep.assert_called_once_with(30)
        self.assertEqual(corrections, [None, None])
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

            def first_attempt(batch, _config, *, correction=None):
                self.assertIsNone(correction)
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

            def second_attempt(batch, _config, *, correction=None):
                self.assertIsNone(correction)
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

    def test_translation_discards_progress_when_policy_or_grouping_changes(self) -> None:
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

            def translate(batch, _config, *, correction=None):
                self.assertIsNone(correction)
                calls.append((batch.target_language, batch.policy.style))
                return {1: "你好"}

            with (
                patch.dict(os.environ, environment, clear=True),
                patch("xvt.pipeline.translation.translate_batch", side_effect=translate),
            ):
                run_translation(job_file, target_language="zh-CN")
                run_translation(
                    job_file,
                    target_language="zh-CN",
                    translation_style="natural",
                )
                with patch("xvt.pipeline.translation.SEMANTIC_GROUPING_VERSION", 999):
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
            [
                ("zh-CN", "faithful"),
                ("zh-CN", "natural"),
                ("zh-CN", "natural"),
            ],
        )
        self.assertIn("fingerprint", progress)
        self.assertEqual(progress["items"], [{"id": 1, "text": "你好"}])

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from xvt.errors import DependencyError, UserInputError
from xvt.translate import TranslationBatch, parse_translation_response

TRANSLATION_PROMPT_VERSION = 2
RETRYABLE_HTTP_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class TranslationProviderError(DependencyError):
    """翻译提供方传输层故障（HTTP 状态、网络、超时），按 retryable 决定原样退避重试。"""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class ProviderResponseError(DependencyError):
    """翻译提供方返回了结构性无效的响应（缺 items、报文不完整、内容非文本）。

    与传输层故障不同：原样重发大概率得到同样的畸形输出，这类错误应进入
    携带纠正提示词的纠错重试分支；重试耗尽后仍按依赖故障分类（退出码 3）。
    """


@dataclass(frozen=True, slots=True)
class CompatibleProviderConfig:
    base_url: str
    model: str
    api_key: str
    timeout_seconds: float = 60


def resolve_provider(
    provider: str,
    *,
    base_url: str | None,
    model: str | None,
    api_key_env: str,
    timeout_seconds: float = 60,
) -> tuple[str, CompatibleProviderConfig | None, str | None]:
    if provider == "agent":
        return "agent", None, None

    resolved_base_url = base_url or os.environ.get("XVT_TRANSLATION_BASE_URL")
    resolved_model = model or os.environ.get("XVT_TRANSLATION_MODEL")
    api_key = os.environ.get(api_key_env)
    missing = [
        name
        for name, value in (
            ("base URL", resolved_base_url),
            ("model", resolved_model),
            (f"API key environment variable {api_key_env}", api_key),
        )
        if not value
    ]
    if missing:
        detail = ", ".join(missing)
        if provider == "auto":
            return "agent", None, f"missing {detail}"
        raise UserInputError(f"openai-compatible provider is missing {detail}")

    return (
        "openai-compatible",
        CompatibleProviderConfig(
            resolved_base_url.rstrip("/"),
            resolved_model,
            api_key,
            timeout_seconds,
        ),
        None,
    )


def _response_items(content: str) -> list[dict[str, Any]]:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1])
    data = json.loads(text)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ProviderResponseError("translation provider response must contain an items array")
    return items


def translate_batch(
    batch: TranslationBatch,
    config: CompatibleProviderConfig,
    *,
    opener=None,
    correction: str | None = None,
) -> dict[int, str]:
    style_instruction = {
        "faithful": "Preserve meaning, tone, and formality faithfully.",
        "natural": "Use idiomatic target-language phrasing without adding or omitting meaning.",
        "concise": "Keep the core meaning while removing only non-semantic repetition when needed.",
    }[batch.policy.style]
    system_content = (
        f"{style_instruction} Preserve every integer id, "
        "respect max_target_characters, use neighboring context only for meaning, and "
        'return JSON only in the form {"items":[{"id":1,"text":"..."}]}.'
    )
    if batch.policy.terms:
        system_content = (
            f"{system_content} When a glossary source term appears in an item, use its exact "
            "required target term."
        )
    if correction:
        system_content = f"{system_content} {correction}"
    payload = {
        "model": config.model,
        "messages": [
            {
                "role": "system",
                "content": system_content,
            },
            {
                "role": "user",
                "content": json.dumps(batch.to_dict(), ensure_ascii=False),
            },
        ],
    }
    request = Request(
        f"{config.base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        response = (opener or urlopen)(request, timeout=config.timeout_seconds)
        with response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        retry_after = None
        if error.headers:
            value = error.headers.get("Retry-After")
            try:
                retry_after = float(value) if value is not None else None
            except ValueError:
                retry_after = None
        raise TranslationProviderError(
            f"translation provider returned HTTP {error.code}",
            retryable=error.code in RETRYABLE_HTTP_STATUS,
            retry_after=retry_after,
        ) from error
    except URLError as error:
        raise TranslationProviderError(
            f"translation provider request failed: {error.reason}",
            retryable=True,
        ) from error
    except TimeoutError as error:
        raise TranslationProviderError(
            "translation provider request timed out",
            retryable=True,
        ) from error

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise ProviderResponseError(
            "translation provider returned an incompatible response"
        ) from error
    if not isinstance(content, str):
        raise ProviderResponseError("translation provider response content must be text")
    return parse_translation_response(batch, _response_items(content))

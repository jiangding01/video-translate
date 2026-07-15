from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_REDACTED = "<redacted>"
_URL_PATTERN = re.compile(r"https?://[^\s\"']+")
_SENSITIVE_QUERY_KEYS = {
    "access_token",
    "api_key",
    "auth",
    "authorization",
    "key",
    "signature",
    "sig",
    "token",
    "x-amz-credential",
    "x-amz-security-token",
    "x-amz-signature",
}


@dataclass(frozen=True, slots=True)
class Event:
    type: str
    timestamp: str
    data: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


EventSink = Callable[[Event], None]


def _redact_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return value
    netloc = (
        f"{_REDACTED}@{parsed.netloc.rsplit('@', 1)[1]}"
        if parsed.username is not None
        else parsed.netloc
    )
    query = urlencode(
        [
            (key, _REDACTED if key.lower() in _SENSITIVE_QUERY_KEYS else query_value)
            for key, query_value in parse_qsl(parsed.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))


def _redact_event_value(value: Any) -> Any:
    if isinstance(value, str):
        return _URL_PATTERN.sub(lambda match: _redact_url(match.group()), value)
    if isinstance(value, list):
        return [_redact_event_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_event_value(item) for item in value)
    if isinstance(value, dict):
        return {key: _redact_event_value(item) for key, item in value.items()}
    return value


def create_event(event_type: str, **data: Any) -> Event:
    return Event(
        type=event_type,
        timestamp=datetime.now(UTC).isoformat(),
        data=_redact_event_value(data),
    )

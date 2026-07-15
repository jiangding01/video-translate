from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xvt.process import CommandResult, run_command

CommandRunner = Callable[..., CommandResult]


@dataclass(frozen=True, slots=True)
class MediaInfo:
    path: str
    duration_ms: int
    size_bytes: int
    format_name: str
    width: int | None
    height: int | None
    frame_rate: float | None
    video_codec: str | None
    audio_codec: str | None
    has_video: bool
    has_audio: bool


def parse_frame_rate(value: str | None) -> float | None:
    if not value or value == "0/0":
        return None
    numerator, separator, denominator = value.partition("/")
    if not separator:
        return float(value)
    if float(denominator) == 0:
        return None
    return float(numerator) / float(denominator)


def parse_probe_output(path: Path, data: dict[str, Any]) -> MediaInfo:
    streams = data.get("streams", [])
    format_data = data.get("format", {})
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    duration = float(format_data.get("duration") or 0)
    size = int(format_data.get("size") or path.stat().st_size)

    return MediaInfo(
        path=str(path),
        duration_ms=round(duration * 1000),
        size_bytes=size,
        format_name=str(format_data.get("format_name") or ""),
        width=int(video["width"]) if video and video.get("width") is not None else None,
        height=int(video["height"]) if video and video.get("height") is not None else None,
        frame_rate=parse_frame_rate(video.get("avg_frame_rate")) if video else None,
        video_codec=video.get("codec_name") if video else None,
        audio_codec=audio.get("codec_name") if audio else None,
        has_video=video is not None,
        has_audio=audio is not None,
    )


def probe_media(path: str | Path, runner: CommandRunner = run_command) -> MediaInfo:
    media_path = Path(path).expanduser()
    if not media_path.is_file():
        raise ValueError(f"media file does not exist: {media_path}")
    media_path = media_path.resolve()
    result = runner(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(media_path),
        ]
    )
    data = json.loads(result.stdout)
    if not isinstance(data, dict):
        raise ValueError("ffprobe output must be a JSON object")
    return parse_probe_output(media_path, data)

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from xvt.events import EventSink, EventType, create_event
from xvt.process import CommandError, CommandResult, run_command

CommandRunner = Callable[..., CommandResult]

HARDWARE_ENCODER_ERRORS = (
    "cannot create compression session",
    "error while opening encoder",
    "hardware encoder may be busy",
    "qscale not available for encoder",
    "videotoolbox session",
)


# FFmpeg filtergraph 描述是两层解析：先按 filtergraph 语法切分链路与 filter，再由
# 每个 filter 解析自己的选项值。路径必须逐层转义，且顺序为「先选项值层、后 filtergraph
# 层」，否则第一层加出来的反斜杠会被第二层重新吃掉。
# 注意：不要退回 `ass=filename='...'` 的单引号包裹写法——包裹内单引号无法表示
# （shell 风格的 `'\''` 在 filtergraph 里不成立），含单引号的路径必定烧录失败。
_FILTER_OPTION_SPECIALS = frozenset("\\':")
_FILTERGRAPH_SPECIALS = frozenset("\\',;[]")


def _escape_characters(value: str, specials: frozenset[str]) -> str:
    return "".join(f"\\{character}" if character in specials else character for character in value)


def escape_filter_path(path: Path) -> str:
    """把路径转义成可直接嵌入 filtergraph 的裸值（不加引号包裹）。"""

    option_value = _escape_characters(str(path), _FILTER_OPTION_SPECIALS)
    return _escape_characters(option_value, _FILTERGRAPH_SPECIALS)


def recommended_video_bitrate(video_height: int) -> str:
    if video_height <= 480:
        return "2M"
    if video_height <= 720:
        return "3M"
    if video_height <= 1080:
        return "5M"
    if video_height <= 1440:
        return "9M"
    return "16M"


def _is_hardware_encoder_error(error: CommandError) -> bool:
    detail = f"{error.result.stderr}\n{error.result.stdout}".casefold()
    return any(marker in detail for marker in HARDWARE_ENCODER_ERRORS)


def build_burn_command(
    media_path: Path,
    subtitle_path: Path,
    output_path: Path,
    *,
    hardware_encoding: bool = True,
    video_bitrate: str = "5M",
    progress: bool = False,
    start_ms: int | None = None,
    clip_duration_ms: int | None = None,
) -> list[str]:
    video_codec = "h264_videotoolbox" if hardware_encoding else "libx264"
    subtitle_filter = f"ass=filename={escape_filter_path(subtitle_path)}"
    command = ["ffmpeg", "-y"]
    if start_ms is not None:
        command.extend(["-ss", f"{start_ms / 1000:.3f}"])
    command.extend(
        [
            "-i",
            str(media_path),
        ]
    )
    if clip_duration_ms is not None:
        command.extend(["-t", f"{clip_duration_ms / 1000:.3f}"])
    command.extend(
        [
            "-vf",
            subtitle_filter,
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            video_codec,
        ]
    )
    if hardware_encoding:
        command.extend(["-b:v", video_bitrate])
    else:
        command.extend(["-preset", "medium", "-crf", "18"])
    command.extend(["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"])
    if progress:
        command.extend(["-progress", "pipe:1", "-nostats"])
    command.append(str(output_path))
    return command


def burn_subtitles(
    media: str | Path,
    subtitles: str | Path,
    output: str | Path,
    *,
    hardware_encoding: bool = True,
    video_bitrate: str = "5M",
    duration_ms: int | None = None,
    start_ms: int | None = None,
    clip_duration_ms: int | None = None,
    progress_event_type: EventType = EventType.BURN_PROGRESS,
    event_sink: EventSink | None = None,
    runner: CommandRunner = run_command,
) -> Path:
    media_path = Path(media).expanduser()
    subtitle_path = Path(subtitles).expanduser()
    output_path = Path(output).expanduser()
    if not media_path.is_file():
        raise ValueError(f"media file does not exist: {media_path}")
    if not subtitle_path.is_file():
        raise ValueError(f"subtitle file does not exist: {subtitle_path}")
    if not output_path.is_absolute():
        raise ValueError("output path must be absolute")
    if start_ms is not None and start_ms < 0:
        raise ValueError("start_ms must not be negative")
    if clip_duration_ms is not None and clip_duration_ms <= 0:
        raise ValueError("clip_duration_ms must be positive")

    media_path = media_path.resolve()
    subtitle_path = subtitle_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    runner_kwargs = {}
    if event_sink:
        progress_state: dict[str, str] = {}

        def handle_line(_stream: str, line: str) -> None:
            key, separator, value = line.partition("=")
            if not separator:
                return
            progress_state[key] = value
            if key != "progress":
                return
            try:
                out_time_ms = (
                    int(progress_state.get("out_time_us") or progress_state.get("out_time_ms") or 0)
                    // 1000
                )
            except ValueError:
                out_time_ms = 0
            event_sink(
                create_event(
                    progress_event_type,
                    out_time_ms=out_time_ms,
                    duration_ms=duration_ms,
                    percent=(
                        round(min(out_time_ms / duration_ms * 100, 100), 2) if duration_ms else None
                    ),
                    frame=progress_state.get("frame"),
                    speed=progress_state.get("speed"),
                    state=value,
                )
            )

        runner_kwargs["line_callback"] = handle_line
    try:
        runner(
            build_burn_command(
                media_path,
                subtitle_path,
                output_path,
                hardware_encoding=hardware_encoding,
                video_bitrate=video_bitrate,
                progress=event_sink is not None,
                start_ms=start_ms,
                clip_duration_ms=clip_duration_ms,
            ),
            **runner_kwargs,
        )
    except CommandError as error:
        if not hardware_encoding or not _is_hardware_encoder_error(error):
            raise
        runner(
            build_burn_command(
                media_path,
                subtitle_path,
                output_path,
                hardware_encoding=False,
                video_bitrate=video_bitrate,
                progress=event_sink is not None,
                start_ms=start_ms,
                clip_duration_ms=clip_duration_ms,
            ),
            **runner_kwargs,
        )
    return output_path

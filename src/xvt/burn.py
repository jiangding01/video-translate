from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

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


# ffmpeg 的 `-progress` 块在「这一帧还没输出完」时把 out_time / speed / bitrate 全写成
# 字面量 `N/A`（实测：3840x2160 + `-preset placebo` 的首帧必现）。这两个 helper 是它的
# 唯一归一处——归一放在**事件边界**，不放渲染层：事件是协议，让它带着 `"N/A"` 出门，
# 每个消费方都得自己认一遍这个字面量。
_FFMPEG_PROGRESS_UNAVAILABLE = "N/A"


def _progress_field(value: str | None) -> str | None:
    """ffmpeg 进度字段的展示值；`N/A` 与空串都归一成 ``None``。"""

    if value is None:
        return None
    stripped = value.strip()
    if not stripped or stripped == _FFMPEG_PROGRESS_UNAVAILABLE:
        return None
    return stripped


def _parse_out_time_ms(progress_state: dict[str, str]) -> int | None:
    """从进度块解析已输出时长（毫秒）；解析不出来返回 ``None`` 而不是 0。

    两个键都是**微秒**——`out_time_ms` 这个名字是 ffmpeg 的历史遗留，它装的也是微秒，
    所以两者同样 ``// 1000``。
    """

    for key in ("out_time_us", "out_time_ms"):
        raw = _progress_field(progress_state.get(key))
        if raw is None:
            continue
        try:
            return int(raw) // 1000
        except ValueError:
            continue
    return None


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

    def progress_kwargs() -> dict[str, Any]:
        """每次 ``runner`` 调用都新建一份进度回调与它的状态。

        **不能跨调用共用**：硬件编码失败会回退软件编码再跑一次（见下面两个 ``runner``），
        而「N/A 块沿用上一个已知 out_time」要的是*本次*编码的进度。共用一份状态时，回退
        那次开头的 N/A 块会沿用到硬件那次的 99%，于是显示 99% 而实际从零重编——比原来
        显示 0% 更能骗人。
        """

        if event_sink is None:
            return {}
        progress_state: dict[str, str] = {}
        last_out_time_ms: int | None = None

        def handle_line(_stream: str, line: str) -> None:
            nonlocal last_out_time_ms
            key, separator, value = line.partition("=")
            if not separator:
                return
            progress_state[key] = value
            if key != "progress":
                return
            parsed = _parse_out_time_ms(progress_state)
            if parsed is not None:
                last_out_time_ms = parsed
            # 解析不出来时沿用本次编码上一个已知值，一次都没拿到过就发 None。
            # **绝不兜底成 0**：ffmpeg 每 0.5 秒定时发一块，这一帧还没输出完的那块里
            # out_time_us / out_time_ms / speed / bitrate 全是字面量 `N/A`（2160p +
            # libass 的首帧实测会命中），兜底成 0 就让进度条当场倒退回 0%。
            out_time_ms = last_out_time_ms
            event_sink(
                create_event(
                    progress_event_type,
                    out_time_ms=out_time_ms,
                    duration_ms=duration_ms,
                    percent=(
                        round(min(out_time_ms / duration_ms * 100, 100), 2)
                        if duration_ms and out_time_ms is not None
                        else None
                    ),
                    frame=_progress_field(progress_state.get("frame")),
                    speed=_progress_field(progress_state.get("speed")),
                    state=value,
                )
            )

        return {"line_callback": handle_line}

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
            **progress_kwargs(),
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
            **progress_kwargs(),
        )
    return output_path

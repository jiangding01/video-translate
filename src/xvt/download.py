from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlparse

from xvt.errors import DependencyError
from xvt.events import EventSink, EventType, create_event
from xvt.process import CommandError, CommandResult, run_command

CommandRunner = Callable[..., CommandResult]

# 视频格式回退链：优先分离流合并，再试渐进式 360p(18)，最后任意 best。
VIDEO_FORMAT_SELECTOR = "bv*+ba/b/18/best"


@dataclass(frozen=True, slots=True)
class RemoteMediaInfo:
    id: str
    title: str
    duration_ms: int
    webpage_url: str
    subtitle_languages: tuple[str, ...]
    automatic_subtitle_languages: tuple[str, ...]
    subtitles_anonymous: bool = False


def validate_remote_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("input must be an HTTP or HTTPS URL")
    return url


def parse_remote_info(data: dict[str, Any]) -> RemoteMediaInfo:
    return RemoteMediaInfo(
        id=str(data.get("id") or ""),
        title=str(data.get("title") or ""),
        duration_ms=round(float(data.get("duration") or 0) * 1000),
        webpage_url=str(data.get("webpage_url") or data.get("original_url") or ""),
        subtitle_languages=tuple(sorted((data.get("subtitles") or {}).keys())),
        automatic_subtitle_languages=tuple(sorted((data.get("automatic_captions") or {}).keys())),
    )


def _command_output_detail(error: CommandError) -> str:
    return error.result.stderr.strip() or error.result.stdout.strip() or str(error)


def _looks_like_http_403(text: str) -> bool:
    lowered = text.casefold()
    return (
        "http error 403" in lowered
        or "error code: 403" in lowered
        or " 403: forbidden" in lowered
        or "status code 403" in lowered
    )


def _reraise_media_download_failure(error: CommandError) -> NoReturn:
    """将 yt-dlp 媒体下载失败转为更可操作的错误；非 403 原样抛出。"""

    detail = _command_output_detail(error)
    if _looks_like_http_403(detail):
        raise DependencyError(
            "yt-dlp 下载失败（HTTP 403）。可对同一 Job 运行 xvt continue 重试；"
            "仍失败时请更新 yt-dlp，或显式传入 --cookies-from-browser。"
            f" 原始错误：{detail}"
        ) from error
    raise error


def inspect_remote(
    url: str,
    runner: CommandRunner = run_command,
    *,
    cookies_from_browser: str | None = None,
    proxy: str | None = None,
    anonymous: bool = False,
) -> RemoteMediaInfo:
    validated_url = validate_remote_url(url)
    command = [
        "yt-dlp",
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
    ]
    if anonymous:
        command.append("--ignore-config")
    elif cookies_from_browser:
        command.extend(["--cookies-from-browser", cookies_from_browser])
    if proxy:
        command.extend(["--proxy", proxy])
    command.append(validated_url)
    result = runner(command)
    data = json.loads(result.stdout)
    if not isinstance(data, dict):
        raise ValueError("yt-dlp output must be a JSON object")
    return parse_remote_info(data)


def select_subtitle_language(requested: str, available: tuple[str, ...]) -> str | None:
    normalized = requested.casefold()
    exact = next((language for language in available if language.casefold() == normalized), None)
    if exact:
        return exact
    base = normalized.split("-", 1)[0]
    return next(
        (language for language in available if language.casefold().split("-", 1)[0] == base),
        None,
    )


def _preferred_subtitle(candidates: list[Path], language: str) -> Path:
    """Pick the subtitle file matching the requested language.

    yt-dlp writes the language into the output template, so a job directory can
    hold ``source.en.srt``, ``source.en-orig.srt`` and ``source.zh-Hans.srt`` at
    once. Prefer an exact ``source.<language>.srt`` match, then any
    ``source.<language>-*`` / ``source.<language>.*`` variant, and only fall back
    to the first sorted candidate when nothing carries the requested language.
    """

    prefix = f"source.{language.casefold()}"
    exact = [item for item in candidates if item.name.casefold() == f"{prefix}.srt"]
    if exact:
        return exact[0]
    variants = [
        item for item in candidates if item.name.casefold().startswith((f"{prefix}.", f"{prefix}-"))
    ]
    if variants:
        return variants[0]
    return candidates[0]


def download_remote_subtitles(
    url: str,
    output_directory: str | Path,
    *,
    language: str,
    automatic: bool = False,
    cookies_from_browser: str | None = None,
    proxy: str | None = None,
    anonymous: bool = False,
    runner: CommandRunner = run_command,
) -> Path:
    validated_url = validate_remote_url(url)
    root = Path(output_directory).expanduser()
    if not root.is_absolute():
        raise ValueError("subtitle download output directory must be absolute")
    root.mkdir(parents=True, exist_ok=True)
    command = [
        "yt-dlp",
        "--no-playlist",
        "--skip-download",
        "--no-progress",
        "--no-warnings",
        "--write-auto-subs" if automatic else "--write-subs",
        "--sub-langs",
        language,
        "--sub-format",
        "srt/vtt/best",
        "--convert-subs",
        "srt",
        "-o",
        str(root / "source.%(ext)s"),
    ]
    if anonymous:
        command.append("--ignore-config")
    elif cookies_from_browser:
        command.extend(["--cookies-from-browser", cookies_from_browser])
    if proxy:
        command.extend(["--proxy", proxy])
    command.append(validated_url)
    runner(command)

    candidates = sorted(root.glob("source*.srt"))
    if not candidates:
        raise RuntimeError("yt-dlp completed without producing a subtitle file")
    subtitle_path = _preferred_subtitle(candidates, language).resolve()
    try:
        subtitle_path.relative_to(root.resolve())
    except ValueError as error:
        raise RuntimeError("yt-dlp produced subtitles outside the job directory") from error
    return subtitle_path


def download_remote_media(
    url: str,
    output_directory: str | Path,
    *,
    audio_only: bool = False,
    cookies_from_browser: str | None = None,
    proxy: str | None = None,
    event_sink: EventSink | None = None,
    runner: CommandRunner = run_command,
) -> Path:
    validated_url = validate_remote_url(url)
    root = Path(output_directory).expanduser()
    if not root.is_absolute():
        raise ValueError("download output directory must be absolute")
    root.mkdir(parents=True, exist_ok=True)

    command = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--print",
        "after_move:filepath",
        "-o",
        str(root / "source.%(ext)s"),
    ]
    if event_sink:
        command.extend(
            [
                "--newline",
                "--progress",
                "--progress-template",
                "download:XVT_PROGRESS:%(progress)j",
            ]
        )
    else:
        command.append("--no-progress")
    if audio_only:
        command.extend(["-f", "bestaudio/best"])
    else:
        command.extend(["-f", VIDEO_FORMAT_SELECTOR, "--merge-output-format", "mp4"])
    if cookies_from_browser:
        command.extend(["--cookies-from-browser", cookies_from_browser])
    if proxy:
        command.extend(["--proxy", proxy])
    command.append(validated_url)

    runner_kwargs = {}
    if event_sink:

        def handle_line(_stream: str, line: str) -> None:
            if not line.startswith("XVT_PROGRESS:"):
                return
            try:
                progress = json.loads(line.removeprefix("XVT_PROGRESS:"))
            except json.JSONDecodeError:
                return
            try:
                downloaded = int(float(progress.get("downloaded_bytes") or 0))
            except (TypeError, ValueError):
                downloaded = 0
            try:
                total = int(
                    float(progress.get("total_bytes") or progress.get("total_bytes_estimate") or 0)
                )
            except (TypeError, ValueError):
                total = 0
            event_sink(
                create_event(
                    EventType.DOWNLOAD_PROGRESS,
                    downloaded_bytes=downloaded,
                    total_bytes=total or None,
                    percent=round(downloaded / total * 100, 2) if total else None,
                    speed=progress.get("speed"),
                    eta_seconds=progress.get("eta"),
                )
            )

        runner_kwargs["line_callback"] = handle_line
    try:
        result = runner(command, **runner_kwargs)
    except CommandError as error:
        _reraise_media_download_failure(error)
    output_lines = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip() and not line.startswith("XVT_PROGRESS:")
    ]
    if not output_lines:
        raise RuntimeError("yt-dlp completed without reporting an output file")
    downloaded = Path(output_lines[-1]).expanduser().resolve()
    try:
        downloaded.relative_to(root.resolve())
    except ValueError as error:
        raise RuntimeError("yt-dlp reported a file outside the job directory") from error
    if not downloaded.is_file():
        raise RuntimeError(f"yt-dlp output file does not exist: {downloaded}")
    return downloaded

from __future__ import annotations

import json
import math
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import parse_qs, urlparse

from xvt.checksums import file_checksum
from xvt.errors import DependencyError
from xvt.events import EventSink, EventType, create_event
from xvt.media import probe_media
from xvt.process import CommandError, CommandResult, run_command

CommandRunner = Callable[..., CommandResult]

VIDEO_FORMAT_SELECTOR = "bv*+ba/b"

# 探测都是纯元数据查询，秒级返回；给足余量后仍不返回按环境异常处理，不让一次探测
# 把整条下载挂住。画质比较与容量预检两处共用这一个上限。
_PROBE_TIMEOUT_SECONDS = 60


def _source_output_template(root: Path) -> str:
    """把 Job 目录拼成 yt-dlp 的 ``-o`` 值，目录部分里的字面量 ``%`` 一律转义成 ``%%``。

    yt-dlp 对 ``-o`` 的值整体套用输出模板，**目录部分不例外**。实测
    （yt-dlp 2026.08.19，离线 ``--load-info-json ... --skip-download --print filename``）：

    * ``-o '/Users/x/My %(title)s videos/source.%(ext)s'``
      → ``/Users/x/My PWNED TITLE videos/source.mp4``
    * ``-o '/Users/x/%(id)s/source.%(ext)s'`` → ``/Users/x/abc123/source.mp4``
    * ``-o '/Users/x/100%% done/source.%(ext)s'`` → ``/Users/x/100% done/source.mp4``

    也就是说 ``--output`` 落在名字带 ``%(...)s`` 的目录下时，产物会被写到 Job 目录之外
    （下载完成后的越界检查只报错，不会把写出去的文件收回来）。只转义 root，
    ``source.%(ext)s`` 是真模板，必须原样留给 yt-dlp。
    """

    return str(Path(str(root).replace("%", "%%")) / "source.%(ext)s")


@dataclass(frozen=True, slots=True)
class VideoSelection:
    video_id: str
    format_id: str
    width: int
    height: int
    duration_ms: int
    has_audio: bool
    language: str | None
    original_audio: bool
    incomplete: bool
    # 实际要下载的各 format 登记大小之和；拿不到就是 None（不猜）。
    size_bytes: int | None = None
    # 实际要下载的 format 数：>1 表示 video / audio 分离、下载后要 mux。
    part_count: int = 1


@dataclass(frozen=True, slots=True)
class _DownloadSize:
    """容量预检要的三个值，与 ``VideoSelection`` 解耦。

    无 Cookie / 非 YouTube 的下载路径没有 ``VideoSelection``（它是 YouTube 画质比较的
    产物），所以预检吃这个轻量结构，两条路径各自构造。
    """

    size_bytes: int | None
    part_count: int
    format_id: str


def _requested_size_bytes(parts: list[dict[str, Any]]) -> int | None:
    """实际要下载的各 format 的登记大小之和；任一 part 拿不到大小就返回 ``None``。

    **按 ``parts`` 求和，不能写 ``video_size + audio_size``**：单文件格式（选择器
    回落到 ``b``）下 ``parts`` 只有一个元素，而那个 dict 同时带 ``vcodec`` 与
    ``acodec``，``_probe_video`` 里的 ``video`` 与 ``audio`` 指向**同一份** dict，
    两侧相加会把大小翻一倍，于是空间充足也会被预检误拦。

    ``filesize`` 优先于 ``filesize_approx``。拿不到大小不等于「大小是 0」，所以
    返回 ``None`` 让调用方跳过预检——这条判定只做证伪，不猜。
    """

    total = 0
    for part in parts:
        size = part.get("filesize") or part.get("filesize_approx")
        if type(size) not in {int, float} or not math.isfinite(size) or size <= 0:
            return None
        total += int(size)
    return total or None


def _probe_video(
    url: str, cookies: str | None, proxy: str | None, runner: CommandRunner
) -> VideoSelection:
    command = [
        "yt-dlp",
        "--skip-download",
        "--no-playlist",
        "--no-progress",
        "--verbose",
        "--dump-single-json",
        "-f",
        VIDEO_FORMAT_SELECTOR,
        "--format-sort-force",
        "-S",
        "lang,res",
    ]
    if cookies:
        command.extend(["--cookies-from-browser", cookies])
    else:
        command.append("--ignore-config")
    if proxy:
        command.extend(["--proxy", proxy])
    command.append(url)
    try:
        result = runner(command, timeout=_PROBE_TIMEOUT_SECONDS)
    except CommandError as error:
        raise DependencyError("视频画质查询失败；请对同一 Job 重试，或提供本地视频。") from error
    try:
        data = json.loads(result.stdout)
        parts = data.get("requested_formats") or [data]
        video = next(p for p in parts if p.get("vcodec") not in {None, "none"})
        audio = next((p for p in parts if p.get("acodec") not in {None, "none"}), None)
        video_id = data["id"]
        format_id = data["format_id"]
        width, height = video["width"], video["height"]
        duration = data["duration"]
        if (
            not isinstance(video_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", video_id)
            or not isinstance(format_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_.+-]+", format_id)
            or type(width) is not int
            or type(height) is not int
            or min(width, height) <= 0
            or type(duration) not in {int, float}
            or not math.isfinite(duration)
            or duration <= 0
            or data.get("is_live")
            or data.get("live_status") in {"is_live", "post_live"}
        ):
            raise ValueError("invalid video metadata")
        language = audio.get("language") if audio else None
        if language is not None and (
            not isinstance(language, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", language)
        ):
            raise ValueError("invalid audio language")
    except (ValueError, TypeError, KeyError, StopIteration, AttributeError) as error:
        raise DependencyError("视频画质查询返回不完整或无效的格式信息。") from error
    diagnostics = "\n".join(
        line.casefold()
        for line in result.stderr.splitlines()
        if line.startswith(("WARNING:", "ERROR:"))
    )
    incomplete = any(
        marker in diagnostics
        for marker in (
            "formats have been skipped",
            "formats may be missing",
            "challenge solving failed",
            "signature extraction failed",
            "nsig extraction failed",
            "formats require a gvs po token",
        )
    )
    return VideoSelection(
        video_id,
        format_id,
        width,
        height,
        round(duration * 1000),
        audio is not None,
        language,
        # YouTube's extractor marks original audio with preference 10 (default/dubbed is lower).
        audio is not None and audio.get("language_preference") == 10,
        incomplete,
        _requested_size_bytes(parts),
        len(parts),
    )


# 分离下载的 video 与 audio 要各自先落盘、再 mux 成 mp4（``--merge-output-format mp4``），
# mux 完才删中间文件，所以峰值占用约是两份；单文件格式无需 mux，峰值就是一份。
# 磁盘只剩 1.5 份时下载不会在传输阶段失败，而是在 mux 阶段失败——所以门槛取两份不是
# 保守过度，而是实际峰值。
_MUXED_PEAK_FACTOR = 2


def _check_download_space(size: _DownloadSize, root: Path, event_sink: EventSink | None) -> None:
    """下载前的容量预检：明显不够才 fail closed，拿不到大小就跳过。

    只做**证伪**。``filesize_approx`` 名字就写着是估算，低估时预检放过一次本该拦的
    下载（退化成预检之前的现状，不产生新的失败模式），所以这里不为估算误差再加固定
    余量——加了就从证伪滑向猜。

    `root` 与 yt-dlp 真正写入的临时目录同在一个文件系统（``tempfile.TemporaryDirectory``
    带 ``dir=root``），发布用的是 ``os.link`` 硬链接、不额外占空间，所以量 `root` 的
    可用空间就是量实际要用的那块盘。
    """

    if size.size_bytes is None:
        return
    required = size.size_bytes * (_MUXED_PEAK_FACTOR if size.part_count > 1 else 1)
    available = shutil.disk_usage(root).free
    if available >= required:
        return
    if event_sink:
        event_sink(
            create_event(
                EventType.DOWNLOAD_SPACE_INSUFFICIENT,
                required_bytes=required,
                available_bytes=available,
                format_id=size.format_id,
                part_count=size.part_count,
            )
        )
    raise DependencyError(
        f"下载目标磁盘空间不足：预计需要 {required / 2**30:.1f} GiB（含合并峰值），"
        f"当前可用 {available / 2**30:.1f} GiB；请清理磁盘或改用本地媒体入口。"
    )


def _probe_download_size(
    url: str,
    selector: str,
    *,
    cookies: str | None,
    proxy: str | None,
    anonymous: bool,
    runner: CommandRunner,
) -> _DownloadSize:
    """只为容量预检探测所选格式的大小；拿不到一律降级成「不知道」。

    **刻意不复用 ``_probe_video``**：那个为 YouTube 画质比较服务，对 ``id`` / ``format_id``
    / 宽高 / 时长逐项严格校验，缺一项就抛 ``DependencyError``——非 YouTube 站点很可能过不了
    那些校验，而容量预检只要一个大小，不该因为某个站点的元数据形状不同就把整条下载弄挂。
    所以这里捕获一切解析与命令失败，返回 ``size_bytes=None`` 让调用方跳过预检：代价是那次
    探测白花，比让下载失活划算。

    flag 照**下载命令**构造（而不是照 ``_probe_video``：它在无 Cookie 时总加
    ``--ignore-config``，而直接下载路径只在 ``_anonymous`` 时才加）。探测与下载必须看到
    同一组格式，否则量的不是要下的。
    """

    command = [
        "yt-dlp",
        "--skip-download",
        "--no-playlist",
        "--no-progress",
        "--no-warnings",
        "--dump-single-json",
        "-f",
        selector,
    ]
    if anonymous:
        command.append("--ignore-config")
    if cookies:
        command.extend(["--cookies-from-browser", cookies])
    if proxy:
        command.extend(["--proxy", proxy])
    command.append(url)
    unknown = _DownloadSize(None, 1, selector)
    try:
        result = runner(command, timeout=_PROBE_TIMEOUT_SECONDS)
        data = json.loads(result.stdout)
    except (CommandError, OSError, ValueError):
        # 刻意**不**捕 TypeError：``result.stdout`` 必然是 str、非 dict 的分支下面用
        # isinstance 判，所以这里出 TypeError 只会是 runner 的调用约定不对（例如测试桩
        # 不接 ``timeout``）。那是该暴露的 bug，吞掉它会让预检静默失活而测试照旧全绿。
        return unknown
    if not isinstance(data, dict):
        return unknown
    requested = data.get("requested_formats") or [data]
    if not isinstance(requested, list):
        return unknown
    parts = [part for part in requested if isinstance(part, dict)]
    if not parts:
        return unknown
    format_id = data.get("format_id")
    return _DownloadSize(
        _requested_size_bytes(parts),
        len(parts),
        # 空串也退回 selector：事件契约里 format_id 是「所选格式 ID」，不是空值。
        format_id if isinstance(format_id, str) and format_id else selector,
    )


def _download_compared_video(
    url: str,
    root: Path,
    cookies: str,
    proxy: str | None,
    event_sink: EventSink | None,
    runner: CommandRunner,
) -> Path:
    configured = _probe_video(url, cookies, proxy, runner)
    anonymous = _probe_video(url, None, proxy, runner)
    parsed = urlparse(url)
    expected_id = (
        parsed.path.strip("/").split("/")[0]
        if parsed.hostname == "youtu.be"
        else (parse_qs(parsed.query).get("v") or [None])[0]
    )
    if expected_id is None and parsed.path.strip("/").split("/")[0] in {"shorts", "embed", "live"}:
        expected_id = parsed.path.strip("/").split("/")[-1]
    if expected_id and configured.video_id != expected_id:
        raise DependencyError("视频画质查询与请求的视频 ID 不一致。")
    if (
        configured.video_id != anonymous.video_id
        or abs(configured.duration_ms - anonymous.duration_ms) > 500
    ):
        raise DependencyError("匿名画质查询返回不同的视频或时长，拒绝下载。")
    selected = configured
    access = "configured"
    # 画质相同、而只有 Cookie 侧提取不完整时也要换路：两侧画质一致就没有降级风险，
    # 留在脏的一侧只会让整单失败。YouTube 的 SABR 实验按账号开启，Cookie 侧会**一直**
    # 报 `web_embedded ... formats have been skipped`，而匿名侧零警告、选中同一个格式，
    # 于是「严格更高画质才换」会把这类账号的每一个 YouTube 视频都卡死（2026-10-23 实测）。
    # 匿名侧画质更低时仍然不换——那才是 5ae28e8 要防的静默降级。
    clean_anonymous_tie = (
        configured.incomplete
        and not anonymous.incomplete
        and anonymous.original_audio == configured.original_audio
        and min(anonymous.width, anonymous.height) >= min(configured.width, configured.height)
    )
    if (
        anonymous.original_audio
        and not configured.original_audio
        or anonymous.original_audio == configured.original_audio
        and min(anonymous.width, anonymous.height) > min(configured.width, configured.height)
        or clean_anonymous_tie
    ):
        if configured.has_audio and not anonymous.has_audio:
            raise DependencyError("匿名高清视频缺少原视频音轨，拒绝替换。")
        if (
            configured.original_audio
            and anonymous.original_audio
            and configured.language
            and anonymous.language
            and configured.language.split("-", 1)[0].casefold()
            != anonymous.language.split("-", 1)[0].casefold()
        ):
            raise DependencyError("匿名高清视频音轨语言与原访问方式不同，拒绝替换。")
        selected, access = anonymous, "anonymous"
    if selected.incomplete or anonymous.incomplete:
        raise DependencyError("视频格式提取不完整，无法确认最高画质；请重试或提供本地高清视频。")
    # 排在 incomplete 护栏**之后**：提取不完整时无论空间多少都不该下载，既有失败顺序
    # 不变。排在建临时目录**之前**：失败早于下载，不留半个临时目录，一个字节都不下。
    _check_download_space(
        _DownloadSize(selected.size_bytes, selected.part_count, selected.format_id),
        root,
        event_sink,
    )
    with tempfile.TemporaryDirectory(prefix=".xvt-download-", dir=root) as directory:
        temporary = Path(directory)
        # Exact IDs prevent a later extraction from silently choosing a lower format.
        downloaded = download_remote_media(
            url,
            temporary,
            cookies_from_browser=cookies if access == "configured" else None,
            proxy=proxy,
            event_sink=event_sink,
            runner=runner,
            _selection=selected,
            _anonymous=access == "anonymous",
        )
        info = probe_media(downloaded, runner=runner)
        if (
            (info.width, info.height) != (selected.width, selected.height)
            or not info.has_video
            or selected.has_audio
            and not info.has_audio
            or abs(info.duration_ms - selected.duration_ms) > 500
        ):
            raise DependencyError("下载视频与选中高清格式不符，拒绝登记低清或不完整媒体。")
        destination = root / downloaded.name
        if destination.exists() or destination.is_symlink():
            raise DependencyError(
                "下载目标已存在，拒绝覆盖；请使用本地媒体入口或清理未登记文件后重试。"
            )
        report = {
            "version": 1,
            "video_id": selected.video_id,
            "access": access,
            "format_id": selected.format_id,
            "width": info.width,
            "height": info.height,
            "duration_ms": info.duration_ms,
            "audio_language": selected.language,
            "original_audio": selected.original_audio,
            "reason": (
                "anonymous_higher_resolution"
                if access == "anonymous"
                and min(selected.width, selected.height) > min(configured.width, configured.height)
                else "anonymous_clean_extraction"
                if access == "anonymous" and configured.incomplete
                else "anonymous_original_audio"
                if access == "anonymous"
                else "configured_best"
            ),
            "media_checksum": file_checksum(downloaded),
        }
        report_path = downloaded.with_suffix(".download.json")
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        # A hard link publishes atomically without overwriting a concurrently created file.
        try:
            os.link(downloaded, destination)
        except FileExistsError as error:
            raise DependencyError("下载目标已存在，拒绝覆盖。") from error
        try:
            os.link(report_path, destination.with_suffix(".download.json"))
        except OSError:
            # Diagnostics are optional; a valid published media file remains usable.
            pass
        return destination


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


# yt-dlp 的 `speed` / `eta` 是**整段下载的累计平均**（`downloader/common.py` 的
# `calc_speed(start, now, bytes) = bytes / (now - start)`，`start` 取该流的 `start_time`），
# 不是瞬时值。累计平均在一条流内很稳，问题出在**每条流各自重置 start_time**：`bv*+ba`
# 的第二条流刚开头那几百毫秒，平均值是个没意义的小数字，于是速度读出 15 KB/s、ETA 报
# 「剩余 18:52」，几秒后就结束了。所以不换算法——换成 XVT 自己的滑动窗口对稳定下载只会
# 更抖——只在证据不足时闭嘴：当前流累计不到这么久就一个数都不报。
_DOWNLOAD_RATE_WARMUP_SECONDS = 3.0


def _warm_download_rate(
    progress: dict[str, Any],
    downloaded_bytes: int | None,
) -> tuple[float | None, float | None]:
    """预热够了才把 yt-dlp 的速度与 ETA 透出去，否则两个都给 ``None``。

    `elapsed` 是**当前流**已下载的秒数，HTTP 与分片两条腿都带它。缺字段时按「证据不足」
    处理：少显示一个数，而不是显示一个错的。
    """

    try:
        elapsed = float(progress.get("elapsed"))
    except (TypeError, ValueError):
        return None, None
    if not downloaded_bytes or downloaded_bytes < 0 or elapsed < _DOWNLOAD_RATE_WARMUP_SECONDS:
        return None, None
    speed = progress.get("speed")
    eta = progress.get("eta")
    return (
        speed if isinstance(speed, int | float) else None,
        eta if isinstance(eta, int | float) else None,
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
        _source_output_template(root),
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
    _selection: VideoSelection | None = None,
    _anonymous: bool = False,
) -> Path:
    validated_url = validate_remote_url(url)
    root = Path(output_directory).expanduser()
    if not root.is_absolute():
        raise ValueError("download output directory must be absolute")
    root.mkdir(parents=True, exist_ok=True)
    if (
        not audio_only
        and cookies_from_browser
        and _selection is None
        and urlparse(validated_url).hostname
        in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
    ):
        return _download_compared_video(
            validated_url, root, cookies_from_browser, proxy, event_sink, runner
        )

    if not audio_only and _selection is None:
        # 这条路径（无 Cookie / 非 YouTube 的完整视频）没有任何探测数据，所以自己探一次。
        # audio_only 不做：`bestaudio/best` 通常是几十 MB，不值一次网络往返；`_selection`
        # 非空时是 `_download_compared_video` 递归进来的，那边已经预检过了。
        _check_download_space(
            _probe_download_size(
                validated_url,
                VIDEO_FORMAT_SELECTOR,
                cookies=cookies_from_browser,
                proxy=proxy,
                anonymous=_anonymous,
                runner=runner,
            ),
            root,
            event_sink,
        )

    command = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--print",
        "after_move:filepath",
        "-o",
        _source_output_template(root),
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
        command.extend(
            [
                "-f",
                _selection.format_id if _selection else VIDEO_FORMAT_SELECTOR,
                "--merge-output-format",
                "mp4",
                "--format-sort-force",
                "-S",
                "lang,res",
            ]
        )
    if _anonymous:
        command.append("--ignore-config")
    if cookies_from_browser:
        command.extend(["--cookies-from-browser", cookies_from_browser])
    if proxy:
        command.extend(["--proxy", proxy])
    command.append(validated_url)

    runner_kwargs = {}
    if event_sink:
        stream_name: str | None = None
        stream_index = 0

        def handle_line(_stream: str, line: str) -> None:
            nonlocal stream_name, stream_index
            if not line.startswith("XVT_PROGRESS:"):
                return
            try:
                progress = json.loads(line.removeprefix("XVT_PROGRESS:"))
            except json.JSONDecodeError:
                return
            try:
                downloaded: int | None = int(float(progress.get("downloaded_bytes") or 0))
            except (TypeError, ValueError):
                # 解析不出来就是「不知道下了多少」，**不是「下了 0 字节」**：带着 0 去除
                # total 会报出 0%，正是烧录侧刚修掉的那个「进度倒退回 0%」。
                downloaded = None
            try:
                total = int(
                    float(progress.get("total_bytes") or progress.get("total_bytes_estimate") or 0)
                )
            except (TypeError, ValueError):
                total = 0
            # `bv*+ba` 下载两条流，每条流各自从 0% 走到 100%。不标出来的话第二条流开始时
            # 看着就是「100% 之后从 1% 重来」，形似失败重试。`filename` 在 HTTP 与分片两条
            # 下载腿的 downloading / finished 四个 hook 里都有，且一律是最终文件名而不是
            # `.part` 临时名（yt-dlp 的 `downloader/http.py` 与 `downloader/fragment.py`），
            # 所以只认这一个键——再退到 `tmpfilename` 只会在两者不一致时把一条流数成两条。
            name = progress.get("filename")
            if isinstance(name, str) and name and name != stream_name:
                stream_name = name
                stream_index += 1
            speed, eta_seconds = _warm_download_rate(progress, downloaded)
            event_sink(
                create_event(
                    EventType.DOWNLOAD_PROGRESS,
                    downloaded_bytes=downloaded if downloaded is not None else 0,
                    total_bytes=total or None,
                    percent=(
                        round(downloaded / total * 100, 2)
                        if total and downloaded is not None
                        else None
                    ),
                    speed=speed,
                    eta_seconds=eta_seconds,
                    stream_index=stream_index or None,
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

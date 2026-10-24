from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import tempfile
from pathlib import Path
from typing import Any, Literal

from xvt.models import Cue
from xvt.process import CommandError, run_command
from xvt.render import font_for_language, save_bilingual_ass

# doctor 的 ffmpeg 能力探测都是纯查询，秒级返回；给足余量后仍不返回按环境异常处理，
# 归为该检查项 fail，不让 `xvt doctor` 无限期挂住。
_FFMPEG_CAPABILITY_TIMEOUT_SECONDS = 30.0
# 字体检查会真的用 libass 渲染一帧：首次运行可能触发 fontconfig 缓存重建，
# 明显慢于纯查询，所以给更宽的上限。
_FFMPEG_RENDER_TIMEOUT_SECONDS = 60.0


def _check_binary(name: str) -> dict[str, str]:
    path = shutil.which(name)
    if path:
        return {"name": name, "status": "pass", "message": path}
    return {"name": name, "status": "fail", "message": f"{name} is not installed"}


def _ffmpeg_capability(
    arguments: list[str],
    token: str,
    name: str,
    *,
    missing_status: Literal["fail", "warn"] = "fail",
) -> dict[str, str]:
    """探测 ffmpeg 是否带某个能力。

    ``missing_status`` 只作用于「ffmpeg 正常返回、但输出里没有这个 token」这一个分支
    ——也就是「这台机器确实没有这个能力」。ffmpeg 不存在或探测超时都是**判不出来**，
    那两条照旧 fail：前者已由 ``_check_binary("ffmpeg")`` 决定整体结论，后者是环境
    异常，不该因为「这个能力可以降级」就被说成没事。
    """

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return {"name": name, "status": "fail", "message": "ffmpeg is not installed"}
    # 走 run_command 而非裸 subprocess.run：以进程组方式启动并在所有退出路径
    # （含超时）清理孙进程，同时顺带响应全局取消事件。doctor 本来就自行判定
    # 通过/失败（原先 check=False），所以这里用 CommandError 捞回 result 即可，
    # 不需要 run_command 抛出的异常语义。
    try:
        result = run_command(
            [ffmpeg, "-hide_banner", *arguments],
            timeout=_FFMPEG_CAPABILITY_TIMEOUT_SECONDS,
        )
    except CommandError as command_error:
        result = command_error.result
    if result.returncode == 124:
        return {
            "name": name,
            "status": "fail",
            "message": (
                f"ffmpeg capability probe timed out after {_FFMPEG_CAPABILITY_TIMEOUT_SECONDS:g}s"
            ),
        }
    output = f"{result.stdout}\n{result.stderr}"
    if result.returncode == 0 and token in output:
        return {"name": name, "status": "pass", "message": token}
    return {"name": name, "status": missing_status, "message": f"ffmpeg lacks {token}"}


def _check_asr() -> dict[str, str]:
    engines = []
    if importlib.util.find_spec("mlx_whisper"):
        engines.append("mlx")
    if importlib.util.find_spec("faster_whisper"):
        engines.append("faster")
    if engines:
        return {"name": "asr", "status": "pass", "message": ", ".join(engines)}
    return {
        "name": "asr",
        "status": "fail",
        "message": "install the asr-mlx extra or faster-whisper",
    }


# `font_for_language` 按语言给出不同家族（非 darwin / 非 win32 上 zh / ja / ko 各要一份
# Noto Sans CJK，其余语言走拉丁回退）。只探 zh-CN 会让日语用户拿到一份全绿的 doctor
# 和一屏豆腐块——缺字在烧录时才暴露，而那时 QA 只能退回保守规则。
_FONT_CHECK_SAMPLES = (
    ("zh-CN", "字体检查"),
    ("ja", "字幕テスト"),
    ("ko", "자막 확인"),
    ("en", "Font check"),
)


def _probe_ass_font(ffmpeg: str, language: str, sample: str) -> str | None:
    """渲染一帧该语言的样例字幕；成功返回 ``None``，失败返回原因。"""

    with tempfile.TemporaryDirectory() as directory:
        subtitle_path = save_bilingual_ass(
            [
                Cue(
                    1,
                    0,
                    1000,
                    "Font check",
                    target_text=sample,
                    source_language="en",
                    target_language=language,
                )
            ],
            Path(directory) / "font-check.ass",
            video_width=640,
            video_height=360,
        )
        # 同上：改走 run_command 以进程组方式清理超时/取消，避免孙进程遗留。
        try:
            result = run_command(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "info",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=s=640x360:d=0.1",
                    "-vf",
                    f"ass=filename={subtitle_path}",
                    "-frames:v",
                    "1",
                    "-f",
                    "null",
                    "-",
                ],
                timeout=_FFMPEG_RENDER_TIMEOUT_SECONDS,
            )
        except CommandError as command_error:
            result = command_error.result
    if result.returncode == 124:
        return f"timed out after {_FFMPEG_RENDER_TIMEOUT_SECONDS:g}s"
    detail = f"{result.stdout}\n{result.stderr}".casefold()
    font_errors = ("error opening font", "failed to find any fallback", "could not open")
    if result.returncode == 0 and not any(error in detail for error in font_errors):
        return None
    return "libass cannot render it"


def _check_ass_font() -> dict[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return {"name": "subtitle_font", "status": "fail", "message": "ffmpeg is not installed"}
    checked = []
    failures = []
    for language, sample in _FONT_CHECK_SAMPLES:
        font = font_for_language(language)
        reason = _probe_ass_font(ffmpeg, language, sample)
        checked.append(f"{language}: {font}")
        if reason is not None:
            failures.append(f"{language} ({font}): {reason}")
    if failures:
        return {"name": "subtitle_font", "status": "fail", "message": "; ".join(failures)}
    return {"name": "subtitle_font", "status": "pass", "message": ", ".join(checked)}


def _resolved_output(output: str | Path) -> tuple[Path, Path]:
    """返回 (解析后的目标路径, 最近一个已存在的祖先)。

    目标目录可能还没建，所以可写性与可用空间都只能问最近的已存在祖先——两项共用这一份
    解析，免得一项量目标、一项量祖先而给出互相矛盾的结论。
    """

    path = Path(output).expanduser().resolve()
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return path, candidate


def _check_output_directory(output: str | Path) -> dict[str, str]:
    path, candidate = _resolved_output(output)
    if candidate.is_dir() and os.access(candidate, os.W_OK):
        return {"name": "output", "status": "pass", "message": str(path)}
    return {"name": "output", "status": "fail", "message": f"not writable: {path}"}


# 1080p 成品约 1–3 GB/小时，低于这个量连一条短视频都放不下。阈值是经验值，所以这一项
# **永远只是 warn**：它用固定阈值而不是实际需求，没有资格拦任何东西——真正按所选格式
# 大小判定的闸门在下载前那道预检（``download._check_download_space``）。
_MINIMUM_FREE_BYTES = 2 * 2**30


def _check_output_free_space(output: str | Path) -> dict[str, str] | None:
    """输出位置的可用空间；拿不到就返回 ``None``，调用方不追加这一项。

    ``disk_usage`` 在网络卷或权限受限的路径上可能抛 ``OSError``，那是**判不出来**而不是
    「空间不足」，所以不报 warn——与下载前预检同一条纪律：只做证伪，不猜。
    """

    _, candidate = _resolved_output(output)
    try:
        free = shutil.disk_usage(candidate).free
    except OSError:
        return None
    if free >= _MINIMUM_FREE_BYTES:
        return {"name": "output_free_space", "status": "pass", "message": f"{free / 2**30:.1f} GiB"}
    return {
        "name": "output_free_space",
        "status": "warn",
        "message": (
            f"only {free / 2**30:.1f} GiB free at {candidate}; "
            "downloads and burns may run out of space"
        ),
    }


def run_doctor(output: str | Path | None = None) -> dict[str, Any]:
    checks = [
        _check_binary("ffmpeg"),
        _check_binary("ffprobe"),
        _check_binary("yt-dlp"),
        _ffmpeg_capability(["-filters"], "subtitles", "libass"),
        _check_ass_font(),
        # 缺 VideoToolbox 只是回退软件编码（AGENTS 关键设计决定第 8 条），不是跑不了。
        # 此前它报 fail，而 h264_videotoolbox 是 Apple 独有的，于是非 macOS 上
        # `xvt doctor` 整体**永远** fail、退出码永远 3，整份报告因此没有信号价值。
        _ffmpeg_capability(
            ["-encoders"], "h264_videotoolbox", "hardware_encoder", missing_status="warn"
        ),
        _ffmpeg_capability(["-encoders"], "libx264", "software_encoder"),
        _check_asr(),
    ]
    if output:
        checks.append(_check_output_directory(output))
        if (free_space := _check_output_free_space(output)) is not None:
            checks.append(free_space)
    # fail 优先于 warn：有任何一项真的跑不了，整体就是 fail，不被「其余只是建议」冲淡。
    if any(check["status"] == "fail" for check in checks):
        status = "fail"
    elif any(check["status"] == "warn" for check in checks):
        status = "warn"
    else:
        status = "pass"
    return {
        "status": status,
        "platform": platform.system().lower(),
        "architecture": platform.machine().lower(),
        "checks": checks,
    }

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import tempfile
from pathlib import Path
from typing import Any

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


def _ffmpeg_capability(arguments: list[str], token: str, name: str) -> dict[str, str]:
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
    return {"name": name, "status": "fail", "message": f"ffmpeg lacks {token}"}


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


def _check_output_directory(output: str | Path) -> dict[str, str]:
    path = Path(output).expanduser().resolve()
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    if candidate.is_dir() and os.access(candidate, os.W_OK):
        return {"name": "output", "status": "pass", "message": str(path)}
    return {"name": "output", "status": "fail", "message": f"not writable: {path}"}


def run_doctor(output: str | Path | None = None) -> dict[str, Any]:
    checks = [
        _check_binary("ffmpeg"),
        _check_binary("ffprobe"),
        _check_binary("yt-dlp"),
        _ffmpeg_capability(["-filters"], "subtitles", "libass"),
        _check_ass_font(),
        _ffmpeg_capability(["-encoders"], "h264_videotoolbox", "hardware_encoder"),
        _ffmpeg_capability(["-encoders"], "libx264", "software_encoder"),
        _check_asr(),
    ]
    if output:
        checks.append(_check_output_directory(output))
    status = "pass" if all(check["status"] == "pass" for check in checks) else "fail"
    return {
        "status": status,
        "platform": platform.system().lower(),
        "architecture": platform.machine().lower(),
        "checks": checks,
    }

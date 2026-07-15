from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from xvt.models import Cue
from xvt.render import font_for_language, save_bilingual_ass


def _check_binary(name: str) -> dict[str, str]:
    path = shutil.which(name)
    if path:
        return {"name": name, "status": "pass", "message": path}
    return {"name": name, "status": "fail", "message": f"{name} is not installed"}


def _ffmpeg_capability(arguments: list[str], token: str, name: str) -> dict[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return {"name": name, "status": "fail", "message": "ffmpeg is not installed"}
    completed = subprocess.run(
        [ffmpeg, "-hide_banner", *arguments],
        capture_output=True,
        check=False,
        text=True,
    )
    output = f"{completed.stdout}\n{completed.stderr}"
    if completed.returncode == 0 and token in output:
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


def _check_ass_font() -> dict[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    font = font_for_language("zh-CN")
    if not ffmpeg:
        return {"name": "subtitle_font", "status": "fail", "message": "ffmpeg is not installed"}
    with tempfile.TemporaryDirectory() as directory:
        subtitle_path = save_bilingual_ass(
            [
                Cue(
                    1,
                    0,
                    1000,
                    "Font check",
                    target_text="字体检查",
                    source_language="en",
                    target_language="zh-CN",
                )
            ],
            Path(directory) / "font-check.ass",
            video_width=640,
            video_height=360,
        )
        completed = subprocess.run(
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
            capture_output=True,
            check=False,
            text=True,
        )
    detail = f"{completed.stdout}\n{completed.stderr}".casefold()
    font_errors = ("error opening font", "failed to find any fallback", "could not open")
    if completed.returncode == 0 and not any(error in detail for error in font_errors):
        return {"name": "subtitle_font", "status": "pass", "message": font}
    return {
        "name": "subtitle_font",
        "status": "fail",
        "message": f"libass cannot render the default font: {font}",
    }


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

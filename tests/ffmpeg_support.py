"""集成测试的 FFmpeg 前置条件探测。

烧录与预览把字幕经 ``-vf ass=filename=...`` 送进 filtergraph，所以真实前置条件
不是「ffmpeg 可执行文件存在」，而是**这个 ffmpeg 带 libass 的 ass 滤镜**。

两者并不等价：homebrew 把 ffmpeg 拆成了 `ffmpeg`（精简，不带 ``--enable-libass``）
与 `ffmpeg-full` 两个 formula。按「有 ffmpeg 就跑」的门控在精简版上不会跳过，而是
崩在 ``No such filter: 'ass'`` —— 这正是 2026-10-17 macOS CI 上出现的失败。

探测结果按进程缓存：``ffmpeg -filters`` 要跑一次子进程，而门控会被多个测试类求值。
"""

from __future__ import annotations

import shutil
import subprocess
from functools import lru_cache

FFMPEG_REQUIREMENT = (
    "FFmpeg integration tests require ffmpeg and ffprobe built with libass "
    "(the `ass` filter); homebrew users need the ffmpeg-full formula"
)


@lru_cache(maxsize=1)
def ffmpeg_can_render_ass() -> bool:
    """ffmpeg 与 ffprobe 都在，且该 ffmpeg 提供 ``ass`` 滤镜。"""

    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        return False
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    # `-filters` 的每行形如 " .. ass               V->V       Render ASS subtitles..."，
    # 取第二个字段做整词比对，避免 `assrender` 之类同前缀滤镜误判。
    return any(
        len(parts) >= 2 and parts[1] == "ass"
        for parts in (line.split() for line in result.stdout.splitlines())
    )

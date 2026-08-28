"""文件内容 checksum 的唯一实现。

刻意做成零 ``xvt`` 依赖的叶子模块：``recovery``、``candidate_qa`` 与
``pipeline.common`` 都要用它，而 ``recovery`` 顶层依赖 ``candidate_qa``，
把实现放在其中任何一个里都会让另一个成环。
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def file_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

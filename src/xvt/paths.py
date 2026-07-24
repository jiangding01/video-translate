"""用户输出路径安全：绝对路径要求与覆盖保护。

烧录 / 预览允许写到 Job 目录之外，但不得覆盖源媒体、job.json，
以及除「正在写入的同 kind」以外的已登记 artifact（含 hardlink / samefile）。
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from xvt.errors import UserInputError
from xvt.job_store import get_job_file
from xvt.models import Job


def resolve_absolute_output(path: str | Path, *, label: str = "output") -> Path:
    """expanduser 后要求绝对路径；相对路径视为用户输入错误。"""
    output_path = Path(path).expanduser()
    if not output_path.is_absolute():
        raise UserInputError(f"{label} path must be absolute")
    return output_path


def paths_collide(left: Path, right: Path) -> bool:
    """路径是否指向同一文件（resolve 相等，或存在时 samefile / hardlink）。"""
    left_resolved = left.expanduser().resolve()
    right_resolved = right.expanduser().resolve()
    if left_resolved == right_resolved:
        return True
    left_path = left.expanduser()
    right_path = right.expanduser()
    if left_path.exists() and right_path.exists():
        try:
            return left_path.samefile(right_path)
        except OSError:
            return False
    return False


def protected_job_paths(
    job: Job,
    media_path: Path,
    *,
    writing_kind: str,
    extra: Iterable[Path] = (),
) -> list[Path]:
    """构造不得被输出覆盖的路径集合。

    ``writing_kind`` 对应的已登记 artifact 被排除，以支持同 kind 重写
    （重烧 ``burned_video``、重预览 ``preview_video``）。
    """
    return [
        media_path,
        get_job_file(job),
        *extra,
        *(
            Path(artifact.path).expanduser()
            for artifact in job.artifacts
            if artifact.kind != writing_kind
        ),
    ]


def assert_safe_output(
    output: Path,
    *,
    protected: Iterable[Path],
    label: str = "output",
) -> None:
    """若 ``output`` 与任一受保护路径冲突则抛出 ``UserInputError``。"""
    for protected_path in protected:
        if paths_collide(output, protected_path):
            raise UserInputError(
                f"{label} must not overwrite the source media, job file, or an existing artifact"
            )

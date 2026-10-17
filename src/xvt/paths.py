"""用户输出路径安全：绝对路径要求与覆盖保护。

烧录 / 预览允许写到 Job 目录之外，但不得覆盖源媒体、job.json、Job 记着的
用户原始输入文件（本地 ``job.input`` 与用户传入的字幕），以及除「正在写入的
同 kind」以外的已登记 artifact（含 hardlink / samefile）。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path
from urllib.parse import urlparse

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


def _local_path(value: object) -> Path | None:
    """把 Job 里记着的输入值解析为本地路径；URL 与空值返回 None。"""

    if not isinstance(value, str) or not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        return None
    return Path(value).expanduser()


def _user_input_paths(job: Job) -> Iterator[Path]:
    """Job 登记过的、位于 Job 目录之外的用户原始输入文件。

    这些文件既不是 ``media_path``，也不是已登记 artifact——``input_subtitles``
    之类登记的是 Job 目录内的副本，原件始终在用户自己的目录里。纯音频 Job 的
    ``xvt burn JOB --media video.mp4 --output <job.input 的路径>`` 会让
    ``ffmpeg -y`` 直接覆盖原始音频，属于用户数据丢失，必须在入口拦住。
    """

    local_input = _local_path(job.input)
    if local_input is not None:
        yield local_input
    # 用户传入的源字幕（run/transcribe 的 --subtitles）。
    subtitles = _local_path(job.config.get("subtitles"))
    if subtitles is not None:
        yield subtitles
    # 用户传入的译文字幕（run 的 --translation-subtitles，固化在 workflow 快照里）。
    workflow = job.config.get("workflow")
    if isinstance(workflow, dict):
        translation_subtitles = _local_path(workflow.get("translation_subtitles"))
        if translation_subtitles is not None:
            yield translation_subtitles


def protected_job_paths(
    job: Job,
    media_path: Path,
    *,
    writing_kind: str,
    extra: Iterable[Path] = (),
) -> list[Path]:
    """构造不得被输出覆盖的路径集合。

    ``writing_kind`` 对应的已登记 artifact 被排除，以支持同 kind 重写
    （重烧 ``burned_video``、重预览 ``preview_video``）。用户原始输入文件
    **不**按 ``writing_kind`` 排除：它们从来不是本次写入的目标。
    """
    return [
        media_path,
        get_job_file(job),
        *_user_input_paths(job),
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
                f"{label} must not overwrite the source media, job file, "
                "an original input file, or an existing artifact"
            )

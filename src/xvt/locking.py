"""Job 与 batch 的目录级独占执行锁。

基于 lockfile + ``flock(LOCK_EX | LOCK_NB)``：同一 Job 目录或 batch 文件同时只允许
一个 XVT 进程写入，第二个进程立即以 :class:`~xvt.errors.JobLockedError`（退出码 7）
失败，不排队等待。锁文件记录持有者 PID 便于提示；释放时不删除锁文件，避免
「删除后另一进程对新文件加锁」的竞态。锁序固定为先 batch 后 job，杜绝死锁。

flock 锁绑定在打开的文件描述上而非进程上，因此进程崩溃或被 SIGKILL 时内核会
自动释放锁，不会留下需要人工清理的陈旧锁。

注意：锁 fd 不得泄漏给外部子进程——`process.py` 的 Popen 依赖 Python 默认的
``close_fds=True`` 在子进程中关闭该 fd；若未来有代码放开 close_fds 或使用
pass_fds，ffmpeg/yt-dlp 的孙进程可能在持锁进程退出后继续占有锁。
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from xvt.errors import JobLockedError

_JOB_LOCK_NAME = ".xvt.lock"
_BATCH_LOCK_SUFFIX = ".lock"


@contextmanager
def _acquire(lock_file: Path, *, description: str) -> Iterator[None]:
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_file.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            handle.seek(0)
            holder = handle.read().strip() or "unknown"
            raise JobLockedError(
                f"{description} 正被另一个 XVT 进程（PID {holder}）执行，请等待其结束后重试"
            ) from error
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        try:
            yield
        finally:
            with suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


@contextmanager
def job_lock(job_file: str | Path) -> Iterator[None]:
    """独占一个已存在 Job 的目录（锁文件为 ``<job-dir>/.xvt.lock``）。"""
    path = Path(job_file).expanduser().resolve()
    with _acquire(path.parent / _JOB_LOCK_NAME, description=f"Job {path}"):
        yield


@contextmanager
def batch_lock(batch_file: str | Path) -> Iterator[None]:
    """独占一个 batch（锁文件为 ``<batch.json>.lock``，与 batch 文件同目录）。

    锁序约定：需要同时持有 batch 锁与 job 锁时，必须先取 batch 锁再取 job 锁。
    """
    path = Path(batch_file).expanduser().resolve()
    lock_file = path.with_name(path.name + _BATCH_LOCK_SUFFIX)
    with _acquire(lock_file, description=f"Batch {path}"):
        yield

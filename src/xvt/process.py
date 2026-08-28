from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
from typing import TextIO

from xvt.cancellation import cancel_event as _global_cancel_event
from xvt.cancellation import cancellation_requested
from xvt.events import EventSink, EventType, create_event, error_event_fields, redact_url

# 静默超时的建议值：仅供能保证持续输出的调用方（如带 -progress pipe:1 的烧录）
# 显式传入。默认不启用——yt-dlp 提取阶段、慢网络输入等场景存在合法的长时间
# 静默，无输出不等于挂死；挂死进程可由用户取消（SIGINT/SIGTERM）连同进程组清理。
SUGGESTED_INACTIVITY_TIMEOUT_SECONDS = 120.0

# 终止进程组时先发 SIGTERM 的宽限时间，超过后升级为 SIGKILL。
_TERMINATE_GRACE_SECONDS = 2.0

_REDACTED = "<redacted>"
_SENSITIVE_OPTIONS = {
    "--cookies",
    "--cookies-from-browser",
    "--password",
    "--proxy",
    "--username",
    "--video-password",
}
_SENSITIVE_HEADERS = {"authorization", "cookie", "proxy-authorization", "x-api-key"}


@dataclass(frozen=True, slots=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class CommandError(RuntimeError):
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        super().__init__(f"Command failed with exit code {result.returncode}: {detail}")


class CommandCancelled(BaseException):
    """外部命令因协作式取消（SIGINT/SIGTERM 置位的 cancel 事件）被终止。

    不是命令失败：进程组已被清理，调用方应把当前 stage 持久化为中断状态后退出。
    继承 ``BaseException``（参照 ``asyncio.CancelledError``），确保取消不会被
    pipeline/batch 的 ``except Exception`` 失败隔离误吞。
    """


def raise_if_cancelled() -> None:
    """协作式取消检查点：全局取消事件已置位时抛出 ``CommandCancelled``。

    供不经过 ``run_command`` 的长循环（翻译批次、批处理条目）在迭代边界调用。
    """
    if cancellation_requested():
        raise CommandCancelled("cancelled by signal")


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    """终止整个进程组（含 yt-dlp 派生的孙进程），先 SIGTERM 后升级 SIGKILL。

    进程以 ``start_new_session=True`` 启动，因此进程组 ID 等于其 PID。
    """
    if process.poll() is not None:
        return
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()


def _run_process(
    command: tuple[str, ...],
    *,
    cwd: str | Path | None,
    timeout: float | None,
    inactivity_timeout: float | None,
    cancel: Event | None,
    line_callback: Callable[[str, str], None] | None,
) -> CommandResult:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    lines: Queue[tuple[str, str]] = Queue()
    output: dict[str, list[str]] = {"stdout": [], "stderr": []}

    def read_stream(name: str, stream: TextIO) -> None:
        for line in stream:
            lines.put((name, line))
        stream.close()

    threads = [
        Thread(target=read_stream, args=("stdout", process.stdout), daemon=True),
        Thread(target=read_stream, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()

    started = time.monotonic()
    deadline = started + timeout if timeout is not None else None
    last_activity = started
    timeout_detail: str | None = None
    cancelled = False
    try:
        while process.poll() is None:
            now = time.monotonic()
            if cancel is not None and cancel.is_set():
                cancelled = True
                break
            if deadline is not None and now >= deadline:
                timeout_detail = f"command timed out after {timeout} seconds"
                break
            if inactivity_timeout is not None and now - last_activity >= inactivity_timeout:
                timeout_detail = f"command produced no output for {inactivity_timeout} seconds"
                break
            try:
                name, line = lines.get(timeout=0.1)
            except Empty:
                continue
            last_activity = time.monotonic()
            output[name].append(line)
            if line_callback is not None:
                line_callback(name, line.rstrip("\r\n"))
    finally:
        # 任何退出路径（正常结束、超时、取消、KeyboardInterrupt）都清理整个进程组，
        # 避免 ffmpeg/yt-dlp 及其孙进程沦为孤儿。
        _terminate_process_group(process)

    for thread in threads:
        thread.join(timeout=5.0)
    while not lines.empty():
        name, line = lines.get_nowait()
        output[name].append(line)
        if line_callback is not None:
            line_callback(name, line.rstrip("\r\n"))

    if cancelled:
        raise CommandCancelled(f"command cancelled: {command[0]}")

    return CommandResult(
        args=command,
        returncode=124 if timeout_detail else process.returncode,
        stdout="".join(output["stdout"]),
        stderr="".join(output["stderr"]) + (timeout_detail or ""),
    )


def _redact_header(value: str) -> str:
    name, separator, _ = value.partition(":")
    if separator and name.strip().lower() in _SENSITIVE_HEADERS:
        return f"{name}:{_REDACTED}"
    return value


def _redact_command(command: tuple[str, ...]) -> list[str]:
    redacted: list[str] = []
    redact_next = False
    redact_header = False
    for argument in command:
        if redact_next:
            redacted.append(_REDACTED)
            redact_next = False
            continue
        if redact_header:
            redacted.append(_redact_header(argument))
            redact_header = False
            continue

        option, separator, _ = argument.partition("=")
        if option in _SENSITIVE_OPTIONS:
            if separator:
                redacted.append(f"{option}={_REDACTED}")
            else:
                redacted.append(argument)
                redact_next = True
            continue
        if option == "--add-header":
            if separator:
                redacted.append(f"{option}={_redact_header(argument.partition('=')[2])}")
            else:
                redacted.append(argument)
                redact_header = True
            continue
        redacted.append(redact_url(argument))
    return redacted


def run_command(
    args: Sequence[str],
    *,
    cwd: str | Path | None = None,
    timeout: float | None = None,
    inactivity_timeout: float | None = None,
    cancel: Event | None = None,
    event_sink: EventSink | None = None,
    line_callback: Callable[[str, str], None] | None = None,
) -> CommandResult:
    """以进程组方式执行外部命令，任何退出路径都保证清理全部子孙进程。

    - ``timeout``：整体硬超时；到期终止进程组并以退出码 124 报 ``CommandError``。
    - ``inactivity_timeout``：静默超时，默认不启用；仅由能保证持续产生输出的
      调用方显式设置，连续无输出超过该秒数视为挂死，处理同上。
    - ``cancel``：协作式取消事件；置位后终止进程组并抛 ``CommandCancelled``。
      默认监听全局取消事件（由 CLI 的 SIGINT/SIGTERM 处理器置位）。
    """
    command = tuple(str(arg) for arg in args)
    if not command:
        raise ValueError("command must not be empty")

    event_command = _redact_command(command)

    if event_sink:
        event_sink(create_event(EventType.COMMAND_STARTED, command=event_command))

    try:
        result = _run_process(
            command,
            cwd=cwd,
            timeout=timeout,
            inactivity_timeout=inactivity_timeout,
            cancel=cancel if cancel is not None else _global_cancel_event(),
            line_callback=line_callback,
        )
    except FileNotFoundError as error:
        result = CommandResult(command, 127, "", str(error))
        if event_sink:
            event_sink(
                create_event(
                    EventType.COMMAND_FAILED,
                    command=event_command,
                    exit_code=127,
                    **error_event_fields("dependency_or_process", str(error)),
                )
            )
        raise CommandError(result) from error

    if result.returncode != 0:
        if event_sink:
            detail = result.stderr.strip() or result.stdout.strip() or "no output"
            event_sink(
                create_event(
                    EventType.COMMAND_FAILED,
                    command=event_command,
                    exit_code=result.returncode,
                    **error_event_fields("dependency_or_process", detail),
                )
            )
        raise CommandError(result)

    if event_sink:
        event_sink(create_event(EventType.COMMAND_COMPLETED, command=event_command, exit_code=0))
    return result

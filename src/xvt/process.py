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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from xvt.events import EventSink, EventType, create_event, error_event_fields

# 所有外部命令的默认静默超时：连续无任何 stdout/stderr 输出超过该秒数即视为挂死。
# ffmpeg（默认 stats 或 -progress pipe:1）、yt-dlp、ffprobe 的健康运行要么很快结束、
# 要么持续产生输出，因此该兜底不会误伤长时间的合法烧录或转码。
DEFAULT_INACTIVITY_TIMEOUT_SECONDS = 120.0

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
_SENSITIVE_QUERY_KEYS = {
    "access_token",
    "api_key",
    "auth",
    "authorization",
    "key",
    "signature",
    "sig",
    "token",
    "x-amz-credential",
    "x-amz-security-token",
    "x-amz-signature",
}


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


class CommandCancelled(Exception):
    """外部命令因协作式取消（SIGINT/SIGTERM 置位的 cancel 事件）被终止。

    不是命令失败：进程组已被清理，调用方应把当前 stage 持久化为中断状态后退出。
    """


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


def _redact_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return value

    netloc = (
        f"{_REDACTED}@{parsed.netloc.rsplit('@', 1)[1]}"
        if parsed.username is not None
        else parsed.netloc
    )
    query = urlencode(
        [
            (key, _REDACTED if key.lower() in _SENSITIVE_QUERY_KEYS else query_value)
            for key, query_value in parse_qsl(parsed.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))


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
        redacted.append(_redact_url(argument))
    return redacted


def run_command(
    args: Sequence[str],
    *,
    cwd: str | Path | None = None,
    timeout: float | None = None,
    inactivity_timeout: float | None = DEFAULT_INACTIVITY_TIMEOUT_SECONDS,
    cancel: Event | None = None,
    event_sink: EventSink | None = None,
    line_callback: Callable[[str, str], None] | None = None,
) -> CommandResult:
    """以进程组方式执行外部命令，任何退出路径都保证清理全部子孙进程。

    - ``timeout``：整体硬超时；到期终止进程组并以退出码 124 报 ``CommandError``。
    - ``inactivity_timeout``：静默超时，默认 120 秒；连续无输出即视为挂死，处理同上。
      传 ``None`` 可禁用（仅限确认会长时间静默的命令）。
    - ``cancel``：协作式取消事件；置位后终止进程组并抛 ``CommandCancelled``。
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
            cancel=cancel,
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
